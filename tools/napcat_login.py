"""通过 NapCat 自己的 WebUI API 操作登录（不依赖 QQ 窗口）。

为什么需要这个工具：

    NapCat 注入 QQ 之后，登录二维码**不一定**会画在 QQ 窗口里 ——
    窗口被隐藏、Electron 没渲染、或者干脆用了 `-q` 快速登录失败后回退，
    都会出现「进程在跑、6099 在听、但屏幕上什么都没有」的情况。

    而 NapCat 的 WebUI（默认 http://127.0.0.1:6099）一直有一组
    `/api/QQLogin/*` 接口，能直接读到当前登录阶段、二维码 URL，
    还能强制刷新二维码。这就是「看不见窗口也能登录」的后门。

★ 端口不是固定的：

    `webui.json` 里写的是 6099，但 NapCat 发现 6099 被另一个实例占着时，
    会**自己把 WebUI 往上挪**（实测第二个实例落在 6100）。这时如果还只问
    6099，问到的是那个卡在 waiting_qrcode 的旧实例 —— 于是「QQ 明明在正常
    收发消息，status 却一直说没登录」。本工具因此改为：从配置端口开始向上
    扫 10 个端口，挑**真正 isLogin=true** 的那个实例来回答。

用法：

    python tools/napcat_login.py status          # 看所有实例 + 正在登录的那个
    python tools/napcat_login.py qr              # 刷新并渲染二维码到 data/napcat-qrcode.png
    python tools/napcat_login.py wait            # 盯到登录成功（默认 300 秒）
    python tools/napcat_login.py quick           # 看快速登录列表（空=凭据已失效）
    python tools/napcat_login.py status --port 6100   # 指定实例（排查用）

鉴权：WebUI 的 token 在 `<NapCat>/config/webui.json` 里，登录时提交
`sha256(token + ".napcat")`，拿回来的 Credential 用 `Authorization: Bearer` 带回去。
"""
from __future__ import annotations

import base64  # noqa: F401  (保留：旧版其它脚本 import 它会用到)
import hashlib
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
QR_PATH = ROOT / "data" / "napcat-qrcode.png"

# 端口被占用时 NapCat 会自己往上找空位，实测 +1。扫 10 个足够。
SCAN_PORTS = 10

# 本机请求必须绕开系统代理，否则会被 HTTP_PROXY 劫持
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def shell_dir() -> Path:
    """NapCat.Shell 在哪。

    优先借 `app.napcat.find_shell()` 的探测（环境变量 → 常见路径 → 同级目录 →
    注册表），这样整个文件夹换个位置也不用改代码；探测不到才用下面的兜底值。
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app.napcat import find_shell  # 延迟 import：单独跑这个脚本时也能用

        found = find_shell()
        if found:
            return Path(found)
    except Exception:
        pass
    return Path(r"D:\QQBot\NapCatQQ\NapCat.Shell")


def webui_file() -> Path:
    return shell_dir() / "config" / "webui.json"


def webui_endpoint() -> tuple[str, str]:
    """返回配置里的 (base_url, token)。读不到配置就用默认值。"""
    base, token = "http://127.0.0.1:6099", ""
    try:
        cfg = json.loads(webui_file().read_text(encoding="utf-8"))
        token = cfg.get("token", "") or ""
        port = cfg.get("port", 6099)
        host = cfg.get("host", "::")
        if host in ("::", "0.0.0.0", ""):
            host = "127.0.0.1"
        base = f"http://{host}:{port}"
    except Exception:
        pass
    return base, token


def config_port() -> int:
    """webui.json 里写的端口（NapCat 的第一个选择，未必是真正在听的那个）。"""
    try:
        return int(json.loads(webui_file().read_text(encoding="utf-8")).get("port", 6099))
    except Exception:
        return 6099


def _port_open(port: int, timeout: float = 0.35) -> bool:
    """TCP 探活。比 HTTP 便宜，用来决定值不值得发请求。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def candidate_ports(extra: Optional[List[int]] = None) -> List[int]:
    """配置端口 + 它上面 10 个空位里真正在听的端口。配置端口永远排第一。"""
    base = config_port()
    ports: List[int] = [base]
    for port in extra or []:
        if port not in ports:
            ports.append(port)
    for port in range(base, base + SCAN_PORTS):
        if port not in ports and _port_open(port):
            ports.append(port)
    return ports


# Credential 按 base 分开缓存：两个实例各有自己的票，混用会 401。
_CRED: Dict[str, Dict[str, Any]] = {}


