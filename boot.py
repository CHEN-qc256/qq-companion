"""开机自启的引导器 —— 登录后由注册表调起，**自己不开任何窗口**。

它只做一件事：按固定顺序把这一套程序拉起来，每一步的结果都写进
`data/boot.log` 和 `data/boot-state.json`，失败不影响后面的步骤。

    顺序                          为什么是这个顺序
    ─────────────────────────────────────────────────────────────
    1. 等 boot.delay 秒           登录后桌面、网络、代理设置还没就位，
                                 抢跑会连不上、也会把日志写成一堆假错误
    2. GPT-SoVITS（可选）         最慢的一个（加载模型约 60 秒），先点火，
                                 它跟别的东西没有依赖，纯粹是「越早越早能说话」
    3. 机器人本体                 reverse 模式下它是**服务端**，必须先占住 3001，
                                 否则 NapCat 连过来会扑空
    4. 等机器人端口在听           最多 25 秒。没起来也继续走 —— NapCat 会自己重连
    5. NapCat（可选）             它才是真正登录 QQ 的那个；晚几秒无所谓。
                                 注入 QQ 需要管理员权限，所以走「最高权限的
                                 计划任务」——用 schtasks /run 触发，不弹 UAC
    6. 管理面板                   给人看的，最后开。开完顺手把浏览器打开，
                                 这是全隐藏模式下唯一「看得见」的东西
    7. 收尾核对                   机器人进程在不在、NapCat 连没连上，写进
                                 boot-state.json，下次出问题一眼能看到原因

用法：
    python boot.py --install     写注册表（开机自启）+ 把 boot.enabled 记成 true
    python boot.py --uninstall   删注册表
    python boot.py --status      看现在开没开、上次自启的结果
    python boot.py --now         立刻按同样的顺序跑一遍（前台，能看到输出）
    python boot.py --auto        注册表用的那一条（无窗口，日志进 data/boot.log）
    python boot.py --auto --show 同上，但每个子进程都给它自己的窗口（排查用）

★ 自启是「登录时」触发，不是「开机时」。NapCat 和 QQ 都是用户态程序，做成
  系统服务会跑在 Session 0（没有桌面、没有用户环境），QQ 根本登不上。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import autostart, netprobe  # noqa: E402
from app.config import Config, load_config  # noqa: E402

PANEL_PORT = 8899          # 和 panel/server.py 的 DEFAULT_PORT 一致
BOT_READY_TIMEOUT = 25.0   # 等机器人把监听端口绑上，最多等这么久
POLL = 0.5


# ──────────────────────────────────────────────────────────────────────
#  日志
# ──────────────────────────────────────────────────────────────────────
class Log:
    def __init__(self, path: Path):
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 超过 2 MB 就重开一个，免得常年自启把盘写满
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                path.replace(path.with_suffix(".log.1"))
        except Exception:
            pass
        self.console = sys.stdout

    def __call__(self, msg: str, *, level: str = "·") -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level} {msg}"
        if self.console is not None:
            try:
                print(line, flush=True)
            except Exception:
                pass
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _echo(msg: str = "") -> None:
    """往控制台打一行。pythonw 下 sys.stdout 是 None，print 会直接抛 —— 所以要先看一眼。"""
    if sys.stdout is None:
        return
    try:
        print(msg, flush=True)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
#  单实例锁
# ──────────────────────────────────────────────────────────────────────
class Lock:
    """同一个数据目录只允许一个引导器在跑。

    场景：注册表调起了一次，用户又手动点了「试跑一次」——两个引导器会各起
    一个 main.py，第二个撞端口退出，日志里一片红，看着像坏了。
    """

    def __init__(self, path: Path):
        self.path = path
        self.taken = False

    def acquire(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
            if pid and pid != os.getpid() and netprobe.pid_alive(pid):
                return False
        except Exception:
            pass
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        self.taken = True
        return True

    def release(self) -> None:
        if not self.taken:
            return
        try:
            if int(self.path.read_text(encoding="utf-8").strip()) == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
#  每一步
# ──────────────────────────────────────────────────────────────────────
def _result(name: str, *, ok: bool = False, pid: int = 0,
            msg: str = "", skipped: bool = False) -> Dict[str, Any]:
    return {"name": name, "ok": ok, "pid": pid, "msg": msg, "skipped": skipped}


def _port_of(api_base: str, fallback: int = 9880) -> int:
    try:
        return int(urlparse(api_base).port or fallback)
    except Exception:
        return fallback


def _spawn_alive(pid: int, log_path: Path, seconds: float = 8.0) -> Optional[str]:
    """盯几秒，看刚拉起的进程是不是秒退。

    活过这几秒 → 返回 None（模型还在加载，后面没崩就行）；
    立刻死掉 → 返回一句「看哪个日志 + 最后一行」，让 boot 打出 ✗ 而不是 ✓。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not netprobe.pid_alive(pid):
            tail = ""
            try:
                lines = [ln.strip() for ln in log_path.read_text(
                    encoding="utf-8", errors="replace").splitlines() if ln.strip()]
                tail = lines[-1] if lines else ""
            except OSError:
                pass
            return ("启动后立刻退出了，看 data\\" + str(log_path.name) +
                    (f"：{tail[:160]}" if tail else ""))
        time.sleep(0.4)
    return None


