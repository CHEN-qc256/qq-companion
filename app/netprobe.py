"""探针：NapCat 装没装、在不在跑、端口通不通、谁占着端口。

只做只读探测 —— 不启动、不安装、不改任何东西。setup.py / doctor.py / main.py --check 共用。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# NapCat / QQNT 可能出现的地方。只扫一层两层，不做全盘遍历（那要几十秒）。
_HOME = Path.home()
_ROOTS: List[Path] = [
    _HOME, _HOME / "Desktop", _HOME / "Downloads", _HOME / "Documents",
    _HOME / "OneDrive" / "Desktop", _HOME / "OneDrive" / "桌面",
    Path("C:/"), Path("D:/"), Path("E:/"),
]
_QQNT_DIRS: List[Path] = [
    Path("C:/Program Files/Tencent/QQNT"),
    Path("C:/Program Files (x86)/Tencent/QQNT"),
    _HOME / "AppData/Local/Programs/Tencent/QQNT",
]
# NapCat 的招牌文件。命中任意一个就算找到安装目录。
_MARKERS = ("NapCatWinBootMain.exe", "NapCat.Shell", "napcat.mjs",
            "NapCatWinBootHook.dll", "NapCatQQ", "launcher.bat")


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """目标端口有没有人在听。"""
    try:
        with socket.create_connection((_local(host), int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _local(host: str) -> str:
    """0.0.0.0 / :: 这类「监听所有网卡」的地址，连的时候要换成本机回环。"""
    h = (host or "").strip()
    if h in ("", "0.0.0.0", "::", "[::]", "*"):
        return "127.0.0.1"
    return h


def port_owner(port: int) -> Optional[int]:
    """谁在监听这个端口。拿不到就返回 None（非 Windows 或 netstat 被挡）。"""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=15, encoding="utf-8", errors="replace").stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "listening" not in line.lower():
            continue
        # 本地地址可能是 0.0.0.0:3001 / [::]:3001 / 127.0.0.1:3001
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return None


def _pid_alive_win(pid: int) -> bool:
    """Windows：用 OpenProcess + GetExitCodeProcess 判断进程在不在。

    ★ 为什么不用 `os.kill(pid, 0)`：在 Windows 上 os.kill 对非 CTRL 信号会直接走
      TerminateProcess —— 那是**真的把进程杀掉**，不是「探测」。绝对不能用。

    ★ 为什么不用 `tasklist /FI "PID eq N"`：早先版本就是这么写的，结果在
      「管理面板自己拉起的子进程」这个场景下会偶发判活失败（子进程明明在跑、
      `Get-Process` 也看得到，这里却返回 False），而同一个 PID 从前台 shell 调
      又是 True。子进程 + 管道 + PATH + 输出编码，四层都可能出问题，
      换成 Win32 API 之后完全没有这些变量。
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # 打不开：进程不存在，或者被保护（后面这种极少见，当成不在更安全）
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """进程还在不在。绝不发信号。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            return _pid_alive_win(pid)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def probe_pid(pid: int) -> dict:
    """诊断用：把「这个 PID 到底还在不在」的每一步原始结果都摊出来。

    正常路径用不到它。面板上看到「显示未运行、但端口明明是通的」这种矛盾时，
    调它就能一眼看出是哪一层（权限 / subprocess / 编码）出的问题。
    """
    info: dict = {
        "pid": pid, "platform": sys.platform,
        "executable": sys.executable, "cwd": os.getcwd(),
    }
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            info["posix_kill"] = "alive"
        except Exception as e:
            info["posix_kill"] = repr(e)
        info["verdict"] = pid_alive(pid)
        return info

    import ctypes
    from ctypes import wintypes

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        ctypes.set_last_error(0)
        h = k32.OpenProcess(0x1000, False, int(pid))
        info["OpenProcess"] = int(h) if h else 0
        info["OpenProcess_lastError"] = ctypes.get_last_error()
        if h:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            info["GetExitCodeProcess_ok"] = bool(ok)
            info["exit_code"] = code.value
            k32.CloseHandle(h)
    except Exception as e:
        info["ctypes_error"] = repr(e)

    info["verdict"] = pid_alive(pid)
    return info


def find_napcat(limit: int = 6) -> List[Path]:
    """找 NapCat 的安装目录。找不到返回空列表。"""
    hits: List[Path] = []
    seen: set[str] = set()

    def take(p: Path) -> None:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            hits.append(p)

    for root in _ROOTS:
        if len(hits) >= limit or not root.exists():
            continue
        for depth in ("*NapCat*", "*napcat*", "*/*NapCat*", "*/*napcat*"):
            if len(hits) >= limit:
                break
            try:
                for p in root.glob(depth):
                    if p.is_dir() or p.name in _MARKERS:
                        take(p)
            except (OSError, PermissionError):
                continue

    # QQNT 本体目录：NapCat 是装在它上面的，看到这个说明 QQ 版本得对得上
    for d in _QQNT_DIRS:
        if len(hits) >= limit:
            break
        if d.exists():
            take(d)
    return hits


def has_marker(path: Path) -> bool:
    """这个目录里有没有 NapCat 的招牌文件。"""
    if path.is_file():
        return path.name in _MARKERS
    for m in _MARKERS:
        try:
            if (path / m).exists():
                return True
        except OSError:
            continue
    return False


def napcat_root(path: Path) -> Optional[Path]:
    """从「名字像 NapCat」的那一层往下找到真正装着 NapCat 的目录。

    为什么需要：find_napcat() 的 glob 只到两层，外面那层往往只是你自己建的
    文件夹（比如 E:\\...\\NapCatQQ），里面才是解压出来的 NapCat.Shell。
    判断依据是 NapCatWinBootMain.exe 这个一定在根上的文件。
    """
    p = Path(path)
    if p.is_file():
        p = p.parent
    try:
        if (p / "NapCatWinBootMain.exe").is_file():
            return p
        for child in sorted(p.iterdir()):
            if child.is_dir() and (child / "NapCatWinBootMain.exe").is_file():
                return child
    except OSError:
        return None
    return None


def looks_like_qqnt(path: Path) -> bool:
    return path.is_dir() and path.name.lower() == "qqnt"


def qq_version(path: Path) -> str:
    """尽力读一下 QQNT 的版本号（NapCat 对版本很挑）。读不到就返回空串。"""
    for rel in ("versions", "resources/app/package.json"):
        p = path / rel
        try:
            if p.is_file():
                import json
                data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                v = data.get("version") or data.get("qqVersion")
                if v:
                    return str(v)
            if p.is_dir():
                subs = sorted((x.name for x in p.iterdir() if x.is_dir()), reverse=True)
                if subs:
                    return subs[0]
        except Exception:
            continue
    return ""


def proc_running(name: str) -> bool:
    """有没有这个进程在跑。NapCat 是注入进 QQ 的，QQ.exe 在跑就说明至少 QQ 起来了。"""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        ).stdout or ""
    except Exception:
        return False
    return name.lower() in out.lower()


# NapCat 的 launcher.bat 只认注册表这一个地方。QQ 换过盘符/挪过目录之后
# 注册表经常是旧的，于是 launcher 直接报 "provided QQ path is invalid" 退出。
_QQ_REG = (r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows"
           r"\CurrentVersion\Uninstall\QQ")

# 常见安装位置，找 QQ.exe 用（也是 NapCat 目录里那个 start-napcat.bat 的候选表）
_QQ_EXE_CANDIDATES: List[str] = [
    r"C:\Program Files\Tencent\QQNT\QQ.exe",
    r"E:\QQ\QQ.exe",
    r"C:\QQ\QQ.exe",
    r"C:\Program Files\Tencent\QQNT\QQ.exe",
    r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
    str(_HOME / "AppData/Local/Programs/QQ/QQ.exe"),
]


def registry_qq() -> Optional[str]:
    """注册表里 NapCat launcher 会推出来的 QQ.exe 路径（不管存不存在）。"""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["reg", "query", _QQ_REG, "/v", "UninstallString"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        ).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        if "UninstallString" not in line:
            continue
        # 形如:     UninstallString    REG_SZ    "E:\QQ\Uninstall.exe"
        parts = line.split("REG_SZ", 1)
        if len(parts) != 2:
            continue
        val = parts[1].strip().strip('"')      # 值里通常带一对引号，要剥掉
        if not val:
            continue
        return str(Path(val).parent / "QQ.exe")
    return None


def find_qq() -> List[Tuple[str, str]]:
    """能用的 QQ.exe，返回 [(路径, 来源)]。来源用来在体检里解释「为什么它能找到」。"""
    hits: List[Tuple[str, str]] = []
    env = os.environ.get("NAPCAT_QQ_PATH", "").strip()
    if env and Path(env).is_file():
        hits.append((env, "环境变量 NAPCAT_QQ_PATH"))
    for c in _QQ_EXE_CANDIDATES:
        if Path(c).is_file():
            hits.append((c, "常见安装位置"))
    reg = registry_qq()
    if reg and Path(reg).is_file():
        hits.append((reg, "注册表"))
    return hits


def napcat_launcher_qq() -> Optional[str]:
    """NapCat 自带的 launcher.bat 能找到哪个 QQ（找不到就是它那个已知的坑）。"""
    reg = registry_qq()
    return reg if (reg and Path(reg).is_file()) else None


def onebot_config_files(napcat_dir: Path) -> List[Path]:
    """NapCat 的 OneBot11 配置文件：config/onebot11.json（对所有账号生效）
    和 config/onebot11_<QQ号>.json（只对那个账号生效）。"""
    cfg_dir = Path(napcat_dir) / "config"
    if not cfg_dir.is_dir():
        return []
    out: List[Path] = []
    try:
        for p in cfg_dir.glob("onebot11*.json"):
            if p.is_file():
                out.append(p)
    except OSError:
        pass
    return out


def reverse_ws_targets(path: Path) -> List[str]:
    """这个 OneBot11 配置里启用了哪些「反向 WS（websocketClients）」地址。

    用来回答一个很具体的问题：NapCat 到底会不会主动连到本项目这儿来。
    """
    import json
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    net = (data or {}).get("network") or {}
    out: List[str] = []
    for c in (net.get("websocketClients") or []):
        if isinstance(c, dict) and c.get("enable"):
            url = str(c.get("url") or "").strip()
            if url:
                out.append(url)
    return out


def describe_bind(cfg) -> Tuple[str, int]:
    """当前接入方式下，真正该去连/该被连的 host 和 port。"""
    if cfg.onebot.mode == "reverse":
        return _local(cfg.onebot.host), int(cfg.onebot.port)
    url = cfg.onebot.url or ""
    tail = url.rsplit("/", 1)[0] if url.count("/") > 2 else url
    hostport = tail.rsplit("/", 1)[-1].replace("ws://", "").replace("wss://", "")
    host, _, port = hostport.partition(":")
    return host or "127.0.0.1", int(port or 3000)
