"""体检：把「为什么它不说话」的所有可能原因一次列清楚。

    python doctor.py                 # 全查
    python doctor.py --config x.yaml

三项体检各管一段：
    python main.py --check           # 配置对不对（花不花钱、人设、作息）
    python doctor.py                 # 环境 + NapCat 通不通（本文件）
    python tools/report.py           # 上线后到底花了多少

doctor.py 只读不写：不启动 NapCat、不改配置、不发消息。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import netprobe  # noqa: E402
from app.config import Config, load_config  # noqa: E402
from app.store import Store  # noqa: E402

OK, WARN, BAD = "✓", "!", "✗"
_score = {"ok": 0, "warn": 0, "bad": 0}


def say(level: str, text: str, hint: str = "") -> None:
    mark = {OK: "✓", WARN: "!", BAD: "✗"}.get(level[0], "·")
    print(f"  {mark} {text}")
    if hint:
        for line in hint.splitlines():
            print(f"      {line}")
    _score["ok" if mark == "✓" else ("warn" if mark == "!" else "bad")] += 1


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 44 - len(title)))


def check_python() -> None:
    section("Python")
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        say(OK, f"Python {ver}（{sys.executable}）")
    else:
        say(BAD, f"Python {ver} 太旧", "本项目要 3.10 以上（用到了 match-free 的新语法糖）。\n"
                                        "去 python.org 装个新的，装的时候勾上 Add to PATH。")

    if sys.platform == "win32":
        say(OK, "系统 Windows")
    else:
        say(WARN, f"系统 {sys.platform}", "本项目按 Windows 路径写的（NapCat 的启动脚本也是）。")


def check_deps() -> None:
    section("依赖")
    missing = []
    for mod, why in (("httpx", "调 DeepSeek"), ("websockets", "连 NapCat"),
                     ("yaml", "读配置")):
        try:
            __import__(mod)
            say(OK, f"{mod} 已装（{why}）")
        except ImportError:
            missing.append(mod)
            say(BAD, f"{mod} 没装（{why}）")
    if missing:
        say(BAD, "装一下：", "python -m pip install -r requirements.txt\n"
                             f"（缺的就是：{' '.join(missing)}）")


def check_config(path: str | None) -> Config | None:
    section("配置")
    try:
        cfg = load_config(Path(path) if path else None)
    except FileNotFoundError:
        say(BAD, "找不到 config.yaml", "跑 python setup.py，它会一路问你问题然后把配置写好。")
        return None
    except Exception as e:
        say(BAD, f"config.yaml 读不了：{e}", "多半是 YAML 缩进错了。对照 config.example.yaml 看。")
        return None

    say(OK, f"config.yaml 读到了（{(Path(path) if path else ROOT / 'config.yaml').name}）")

    if cfg.llm.mock:
        say(WARN, "llm.mock = true", "现在是假回复模式，不联网、不花钱，但也真的不会聊天。\n"
                                     "正式用之前改成 false。")
    elif not cfg.llm.api_key:
        say(BAD, "没配 API Key", "在 config.yaml 里填 llm.api_key，或者设环境变量 DEEPSEEK_API_KEY。\n"
                                 "去 https://platform.deepseek.com/api_keys 拿一个。")
    else:
        say(OK, f"API Key 有（{cfg.llm.api_key[:6]}…）")

    if cfg.llm.thinking:
        say(WARN, "思考模式开着（烧钱）", "config.yaml 里设 llm.thinking: false。\n"
                                         "陪聊场景这一项能省掉一半以上的钱。")
    else:
        say(OK, "思考模式已关（最大的省钱开关）")
    return cfg


def check_port(cfg: Config) -> None:
    section("接入端口")
    host, port = netprobe.describe_bind(cfg)

    if cfg.onebot.mode == "reverse":
        say(OK, f"方式：反向 WebSocket —— 程序监听 {cfg.onebot.host}:{cfg.onebot.port}，"
                f"NapCat 连过来")
        who = netprobe.port_owner(int(cfg.onebot.port))
        if who:
            pid_file = cfg.db_path().parent / "companion.pid"
            mine = None
            try:
                mine = int(pid_file.read_text().strip()) if pid_file.exists() else None
            except Exception:
                mine = None
            if mine and mine == who:
                say(OK, f"端口 {cfg.onebot.port} 是我们自己在听（PID {who}），程序已经在跑了")
            else:
                say(WARN, f"端口 {cfg.onebot.port} 被 PID {who} 占着",
                    f"如果那是上一次没退干净的 main.py：taskkill /PID {who} /F\n"
                    f"要是别的程序：改 config.yaml 里 onebot.port。")
        else:
            say(OK, f"端口 {cfg.onebot.port} 空着（程序启动时才能开始听）")
        say(OK, f"NapCat 要连的地址：ws://127.0.0.1:{cfg.onebot.port}{cfg.onebot.path}",
            "这一项通常不用手填 —— NapCat 目录里 config/onebot11.json 已经写好了"
            "（下一节会核对）。\n"
            "真要手动配：NapCat → 网络配置 → 新建 → 类型选「WebSocket 反向代理」，"
            "消息格式选 array（不是 string），勾上「心跳」。")
    else:
        say(OK, f"方式：正向 WebSocket —— 程序主动连 {cfg.onebot.url}")
        if netprobe.tcp_open(host, port, timeout=1.5):
            say(OK, f"{host}:{port} 通，NapCat 应该在跑")
        else:
            say(BAD, f"{host}:{port} 连不上",
                "正向模式下必须先开 NapCat。\n"
                "NapCat → 网络配置 → 新建 → 类型选「WebSocket 服务器」，端口填 "
                f"{port}，消息格式选 array。")


async def probe_ws(cfg: Config) -> None:
    if cfg.onebot.mode != "forward":
        return
    import websockets

    section("NapCat 握手")
    try:
        async with websockets.connect(cfg.onebot.url, open_timeout=8) as ws:
            say(OK, f"握手成功：{cfg.onebot.url}")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                say(OK, f"收到一条事件：{str(raw)[:110]}")
            except asyncio.TimeoutError:
                say(WARN, "握手成功但 5 秒内没有事件",
                    "正常 —— 没人说话时 NapCat 就是安静的。发条消息试试。")
    except Exception as e:
        say(BAD, f"握手失败：{type(e).__name__}: {e}",
            "对着上一节的说明检查 NapCat 的网络配置（类型、端口、消息格式）。")


def check_napcat(cfg) -> None:
    section("NapCat")
    hits = netprobe.find_napcat()
    if not hits:
        say(WARN, "没在常见位置找到 NapCat",
            "如果还没装：https://github.com/NapNeko/NapCatQQ/releases\n"
            "选 NapCat.Shell.zip（Windows 直接解压，不用 Docker），"
            "解压出来双击 launcher.bat。\n"
            "装好之后回来重跑 python doctor.py。")
        return

    for p in hits[:4]:
        kind = "QQNT 本体" if netprobe.looks_like_qqnt(p) else (
            "看起来是 NapCat" if netprobe.has_marker(p) else "名字像 NapCat")
        extra = ""
        if netprobe.looks_like_qqnt(p):
            v = netprobe.qq_version(p)
            extra = f"，版本 {v}" if v else ""
        say(OK, f"{kind}：{p}{extra}")

    nc_dirs = [r for r in (netprobe.napcat_root(p) for p in hits) if r]
    if not nc_dirs:
        say(WARN, "只找到名字像的目录，没看到 NapCat 的招牌文件",
            "确认一下解压出来的目录里有没有 NapCatWinBootMain.exe。")
        return
    nc = nc_dirs[0]
    say(OK, f"找到 NapCat 本体：{nc}")

    # ---- 1. NapCat 的启动脚本找不找得到 QQ ----
    # 这是个真会踩的坑：launcher.bat 只读注册表，QQ 挪过位置（换盘符/重装）
    # 注册表就是旧的，它会直接报 "provided QQ path is invalid" 退出。
    reg = netprobe.registry_qq()
    real = netprobe.find_qq()
    fixed = nc / "start-napcat.bat"
    launcher_qq = netprobe.napcat_launcher_qq()
    if launcher_qq:
        say(OK, f"NapCat 自带的 launcher.bat 能找到 QQ：{launcher_qq}")
    elif fixed.exists():
        qq = real[0][0] if real else "（没找到 QQ.exe）"
        say(OK, "QQ 不在注册表里，但旁边有修好的启动脚本",
            f"启动 NapCat 用 {fixed.name}，别用 launcher.bat。\n"
            f"注册表现在指向 {reg or '（没有这个键）'}，那个文件不存在；\n"
            f"QQ 实际在 {qq}，start-napcat.bat 会用它。\n"
            "（NapCat 自带的 launcher.bat 只认注册表，会直接报 "
            "\"provided QQ path is invalid\" 然后退出。）")
    elif real:
        say(WARN, "NapCat 自带的 launcher.bat 会找不到 QQ",
            f"注册表指向 {reg or '（没有这个键）'}，但那个文件不存在；"
            f"QQ 实际在 {real[0][0]}。\n"
            "两个办法：① 装个 start-napcat.bat 放到 NapCat 目录里；"
            "② 设环境变量 NAPCAT_QQ_PATH 指向 QQ.exe。")
    else:
        say(BAD, "没找到 QQ.exe",
            "NapCat 是挂在 QQ 上的，得先装新版 QQ（QQNT）。")

    # ---- 2. 反向 WS 配好了没（决定 NapCat 会不会主动连过来）----
    confs = netprobe.onebot_config_files(nc)
    targets = []
    for p in confs:
        targets.extend(netprobe.reverse_ws_targets(p))
    reverse = (cfg is None) or (cfg.onebot.mode == "reverse")
    if not confs:
        say(WARN, "NapCat 里还没有 OneBot11 配置文件",
            f"第一次启动 NapCat 之后，建一个 {nc / 'config' / 'onebot11.json'}，\n"
            "或者启动后用 WebUI 配（默认 http://127.0.0.1:6099/webui ）。\n"
            "反向 WS 的话：类型选「WebSocket 客户端 / 反向代理」，"
            "地址填本项目那一节的 ws:// 地址。")
    elif reverse:
        port = str(cfg.onebot.port) if cfg else "3001"
        if any(port in t for t in targets):
            say(OK, f"NapCat 已经配好反向 WS：{'、'.join(targets)}")
        elif targets:
            say(WARN, "NapCat 的反向 WS 指向别处",
                f"现在配的启用地址是 {'、'.join(targets)}，"
                f"但本项目在 {cfg.onebot.path} 上监听 {port}。二选一：改 NapCat，或改 config.yaml。")
        else:
            say(WARN, "NapCat 的配置里没有启用的反向 WS",
                "结果是它不会主动连过来。在 WebUI 里新建一个"
                "「WebSocket 客户端」，勾上「保存时启用」。")

    # ---- 3. 在不在跑 ----
    running = netprobe.proc_running("QQ.exe")
    if cfg is not None and cfg.onebot.mode == "reverse":
        # 反向模式下本项目是服务端，NapCat 在跑的时候才会连过来，
        # 所以「端口没人听」不代表出问题，不能像正向那样判。
        if running:
            say(OK, "QQ.exe 在跑，NapCat 应该已经挂上去了")
        else:
            say(WARN, "QQ.exe 没在跑",
                f"用 {fixed.name if fixed.exists() else 'launcher.bat'} 启动 NapCat，"
                "扫二维码登那个小号。\n"
                "反向模式下本项目是服务端，NapCat 起来了才会连过来。")
    elif netprobe.tcp_open("127.0.0.1", 3000, timeout=1.0):
        say(OK, "NapCat 的默认端口 3000 在听，它应该正在运行")
    else:
        say(WARN, "NapCat 的默认端口 3000 没人听",
            "NapCat 没在运行，或者它的 WS 服务器配置了别的端口。\n"
            f"先把 NapCat 开起来（{fixed.name if fixed.exists() else 'launcher.bat'}），"
            "确认登上了那个小号。")


def check_db(cfg: Config) -> None:
    section("数据")
    db = cfg.db_path()
    if not db.exists():
        say(OK, f"数据库还没建（{db}），第一次启动时创建")
    else:
        try:
            st = Store(db)
            try:
                n = int(st.query("SELECT COUNT(*) AS n FROM messages")[0]["n"] or 0)
                mem = int(st.query("SELECT COUNT(*) AS n FROM facts")[0]["n"] or 0)
                bound = st.query("SELECT uid, nickname FROM allowlist")
            finally:
                st.close()
            say(OK, f"数据库能打开：{n} 条消息，{mem} 条长期记忆，"
                    f"{(db.stat().st_size / 1024):.0f} KB")
            if bound:
                who = ", ".join(f"{r['nickname'] or '?'}({r['uid']})" for r in bound)
                say(OK, f"已绑定的人：{who}")
            else:
                say(WARN, "还没有人绑定",
                    "启动程序后，拿你的主号给这个小号发任意一条消息，\n"
                    "第一个私聊它的人会被自动认成主人，之后只有你能聊。")
        except Exception as e:
            say(BAD, f"数据库打不开：{e}", "删掉 data/companion.db 重来（会丢掉聊天记录）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="qq-companion 体检")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    print("\n" + "=" * 50)
    print("  qq-companion 体检")
    print("=" * 50)

    check_python()
    check_deps()
    cfg = check_config(args.config)
    if cfg:
        check_port(cfg)
        try:
            asyncio.run(probe_ws(cfg))
        except RuntimeError:
            pass
        check_db(cfg)
    check_napcat(cfg)

    print("\n" + "=" * 50)
    ok, warn, bad = _score["ok"], _score["warn"], _score["bad"]
    print(f"  通过 {ok} 项，提醒 {warn} 项，问题 {bad} 项")
    if bad:
        print("  先解决标 ✗ 的。")
    elif warn:
        print("  标 ! 的不一定有问题，看一眼就好。")
    else:
        print("  全绿。可以 python main.py 启动了。")
    print("=" * 50 + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