def step_tts(cfg: Config, log: Log, *, hide: bool) -> Dict[str, Any]:
    """GPT-SoVITS。已经在监听就跳过 —— 模型加载 60 秒，重复拉起会抢显存。"""
    name = "语音服务"
    if not cfg.boot.tts:
        return _result(name, skipped=True, msg="boot.tts 关着")
    install = Path(cfg.tts.install_dir)
    port = _port_of(cfg.tts.api_base)
    if netprobe.tcp_open("127.0.0.1", port, 0.4):
        return _result(name, ok=True, msg=f"已经在监听 {port}", skipped=True)

    from panel import voice          # 复用「整合包目录怎么找」的那套逻辑
    app_dir = voice.detect_app_dir(install)
    data_dir = cfg.db_path().parent
    if app_dir is not None:
        rt = app_dir / "runtime" / "python.exe"
        if not rt.is_file():
            rt = app_dir / "runtime" / "python"
        if not rt.exists():
            return _result(name, msg=f"{app_dir} 里没有 runtime\\python.exe")
        env = os.environ.copy()
        # HTTP_PROXY 常常是全局的，api_v2 自己绑本地也要绕开，
        # 否则它内部请求 127.0.0.1 会走代理然后 502。
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        env["PYTHONUNBUFFERED"] = "1"      # 否则「加载中」那两分钟日志是空的
        cmd = [str(rt), "api_v2.py", "-a", "127.0.0.1", "-p", str(port),
               "-c", "GPT_SoVITS/configs/tts_infer.yaml"]
        cwd = app_dir
    else:
        # 找不到整合包目录就退回官方启动脚本（它会自己 cd 到正确位置）
        bat = voice.start_bat(install)
        if not bat.is_file():
            return _result(name, msg=f"找不到 {install}\\app 或 start_api.bat")
        cmd = ["cmd.exe", "/c", str(bat)]
        cwd = install

    log_path = data_dir / "boot-tts.log"
    pid = autostart.spawn(cmd, cwd=cwd, log_path=log_path, hide=hide)
    # 只等到「进程还活着」为止：模型要加载一分钟，不能干等；但秒退必须抓到，
    # 否则 boot 会打 ✓，人以为语音在，其实早就崩了 —— 2026-10-20 08:29 那次
    # 语音「自启成功」却没有 9880，骗了整整半天。
    bad = _spawn_alive(pid, log_path)
    if bad:
        return _result(name, pid=pid, msg=bad)
    return _result(name, ok=True, pid=pid,
                   msg=f"已拉起（PID {pid}），模型加载约 1 分钟")