def credential(base: Optional[str] = None, token: Optional[str] = None,
               force: bool = False) -> str:
    """用 webui token 换一个一小时内有效的 Credential。

    **必须缓存**：webui.json 里有 `loginRate: 10`，一分钟内换票超过 10 次
    接口会直接返回 `{"code":-1}`（没有 data 字段）。轮询状态时每次都换票
    会立刻把自己限流掉，所以这里缓存到过期（按 50 分钟算，留安全余量）。
    """
    now = time.time()
    base = base or webui_endpoint()[0]
    token = token if token is not None else webui_endpoint()[1]
    ent = _CRED.get(base) or {}
    if not force and ent.get("value") and now - float(ent.get("at") or 0) < 50 * 60:
        return str(ent["value"])
    digest = hashlib.sha256((token + ".napcat").encode()).hexdigest()
    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"hash": digest}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with _OPENER.open(req, timeout=8) as r:
        body = json.loads(r.read())
    if not body.get("data"):
        raise RuntimeError(f"WebUI 换票失败（可能被限流）：{body.get('message')}")
    _CRED[base] = {"value": body["data"]["Credential"], "at": now}
    return str(_CRED[base]["value"])


def api(path: str, payload: Optional[dict] = None, timeout: float = 10.0,
        base: Optional[str] = None) -> dict:
    """调一个 /api 下的接口；票过期会自动换一次。base 不传就用配置端口。"""
    base, token = base or webui_endpoint()[0], webui_endpoint()[1]

    def _do(cred: str) -> dict:
        req = urllib.request.Request(
            base + "/api" + path,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + cred},
        )
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read())

    body = _do(credential(base, token))
    if body.get("message") == "Unauthorized":
        body = _do(credential(base, token, force=True))
    return body


def instances(extra: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """把所有能应答的 NapCat WebUI 都问一遍。

    返回 [{"port", "base", "data", "error"}]，按配置端口优先排序。
    单个实例超时/401 不影响其它实例。
    """
    out: List[Dict[str, Any]] = []
    for port in candidate_ports(extra):
        base = f"http://127.0.0.1:{port}"
        item: Dict[str, Any] = {"port": port, "base": base, "data": {}, "error": ""}
        try:
            body = api("/QQLogin/CheckLoginStatus", base=base)
            data = body.get("data")
            if isinstance(data, dict):
                item["data"] = data
            else:
                item["error"] = f"code={body.get('code')} message={body.get('message')}"
        except Exception as e:  # 端口没在听 / 不是 NapCat / 换票失败
            item["error"] = f"{type(e).__name__}: {e}"[:200]
        if item["data"] or item["error"]:
            out.append(item)
    return out


def pick_instance(extra: Optional[List[int]] = None) -> Dict[str, Any]:
    """挑一个实例回答：优先已登录，其次 coreReady，再其次能应答的。"""
    found = instances(extra)
    for item in found:
        if item["data"].get("isLogin"):
            return item
    for item in found:
        if item["data"].get("coreReady"):
            return item
    if found:
        return found[0]
    return {"port": 0, "base": "", "data": {},
            "error": "没有任何 NapCat WebUI 应答（NapCat 没起来？）"}


def login_state(port: Optional[int] = None) -> dict:
    """登录状态。关键字段：

    isLogin       —— 真的登录进去了
    loginPhase    —— waiting_qrcode / waiting_scan / waiting_confirm / ...
    qrcodeurl     —— 当前二维码里编的 URL（扫码用的就是它）
    loginError    —— 出码失败时的原因

    port 不传时自动挑**已登录的那个实例**（见模块开头的「端口不是固定的」）。
    """
    if port is not None:
        return (api("/QQLogin/CheckLoginStatus",
                    base=f"http://127.0.0.1:{port}").get("data") or {})
    return pick_instance()["data"]


def status_report(extra: Optional[List[int]] = None) -> Dict[str, Any]:
    """给 status 用的完整报告：所有实例 + 选中的那个。"""
    found = instances(extra)
    active = pick_instance(extra)
    return {
        "activePort": active["port"],
        "activeBase": active["base"],
        "active": active["data"],
        "activeError": active["error"],
        "instances": [
            {
                "port": it["port"],
                "isLogin": bool(it["data"].get("isLogin")),
                "loginPhase": it["data"].get("loginPhase", ""),
                "coreReady": bool(it["data"].get("coreReady")),
                "error": it["error"],
            }
            for it in found
        ],
    }


def refresh_qrcode(port: Optional[int] = None) -> dict:
    base = f"http://127.0.0.1:{port}" if port else pick_instance()["base"]
    if not base:
        raise RuntimeError("没有 NapCat WebUI 应答")
    return api("/QQLogin/RefreshQRcode", base=base)


def render_qrcode(url: str, out: Path = QR_PATH, box: int = 14) -> Path:
    """把二维码 URL 画成 PNG。box=14 大约 500px，手机隔屏幕也能扫。"""
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=box, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def wait_login(timeout: float = 300, interval: float = 3.0,
               port: Optional[int] = None) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        if port is None:
            item = pick_instance()
        else:
            item = {"port": port, "data": login_state(port)}
        st = item["data"]
        phase = st.get("loginPhase")
        if phase != last:
            _say(f"  [{time.strftime('%H:%M:%S')}] 端口 {item['port']} · {phase}")
            last = phase
        if st.get("isLogin"):
            return st
        if st.get("loginError"):
            _say("  错误:" + str(st["loginError"]))
            return st
        time.sleep(interval)
    return login_state(port)


def _say(msg: str, *, err: bool = False) -> None:
    """打印一行，编码兜底。

    Windows 控制台常见 cp936：中文能编码，`✓`/`×` 直接抛 UnicodeEncodeError，
    把整条命令带崩。这里统一转成控制台能编码的字符再写。
    """
    stream = sys.stderr if err else sys.stdout
    try:
        stream.write(msg + "\n")
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", "utf-8") or "utf-8"
        stream.write(msg.encode(enc, "replace").decode(enc) + "\n")
    try:
        stream.flush()
    except Exception:
        pass


def _usage() -> None:
    print(__doc__)


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    port: Optional[int] = None
    if "--port" in args:
        i = args.index("--port")
        try:
            port = int(args[i + 1])
        except (IndexError, ValueError):
            _say("--port 后面要跟端口号", err=True)
            return 2
        del args[i:i + 2]
    cmd = (args[0] if args else "status").lower()
    extra = [port] if port else None

    if cmd == "status":
        if port:
            # 明确指定了端口，就别再自动挑 —— 排查时用户要看的就是这一台。
            data = login_state(port)
            rep = {
                "activePort": port,
                "activeBase": f"http://127.0.0.1:{port}",
                "active": data,
                "activeError": "",
                "instances": [{
                    "port": port,
                    "isLogin": bool(data.get("isLogin")),
                    "loginPhase": data.get("loginPhase", ""),
                    "coreReady": bool(data.get("coreReady")),
                    "error": "",
                }],
            }
        else:
            rep = status_report()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        live = [it for it in rep["instances"] if it["isLogin"]]
        if not rep["instances"]:
            _say("x 没有任何 NapCat WebUI 应答", err=True)
        elif not live:
            _say(f"x 找到 {len(rep['instances'])} 个 WebUI，但都没登录"
                 f"（在问端口 {rep['activePort']}）", err=True)
        elif len(rep["instances"]) > 1:
            _say("? 检测到 " + str(len(rep["instances"])) + " 个 NapCat 实例："
                 + "、".join(f"{it['port']}{'(已登录)' if it['isLogin'] else '(未登录)'}"
                            for it in rep["instances"])
                 + f"；正在用 {rep['activePort']}"
                 + "。建议关掉未登录的那个，避免两套 QQ 抢账号库。", err=True)
        return 0 if live else 1

    if cmd == "quick":
        item = {"base": f"http://127.0.0.1:{port}"} if port else pick_instance(extra)
        if not item["base"]:
            _say("x 没有 NapCat WebUI 应答", err=True)
            return 1
        data = api("/QQLogin/GetQuickLoginListNew", base=item["base"]).get("data")
        print(json.dumps(data, ensure_ascii=False))
        return 0

    if cmd == "qr":
        item = {"port": port, "base": f"http://127.0.0.1:{port}",
                "data": login_state(port)} if port else pick_instance(extra)
        if not item["base"]:
            _say("x 没有 NapCat WebUI 应答（NapCat 没起来？）", err=True)
            return 1
        if item["data"].get("isLogin"):
            _say(f"OK 端口 {item['port']} 的实例已经登录，不用扫码")
            return 0
        refresh_qrcode(item["port"])
        time.sleep(1)
        st = login_state(item["port"])
        url = st.get("qrcodeurl") or ""
        if not url:
            _say("没拿到二维码，状态：" + json.dumps(st, ensure_ascii=False))
            return 1
        p = render_qrcode(url)
        _say(f"{time.strftime('%H:%M:%S')} 已生成 {p}"
             f"（端口 {item['port']} · {st.get('loginPhase')}）")
        return 0

    if cmd == "wait":
        secs = float(args[1]) if len(args) > 1 else 300
        st = wait_login(secs, port=port)
        print(json.dumps(st, ensure_ascii=False))
        return 0 if st.get("isLogin") else 1

    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