def step_bot(cfg: Config, cfg_path: Path, log: Log, *, hide: bool) -> Dict[str, Any]:
    name = "机器人"
    from panel import runner
    st = runner.status(cfg, cfg_path)
    if st["running"]:
        return _result(name, ok=True, msg=f"已经在跑（PID {st['pid']}）", skipped=True)

    # pidfile 里留着的是上次没退干净的号码，main.py 会以为有实例在跑
    try:
        pf = Path(st["pidFile"]) if st.get("pidFile") else None
        if pf and pf.exists():
            pf.unlink(missing_ok=True)
            log(f"清掉残留 pidfile：{pf}")
    except Exception:
        pass

    pyw = autostart.pythonw()
    cmd = [str(pyw), str(ROOT / "main.py"), "--config", str(cfg_path)]
    pid = autostart.spawn(cmd, cwd=ROOT, log_path=cfg.db_path().parent / "boot-bot.out",
                          hide=hide)

    # 等它把 pidfile 写出来 / 端口绑上。这一步只影响日志好看，
    # 失败了也继续往下走 —— NapCat 会自己重试连接。
    if str(cfg.onebot.mode).lower() == "reverse":
        host = "127.0.0.1" if str(cfg.onebot.host) in ("0.0.0.0", "::", "*", "") else cfg.onebot.host
        deadline = time.time() + BOT_READY_TIMEOUT
        while time.time() < deadline:
            if netprobe.tcp_open(host, int(cfg.onebot.port), 0.3):
                return _result(name, ok=True, pid=pid,
                               msg=f"已启动（PID {pid}），正在监听 {cfg.onebot.port}")
            if not netprobe.pid_alive(pid):
                return _result(name, msg=f"起来了又退出了（PID {pid}），看 data/boot-bot.out")
            time.sleep(POLL)
        return _result(name, ok=True, pid=pid,
                       msg=f"进程在（PID {pid}），但 {BOT_READY_TIMEOUT:.0f} 秒内没监听上端口")
    return _result(name, ok=True, pid=pid, msg=f"已启动（PID {pid}），主动去连 NapCat")


def _image_running(image: str) -> bool:
    """按进程名看它是不是已经在了。拿不到就当没在跑（宁可多起一次也别漏）。"""
    if os.name != "nt" or not image:
        return False
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("gbk", "replace")
    except Exception:
        return False
    return image.lower() in out.lower()


def _wait_qq(seconds: float) -> bool:
    """等 QQ.exe 出现。注入成功后 QQ 会以 4~5 个进程的形态起来，所以只看进程名。"""
    from app import napcat
    deadline = time.time() + seconds
    while time.time() < deadline:
        if napcat.qq_running():
            return True
        time.sleep(POLL)
    return napcat.qq_running()


def _wait_napcat_connected(cfg: Config, seconds: float = 20.0) -> Optional[bool]:
    """等 NapCat 连上来，别在机器人刚起来那一秒就下结论。

    09:19 那次开机自启：机器人在 09:19:18 拉起，step_napcat 09:19:19 就查
    日志 —— 那时 NapCat 还没建链（09:19:23 才连上），于是 boot-state 里
    写着「× NapCat 还没登录成功」，用户看到的就是误报。所以先看 3001 是否
    在听（机器人在跑），在听就最多等 20 秒；机器人根本没起来就别白等。
    """
    from app import napcat

    if not netprobe.tcp_open("127.0.0.1", int(cfg.onebot.port), 0.3):
        return napcat.connection_state(cfg.log_path())
    deadline = time.time() + seconds
    conn: Optional[bool] = None
    while time.time() < deadline:
        conn = napcat.connection_state(cfg.log_path())
        if conn is True:
            return True
        time.sleep(POLL)
    return conn


def step_napcat(cfg: Config, log: Log, *, hide: bool) -> Dict[str, Any]:
    """NapCat。

    三条路，按「零打扰」到「要你动手」排：

    1. `boot.napcat_path` 填了某个脚本/exe → 照它启动（用户说了算）
    2. 装过提权计划任务 → `schtasks /run`，**不弹 UAC**（推荐，也是默认路径）
    3. 都没有 → 直接拉一次。普通权限下注入 QQ 会被拒（Error Code: 5），
       这时如实报错并告诉用户跑一次 install_napcat_task.bat
    """
    from app import napcat

    name = "NapCat"
    if not cfg.boot.napcat:
        return _result(name, skipped=True, msg="boot.napcat 关着")

    if napcat.qq_running():
        conn = napcat.connection_state(cfg.log_path())
        if conn is False:
            # 刚起的机器人还没等到 NapCat 建链，等一会儿再说，免得误报。
            conn = _wait_napcat_connected(cfg)
        if conn is False:
            return _result(name, msg="QQ 进程在，但 NapCat 还没登录成功（登录窗可能正等你扫码）")
        return _result(name, ok=True, skipped=True, msg="QQ 已经在跑")

    data_dir = cfg.db_path().parent
    shell = napcat.find_shell()
    qq = napcat.find_qq()
    uin = napcat.quick_uin(shell, cfg.boot.napcat_qq)
    login = f"快速登录 {uin}" if uin else "需要扫码"

    raw = (cfg.boot.napcat_path or "").strip()
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            return _result(name, msg=f"找不到 {path}")
        if path.suffix.lower() == ".exe" and _image_running(path.name):
            return _result(name, ok=True, skipped=True, msg=f"{path.name} 已经在跑")
        cmd = ["cmd.exe", "/c", str(path)] if path.suffix.lower() in (".bat", ".cmd") else [str(path)]
        pid = autostart.spawn(cmd, cwd=path.parent,
                              log_path=data_dir / "boot-napcat.log", hide=hide)
        note = "（隐藏窗口，扫码请看 data/boot-napcat.log 或用 --show 跑一次）" if hide else ""
        return _result(name, ok=True, pid=pid, msg=f"已拉起（PID {pid}）{note}")

    if shell is None or qq is None:
        missing = "NapCat.Shell" if shell is None else "QQ.exe"
        return _result(name, msg=f"找不到 {missing}，这一项跳过")

    # 2) 提权计划任务：这是唯一「有管理权限又不弹 UAC」的路径。
    #    任务自带登录触发器（logon+15s），所以就算这里手动触发失败，
    #    Windows 也会在几秒后自己把它叫起来 —— 多等一会儿就行。
    if napcat.task_exists():
        ok, msg = napcat.run_task()
        if ok and _wait_qq(15):
            return _result(name, ok=True, msg=f"已通过计划任务拉起（{login}）")
        if not ok:
            log(f"没能手动触发计划任务（{msg}），等 Windows 自己触发", level="!")
        if _wait_qq(35):
            return _result(name, ok=True, msg=f"已由计划任务拉起（{login}）")
        return _result(name, msg="计划任务装了，但 35 秒内没看到 QQ 起来（下次登录它仍会自动触发）")

    # 3) 没装计划任务 —— 直接跑 start-napcat.bat。
    #    普通权限下 bat 会自己 `-Verb RunAs` 提权（弹一个 UAC），起来之后照常可用。
    #    窗口守护交给后台的 `-m app.napcat guard`，不占着引导器的顺序流程。
    try:
        pid = napcat.spawn_via_bat(shell, uin, log_path=data_dir / "boot-napcat.log",
                                   hide=hide)
    except Exception as e:
        return _result(name, msg=f"拉不起来：{e}")
    if _wait_qq(20):
        napcat.spawn_guard(data_dir / "boot-napcat-guard.log", timeout=180)
        return _result(name, ok=True, pid=pid,
                       msg=f"已通过 {napcat.START_BAT} 拉起（{login}）")
    return _result(name, pid=pid,
                   msg="拉不起来：注入 QQ 需要管理员权限，请双击 install_napcat_task.bat 装一次提权任务")


def _open_url(url: str) -> bool:
    try:
        if os.name == "nt":
            os.startfile(url)          # noqa: S606  —— 就是「用默认浏览器打开」
        else:
            import webbrowser
            webbrowser.open(url)
        return True
    except Exception:
        return False


def _bilinote_healthy(url: str, timeout: float = 2.0) -> bool:
    """探一下 BiliNote 的 /api/sys_health，返回 code==0。"""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/sys_health",
                                    timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
        return isinstance(data, dict) and int(data.get("code", -1)) == 0
    except Exception:
        return False


def step_bilinote(cfg: Config, log: Log, *, hide: bool) -> Dict[str, Any]:
    """把 BiliNote 后端拉起来。视频推送要用它生成笔记，所以自启时一起带上。"""
    name = "BiliNote（视频笔记）"
    vp = cfg.videopush
    if not cfg.boot.bilinote:
        return _result(name, skipped=True, msg="boot.bilinote 关着")
    if not vp.enabled:
        return _result(name, skipped=True, msg="视频推送关着（videopush.enabled）")
    url = (vp.bilinote_url or "").rstrip("/")
    if not url:
        return _result(name, skipped=True, msg="没配 videopush.bilinote_url")
    if _bilinote_healthy(url):
        return _result(name, ok=True, skipped=True, msg=f"{url} 已经就绪")

    from app.bilinote import python_path, spawn_env

    root = Path(vp.bilinote_dir or "")
    py = python_path(vp)
    if py is None:
        return _result(name, msg=f"{root} 下找不到 .venv 里的 python，videopush.bilinote_dir 指错了")
    if not (root / "main.py").is_file():
        return _result(name, msg=f"{root} 里没有 main.py，videopush.bilinote_dir 指错了")

    pid = autostart.spawn([str(py), "main.py"], cwd=root,
                          log_path=cfg.db_path().parent / "boot-bilinote.log",
                          env=spawn_env(), hide=hide)
    deadline = time.time() + 30
    while time.time() < deadline:
        if _bilinote_healthy(url, timeout=1.0):
            return _result(name, ok=True, pid=pid, msg=f"已启动并就绪（PID {pid}），{url}")
        time.sleep(POLL * 2)
    return _result(name, msg=f"进程起来了（PID {pid}）但 30 秒内没就绪，看 data/boot-bilinote.log")


def step_panel(cfg: Config, log: Log, *, hide: bool) -> Dict[str, Any]:
    name = "管理面板"
    if not cfg.boot.panel:
        return _result(name, skipped=True, msg="boot.panel 关着")
    url = f"http://127.0.0.1:{PANEL_PORT}/"
    if netprobe.tcp_open("127.0.0.1", PANEL_PORT, 0.4):
        opened = _open_url(url) if cfg.boot.open_panel else False
        return _result(name, ok=True, skipped=True,
                       msg=f"{PANEL_PORT} 已经有面板在跑" + ("，已打开浏览器" if opened else ""))
    cmd = [str(autostart.pythonw()), "-m", "panel",
           "--port", str(PANEL_PORT), "--no-browser", "--strict-port"]
    pid = autostart.spawn(cmd, cwd=ROOT,
                          log_path=cfg.db_path().parent / "boot-panel.log", hide=hide)
    opened = False
    if cfg.boot.open_panel:
        # 等页面能连上再开，否则浏览器会显示「无法访问」
        deadline = time.time() + 12
        while time.time() < deadline:
            if netprobe.tcp_open("127.0.0.1", PANEL_PORT, 0.3):
                opened = _open_url(url)
                break
            time.sleep(POLL)
    return _result(name, ok=True, pid=pid,
                   msg=f"已启动（PID {pid}），{url}" + ("，已打开浏览器" if opened else ""))


# ──────────────────────────────────────────────────────────────────────
#  主流程
# ──────────────────────────────────────────────────────────────────────
def state_path(cfg: Config) -> Path:
    return cfg.db_path().parent / "boot-state.json"


def run(cfg_path: Path, *, wait: bool = True, hide: bool = True) -> Dict[str, Any]:
    """按顺序跑一遍。返回那份写进 boot-state.json 的结构。"""
    cfg = load_config(cfg_path)
    data_dir = cfg.db_path().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    log = Log(data_dir / "boot.log")
    started = time.time()

    log(f"===== 引导开始（{cfg_path.name}，{'隐藏窗口' if hide else '带窗口'}）=====")
    if wait and cfg.boot.delay > 0:
        log(f"等 {cfg.boot.delay} 秒，让桌面/网络先就位")
        time.sleep(min(int(cfg.boot.delay), 300))

    steps: List[Dict[str, Any]] = []
    # 每一步都单独 try：语音挂了不该影响机器人起来
    for fn, args in ((step_tts, (cfg, log)),):
        steps.append(_guard(fn, args, log, hide=hide))
    steps.append(_guard(step_bot, (cfg, cfg_path, log), log, hide=hide))
    steps.append(_guard(step_bilinote, (cfg, log), log, hide=hide))
    steps.append(_guard(step_napcat, (cfg, log), log, hide=hide))
    steps.append(_guard(step_panel, (cfg, log), log, hide=hide))

    # 收尾核对：只看「进程起来了」不够 —— 机器人可能起来了却没等到 NapCat。
    # 这一段的结论会写进 boot-state.json，面板和 --status 都读它。
    checks: Dict[str, Any] = {"botRunning": None, "qqConnected": None}
    try:
        from panel import runner as _runner
        checks["botRunning"] = bool(_runner.status(cfg, cfg_path)["running"])
    except Exception:
        pass
    try:
        from app import napcat as _nc
        checks["qqConnected"] = _nc.connection_state(cfg.log_path())
    except Exception:
        pass

    if checks.get("botRunning") is False:
        log("机器人没在跑 —— 看 data/boot-bot.out", level="!")

    state = {
        "at": started,
        "atText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "seconds": round(time.time() - started, 1),
        "config": str(cfg_path),
        "headless": hide,
        "ok": all(s["ok"] or s["skipped"] for s in steps) and checks.get("botRunning") is not False,
        "steps": steps,
        "checks": checks,
    }
    try:
        state_path(cfg).write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    except Exception as e:
        log(f"状态文件写不进去：{e}", level="!")

    for s in steps:
        mark = "✓" if s["ok"] else ("-" if s["skipped"] else "×")
        log(f"  {mark} {s['name']}：{s['msg']}", level="→")
    if checks.get("qqConnected") is False:
        log("机器人起来了，但 NapCat 还没连上来：现在发消息不会有回复", level="!")
    log(f"===== 引导结束（{state['seconds']} 秒）=====")
    return state


def _guard(fn, args: tuple, log: Log, *, hide: bool) -> Dict[str, Any]:
    try:
        return fn(*args, hide=hide)
    except Exception as e:
        import traceback
        log(f"{fn.__name__} 抛异常：{e}", level="!")
        try:
            with open(log.path, "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        return _result(fn.__name__, msg=f"出错：{e}")


def read_state(cfg_path: Path) -> Optional[Dict[str, Any]]:
    try:
        cfg = load_config(cfg_path)
        return autostart.read_state(cfg.db_path().parent)
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
#  命令行
# ──────────────────────────────────────────────────────────────────────
def _print_status(cfg_path: Path) -> int:
    st = autostart.status(ROOT)
    print()
    print("  开机自启（登录时触发）")
    print("  ────────────────────────────────────────────────")
    print(f"  状态      : {'已开启' if st['enabled'] else '未开启'}")
    print(f"  注册表    : {st['runKey']}\\{st['valueName']}")
    print(f"  解释器    : {st['pythonw']}")
    print(f"  引导器    : {st['bootPy']}")
    if st["enabled"]:
        print(f"  登记命令  : {st['command']}")
    if st["stale"]:
        print(f"  ⚠ 登记的路径和当前目录不一致，点一次「开启」会重写成：{st['expected']}")
    for w in st["warn"]:
        print(f"  ⚠ {w}")

    state = read_state(cfg_path)
    print()
    if not state:
        print("  上次自启：没有记录（data/boot-state.json 还不存在）")
    else:
        print(f"  上次自启：{state.get('atText')}　用时 {state.get('seconds')} 秒"
              f"　{'成功' if state.get('ok') else '有失败项'}")
        for s in state.get("steps") or []:
            mark = "✓" if s["ok"] else ("-" if s["skipped"] else "×")
            print(f"    {mark} {s['name']}：{s['msg']}")
        c = state.get("checks") or {}
        if c.get("botRunning") is not None:
            print(f"      机器人进程：{'在跑' if c['botRunning'] else '没在跑'}")
        if c.get("qqConnected") is not None:
            # 这是**上次自启那一刻**的快照，不是现在。单独标一下，
            # 否则「上次没连上、现在连上了」时会把人看糊涂。
            print(f"      那一刻 QQ 连接：{'已连上' if c['qqConnected'] else '还没连上'}")
    print()
    try:
        from app import napcat
        ns = napcat.status(config_path=cfg_path)
    except Exception:
        ns = {}

    # 实时状态永远单独打一行，别让快照顶替
    if ns:
        qq_on = bool(ns.get("qqRunning"))
        live = ns.get("connected")
        print("  现在：")
        print(f"    QQ 进程 : {'在跑' if qq_on else '没在跑'}")
        if live is None:
            print("    NapCat  : 看不出来（机器人日志里还没有连接记录）")
        else:
            print(f"    NapCat  : {'已连接' if live else '未连接'}"
                  f"{'（发消息会有回复）' if live else '（发消息不会有回复）'}")
        if live is False and qq_on:
            print("    提示：QQ 在跑但没连上 —— 多半是没登录，或有两套 QQ 进程抢账号库"
                  "（结束所有 QQ.exe 后重启一次）")
        print()

    try:
        cfg = load_config(cfg_path)
        print("  下次自启会拉起：")
        print(f"    - 等待 {cfg.boot.delay} 秒")
        print(f"    - 语音服务：{'是' if cfg.boot.tts else '否'}")
        print(f"    - 机器人  ：是")
        print(f"    - BiliNote：{'是（视频推送要用）' if cfg.boot.bilinote else '否'}")
        if not cfg.boot.napcat:
            print("    - NapCat  ：否（boot.napcat 关着）")
        elif cfg.boot.napcat_path:
            print(f"    - NapCat  ：{cfg.boot.napcat_path}")
        elif ns.get("task"):
            print(f"    - NapCat  ：计划任务 {ns.get('taskName')}（零 UAC）")
        elif ns.get("shell"):
            print("    - NapCat  ：没装提权任务，登录时会拉不起来（双击 install_napcat_task.bat）")
        else:
            print("    - NapCat  ：没找到 NapCat.Shell")
        print(f"    - 管理面板：{'是' if cfg.boot.panel else '否'}"
              f"{'（自动开浏览器）' if cfg.boot.panel and cfg.boot.open_panel else ''}")
        print(f"    - 窗口    ：{'全部隐藏' if cfg.boot.headless else '每个都给窗口'}")
    except Exception as e:
        print(f"  （读不出 config.yaml：{e}）")
    print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="boot.py", description="qq-companion 开机自启引导器")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="写入开机自启（登录时）")
    g.add_argument("--uninstall", action="store_true", help="取消开机自启")
    g.add_argument("--status", action="store_true", help="查看自启状态与上次结果")
    g.add_argument("--now", action="store_true", help="立刻按顺序拉起（不等待延迟）")
    g.add_argument("--auto", action="store_true", help="注册表调用的入口（不等待/静默）")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"), help="配置文件路径")
    ap.add_argument("--show", action="store_true",
                    help="调试用：每个子进程给一个自己的窗口，不隐藏")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (Path.cwd() / cfg_path).resolve()

    if args.status:
        return _print_status(cfg_path)

    if args.install:
        r = autostart.enable(ROOT)
        _echo(f"✓ 已写入 {r['runKey']}\\{r['valueName']}")
        _echo(f"  命令：{r['command']}")
        _echo("  触发时机：下次**登录 Windows** 时（不是开机时，也不是立刻）。")
        _echo("  想现在验证：python boot.py --now")
        # 同步 config.yaml 里的记录，两处不留矛盾
        _sync_cfg_flag(cfg_path, True)
        return 0

    if args.uninstall:
        r = autostart.disable(ROOT)
        _echo(f"✓ 已删除 {r['runKey']}\\{r['valueName']}")
        _sync_cfg_flag(cfg_path, False)
        return 0

    # --now / --auto
    try:
        cfg = load_config(cfg_path)
        data_dir = cfg.db_path().parent
    except Exception as e:
        _echo(f"× 配置读不出来（{cfg_path}）：{e}")
        return 2

    lock = Lock(data_dir / "boot.lock")
    if not lock.acquire():
        Log(data_dir / "boot.log")("上一次引导还在跑（boot.lock 里的进程还活着），本次跳过", level="!")
        _echo("上一次引导还在跑，跳过。")
        return 0
    try:
        # --auto 才等 boot.delay（模拟真登录）；--now 是「我现在就要看到结果」，不等
        hide = bool(cfg.boot.headless) and not args.show
        state = run(cfg_path, wait=args.auto, hide=hide)
    finally:
        lock.release()
    return 0 if state.get("ok") else 1


def _sync_cfg_flag(cfg_path: Path, on: bool) -> None:
    """把 boot.enabled 写进 config.yaml —— 只是**记录意图**，不是生效机制。

    真正决定会不会自启的只有注册表那一行。这里同步一份是为了面板上看到的
    「参数值」和实际状态不会互相打脸。
    """
    try:
        from panel import configio
        cfg = configio.load(cfg_path)
        if configio.get_path(cfg, "boot.enabled") == on:
            return
        configio.set_path(cfg, "boot.enabled", on)
        configio.save(cfg_path, cfg)
        print(f"  （config.yaml 里的 boot.enabled 已同步为 {'true' if on else 'false'}）")
    except Exception as e:
        print(f"  （config.yaml 没同步成功：{e}）")


if __name__ == "__main__":
    raise SystemExit(main())
