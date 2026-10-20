"""NapCat 的定位、静默拉起，以及「登录时提权」计划任务。

为什么不能跟别的程序一样直接 Popen 拉起来
────────────────────────────────────────────
NapCat 的工作方式是往 QQ 进程里注入 `NapCatWinBootHook.dll`。实测（普通权限）：

    Boot Command:"C:\\Program Files\\Tencent\\QQNT\\QQ.exe" --enable-logging -q 123456789
    Error Code: 5          ← ERROR_ACCESS_DENIED，注入被拒

所以 `start-napcat.bat` 里才有一段 `net session` + `-Verb RunAs` 的自提权。
问题是这样每次登录都会弹一个 UAC 框，跟「无感自启」正好相反。

解法：建一个**计划任务**，把它的运行级别设成「最高」（RunLevel=HighestAvailable），
再让 boot.py 用 `schtasks /run` 触发它。计划任务本来就是为了「以更高权限、
不带 UAC 提示地运行」而存在的 —— 提权只在**创建任务那一次**需要（用户点一次
「是」），之后每次运行都是静默的。

为什么任务动作是 `pythonw.exe -m app.napcat launch` 而不是直接指向
`NapCatWinBootMain.exe`
────────────────────────────────────────────
  1. 注入需要几个 `NAPCAT_*` 环境变量（loader / hook / patch 的路径），计划任务的
     XML 里没有「设置环境变量」这种能力；
  2. 如果动作写成 `cmd.exe /c some.bat`，任务运行时会**闪一个黑框**；
     pythonw 是 GUI 子系统，天然没有控制台；
  3. 由我们自己 spawn，还能把 NapCat 的输出重定向到 `data/boot-napcat.log`，
     下次出问题有东西可看。

一句话：计划任务只负责「给我最高权限」，真正怎么拉起还是这段代码说了算。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

TASK_NAME = "NapCatQQ"
SHELL_DIR_NAME = "NapCat.Shell"

# 注入需要的四个路径，和 start-napcat.bat 里 set 的那四个一一对应。
_ENV_KEYS = {
    "NAPCAT_PATCH_PACKAGE": "qqnt.json",
    "NAPCAT_LOAD_PATH": "loadNapCat.js",
    "NAPCAT_INJECT_PATH": "NapCatWinBootHook.dll",
    "NAPCAT_LAUNCHER_PATH": "NapCatWinBootMain.exe",
    "NAPCAT_MAIN_PATH": "napcat.mjs",
}


# ──────────────────────────────────────────────────────────────────────
#  定位
# ──────────────────────────────────────────────────────────────────────
def find_shell(extra: str = "") -> Optional[Path]:
    """找 NapCat.Shell 目录。

    顺序：显式指定 → 环境变量 → 项目旁边 → 常见盘符根目录。
    只做有限几层的 glob，不做全盘搜索（那会把登录卡住几十秒）。
    """
    cands: List[Path] = []
    if extra:
        cands.append(Path(extra))
    for env_key in ("QQ_COMPANION_NAPCAT", "NAPCAT_SHELL"):
        v = os.environ.get(env_key, "").strip()
        if v:
            cands.append(Path(v))

    bases = [ROOT.parent, Path.home()]
    for drive in ("E:/", "D:/", "C:/"):
        try:
            if Path(drive).exists():
                bases.append(Path(drive))
        except Exception:
            pass
    for base in bases:
        cands.append(base / SHELL_DIR_NAME)
        cands.append(base / "NapCatQQ" / SHELL_DIR_NAME)
        try:
            cands.extend(sorted(base.glob(f"*/{SHELL_DIR_NAME}"))[:6])
            cands.extend(sorted(base.glob(f"*/*/{SHELL_DIR_NAME}"))[:6])
        except Exception:
            pass

    for c in cands:
        try:
            if (c / "NapCatWinBootMain.exe").is_file():
                return c.resolve()
        except Exception:
            continue
    return None


def find_qq(extra: str = "") -> Optional[Path]:
    """找 QQ.exe。注册表兜底 —— 用户把 QQ 挪过盘的情况很常见（这台就是 D:\\QQ）。"""
    cands: List[Path] = []
    if extra:
        cands.append(Path(extra))
    v = os.environ.get("NAPCAT_QQ_PATH", "").strip()
    if v:
        cands.append(Path(v))
    for p in (r"C:\Program Files\Tencent\QQNT\QQ.exe", r"E:\QQ\QQ.exe", r"C:\QQ\QQ.exe",
              r"C:\Program Files\Tencent\QQNT\QQ.exe",
              r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
              str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "QQ" / "QQ.exe")):
        if p:
            cands.append(Path(p))

    if os.name == "nt":
        try:
            import winreg
            key = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        un, _ = winreg.QueryValueEx(k, "UninstallString")
                    exe = Path(str(un).strip().strip('"')).parent / "QQ.exe"
                    cands.append(exe)
                except OSError:
                    continue
        except Exception:
            pass

    for c in cands:
        try:
            if c.is_file():
                return c
        except Exception:
            continue
    return None


def quick_uin(shell: Optional[Path] = None, override: str = "") -> str:
    """快速登录用的 QQ 号。

    优先用配置；没填就从用户的 start-napcat.bat 里读 `NAPCAT_QUICK_QQ=...`。
    这样「哪个号」这件事只有一处真源，不用在两个地方各填一遍。
    """
    s = (override or "").strip()
    if s.isdigit():
        return s
    if shell is None:
        return ""
    try:
        text = (Path(shell) / "start-napcat.bat").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("set \"napcat_quick_qq="):
            val = line.split("=", 1)[1].strip().strip('"')
            if val.isdigit():
                return val
    return ""


# ──────────────────────────────────────────────────────────────────────
#  启动参数
# ──────────────────────────────────────────────────────────────────────
def launch_argv(shell: Path, qq: Path, uin: str = "") -> List[str]:
    """和 start-napcat.bat 最后那行完全一致的参数顺序。"""
    argv = [str(Path(shell) / "NapCatWinBootMain.exe"),
            str(qq),
            str(Path(shell) / "NapCatWinBootHook.dll")]
    if uin:
        argv += ["-q", str(uin)]
    return argv


def launch_env(shell: Path, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(base if base is not None else os.environ)
    for key, name in _ENV_KEYS.items():
        env[key] = str(Path(shell) / name)
    return env


def qq_running() -> bool:
    """QQ.exe 有没有在跑。有就说明已经登录过了，不必再拉一次。"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("gbk", "replace")
    except Exception:
        return False
    return "QQ.exe" in out


def spawn(shell: Path, qq: Path, uin: str, *, log_path: Path, hide: bool = True) -> int:
    """把 NapCat 拉起来（调用方必须已经有权限 —— 否则注入会 Error 5）。"""
    from app import autostart
    return autostart.spawn(
        launch_argv(shell, qq, uin),
        cwd=Path(shell),
        log_path=Path(log_path),
        env=launch_env(shell, dict(os.environ, PYTHONUNBUFFERED="1")),
        hide=hide,
    )


START_BAT = "start-napcat.bat"


def spawn_via_bat(shell: Path, uin: str = "", *, log_path: Path,
                  hide: bool = True) -> int:
    """用 NapCat 自带的 `start-napcat.bat` 拉起。

    为什么绕一层 bat 而不是直接调 NapCatWinBootMain.exe
    ────────────────────────────────────────────────────
    bat 里除了最后那行注入命令，还做了三件我们容易漏掉的事：
      1. 把 `loadNapCat.js` 部署进 QQ 的 `versions\\<ver>\\resources\\app\\`
         （QQ 升级后这个文件会没，不部署就直接 "Cannot find module"）；
      2. 自己找 QQ.exe（候选列表 + 注册表回退），QQ 换盘也不用改代码；
      3. 按 `NAPCAT_QUICK_QQ` 决定 `-q <号>` 还是扫码。
    换句话说，它是 NapCat 官方的启动契约，跟着它走比自己拼参数稳。

    bat 开头有 `net session` 判权：非管理员会 `-Verb RunAs` 自提权并弹 UAC。
    所以**调用方必须已经是管理员**（计划任务就是这么跑的），否则用户会看到
    一个 UAC 框。它末尾那句 `pause` 在 stdin 是 DEVNULL 时会立刻读到 EOF
    继续往下走，不会挂住。
    """
    from app import autostart

    bat = Path(shell) / START_BAT
    if not bat.is_file():
        raise FileNotFoundError(f"找不到 {bat}")
    cmd: List[str] = ["cmd.exe", "/c", str(bat)]
    if str(uin or "").isdigit():
        cmd += ["-q", str(uin)]
    return autostart.spawn(
        cmd,
        cwd=Path(shell),
        log_path=Path(log_path),
        env=launch_env(shell, dict(os.environ, PYTHONUNBUFFERED="1")),
        hide=hide,
    )


# ──────────────────────────────────────────────────────────────────────
#  隐藏 QQ 的窗口
# ──────────────────────────────────────────────────────────────────────
_SW_HIDE = 0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

if os.name == "nt":  # pragma: no cover - 只在 Windows 上执行
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindowAsync.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _image_name(pid: int) -> str:
    """进程的 exe 全路径。拿不到就返回空串（权限不够等）。"""
    if os.name != "nt" or not pid:
        return ""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _is_qq(pid: int, names: Tuple[str, ...]) -> bool:
    exe = _image_name(pid)
    if not exe:
        return False
    base = exe.rsplit("\\", 1)[-1].lower()
    return base in names


# 登录窗/主窗都是 QQ.exe 的；`NapCatWinBootMain.exe` 会带一个标题为 QQ 路径的
# 黑色控制台，也一起收掉。
_QQ_IMAGES = ("qq.exe", "napcatwinbootmain.exe")


def qq_windows(*, visible_only: bool = True) -> List[Dict[str, Any]]:
    """当前所有 QQ 相关的顶层窗口。

    `visible_only=False` 会把已经被藏起来的也列出来 —— 恢复显示时要用。
    """
    if os.name != "nt":
        return []
    found: List[Dict[str, Any]] = []

    @_EnumProc
    def cb(hwnd, _lparam):
        if visible_only and not _user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not _is_qq(pid.value, _QQ_IMAGES):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, n + 1))
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        found.append({"hwnd": int(hwnd or 0), "pid": int(pid.value),
                      "title": buf.value, "cls": cls.value,
                      "visible": bool(_user32.IsWindowVisible(hwnd))})
        return True

    try:
        _user32.EnumWindows(cb, 0)
    except Exception:
        return []
    return found


def hide_qq_windows_once() -> int:
    """把此刻所有可见的 QQ 窗口藏起来，返回藏了几个。

    `ShowWindow(SW_HIDE)` 和「最小化」不一样：它直接从桌面和任务栏上拿掉，
    用户完全看不见；进程照常跑，NapCat 的注入和网络都不受影响。
    QQ 自己有时会再 `ShowWindow` 一次（比如登录成功后切换主窗口），
    所以调用方需要**反复扫**，见 `guard_qq_windows`。
    """
    if os.name != "nt":
        return 0
    n = 0
    for w in qq_windows():
        try:
            if _user32.ShowWindow(wintypes.HWND(w["hwnd"]), _SW_HIDE):
                n += 1
        except Exception:
            continue
    return n


def show_qq_windows_once() -> int:
    """把藏起来的 QQ 窗口重新显示出来（登录凭据失效、需要扫码时用）。"""
    if os.name != "nt":
        return 0
    n = 0
    for w in qq_windows(visible_only=False):
        # 这几个是 QQ 内部的辅助窗口，显示出来只会糊在桌面上
        if w["cls"] in ("IME", "Base_PowerMessageWindow",
                        "Electron_SystemPreferencesHostWindow"):
            continue
        try:
            h = wintypes.HWND(w["hwnd"])
            # ShowWindow 对别的进程的窗口经常"返回成功但没反应"，
            # ShowWindowAsync 才会把请求投递到对方的 UI 线程里。
            _user32.ShowWindowAsync(h, 5)          # SW_SHOW
            if w["cls"] == "ConsoleWindowClass":
                _user32.SetWindowPos(h, 0, 120, 120, 1000, 700, 0x0040)
            if _user32.IsWindowVisible(h):
                n += 1
        except Exception:
            continue
    return n


def guard_qq_windows(*, timeout: float = 150.0, interval: float = 0.8,
                     stop: Optional[Callable[[], bool]] = None) -> int:
    """在 timeout 秒内持续把 QQ 的窗口按下去，返回累计隐藏次数。

    `stop` 是提前收工的判据（比如「机器人已经连上 NapCat 了」）——
    登录完成后窗口不会再冒出来，继续扫只是浪费 CPU。
    """
    start = time.time()
    total = 0
    while time.time() - start < timeout:
        total += hide_qq_windows_once()
        if stop is not None:
            try:
                if stop():
                    break
            except Exception:
                pass
        time.sleep(max(0.05, interval))
    return total


# ──────────────────────────────────────────────────────────────────────
#  计划任务
# ──────────────────────────────────────────────────────────────────────
def _no_window() -> Dict[str, Any]:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def task_file(name: str = TASK_NAME) -> Path:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return Path(root) / "System32" / "Tasks" / name


def task_exists(name: str = TASK_NAME) -> bool:
    """任务在不在。

    先问 schtasks（最准），问不到就退回看任务文件 —— 有些环境（安全软件、
    受限的父进程）会把 schtasks.exe 拦掉，而 `C:\\Windows\\System32\\Tasks\\<名字>`
    这个文件普通用户也是可读的。两条都查不到才当「没装」。
    """
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", name],
                           capture_output=True, timeout=12, **_no_window())
        if r.returncode == 0:
            return True
    except Exception:
        pass
    try:
        return task_file(name).is_file()
    except Exception:
        return False


def run_task(name: str = TASK_NAME) -> Tuple[bool, str]:
    """触发任务。**不需要管理员** —— 跑一个已存在的任务是允许的。"""
    if os.name != "nt":
        return False, "只有 Windows 支持"
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", name],
                           capture_output=True, timeout=20, **_no_window())
    except Exception as e:
        return False, f"调不起计划任务：{e}"
    if r.returncode == 0:
        return True, f"已触发计划任务 {name}"
    out = (r.stdout or b"") + (r.stderr or b"")
    return False, out.decode("gbk", "replace").strip()[:300] or f"返回码 {r.returncode}"


def _current_user() -> str:
    dom = os.environ.get("USERDOMAIN", "").strip()
    usr = os.environ.get("USERNAME", "").strip() or os.environ.get("USER", "").strip()
    return f"{dom}\\{usr}" if dom and usr else usr


def task_xml(*, pythonw: str, root: Path, user: str, desc: str = "NapCat for qq-companion",
             delay_sec: int = 15, repeat_min: int = 5) -> str:
    """任务定义：**登录时自动触发**（延迟 15 秒）+ 每 5 分钟巡一次、最高权限。

    为什么触发器要写进任务本身，而不是让 boot.py 去 `schtasks /run`：
      - 计划任务由 Windows 自己拉起时，权限是任务里声明的「最高」，**不弹 UAC**；
      - boot.py 就不用依赖 schtasks.exe（有些安全策略会把它拦掉）；
      - 顺带变成一个看门狗：QQ 崩了或用户手动关了，5 分钟内会被重新拉起来。
        `python -m app.napcat launch` 第一件事就是看 QQ 在不在，在就直接退出，
        所以这个「每 5 分钟」是零成本的空转。
    """
    import html
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{html.escape(desc)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{html.escape(user)}</UserId>
      <Delay>PT{max(0, int(delay_sec))}S</Delay>
      <Repetition>
        <Interval>PT{max(1, int(repeat_min))}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{html.escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{html.escape(pythonw)}</Command>
      <Arguments>-m app.napcat launch</Arguments>
      <WorkingDirectory>{html.escape(str(root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_task(*, root: Optional[Path] = None, name: str = TASK_NAME) -> Tuple[bool, str]:
    """创建计划任务。**需要管理员**，所以只能由提权后的入口调用。"""
    if os.name != "nt":
        return False, "只有 Windows 支持"
    root = Path(root or ROOT)
    from app import autostart
    xml = task_xml(pythonw=str(autostart.pythonw()), root=root, user=_current_user())

    tmp = Path(tempfile.gettempdir()) / f"{name}-task.xml"
    try:
        # schtasks /xml 只认 UTF-16（带 BOM），写成 UTF-8 会报「无法分析」
        tmp.write_text(xml, encoding="utf-16")
    except Exception as e:
        return False, f"任务定义写不出去：{e}"

    try:
        r = subprocess.run(["schtasks", "/create", "/tn", name, "/xml", str(tmp), "/f"],
                           capture_output=True, timeout=30, **_no_window())
    except Exception as e:
        return False, f"调不起 schtasks：{e}"
    if r.returncode != 0:
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("gbk", "replace").strip()
        if "拒绝访问" in out or "Access is denied" in out:
            return False, "需要管理员权限：请右键 install_napcat_task.bat → 以管理员身份运行"
        return False, out[:400] or f"返回码 {r.returncode}"
    if not task_exists(name):
        return False, "任务写进去了但查不到（可能被安全软件拦了）"
    return True, (f"已创建计划任务 {name}：登录后 15 秒自动拉起 NapCat，"
                  f"之后每 5 分钟巡一次（QQ 崩了会自动重启），全程无 UAC")


def remove_task(name: str = TASK_NAME) -> Tuple[bool, str]:
    if os.name != "nt":
        return False, "只有 Windows 支持"
    if not task_exists(name):
        return True, "本来就没有这个任务"
    try:
        r = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                           capture_output=True, timeout=20, **_no_window())
    except Exception as e:
        return False, f"调不起 schtasks：{e}"
    if r.returncode == 0:
        return True, f"已删除计划任务 {name}"
    return False, ((r.stdout or b"") + (r.stderr or b"")).decode("gbk", "replace").strip()[:300]


# ──────────────────────────────────────────────────────────────────────
#  连接状态
# ──────────────────────────────────────────────────────────────────────
def connection_state(log_path: Optional[Path] = None) -> Optional[bool]:
    """从机器人日志尾部判断 NapCat 现在连没连上。None = 看不出来。

    比「端口在听」可靠：3001 是机器人自己在听，跟 NapCat 有没有连上无关。
    """
    if log_path is None:
        return None
    try:
        p = Path(log_path)
        if not p.is_file():
            return None
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - 200_000))
            text = f.read().decode("utf-8", "replace")
    except Exception:
        return None

    # 只看「本次启动」之后的那一段：日志是追加式的，上一次登录留下的
    # 「NapCat 已连接」会一直躺在文件里，不设边界就会把「早就掉线了」
    # 误判成在线（08:29 那次开机自启后 QQ 明明在等扫码，boot-state 却
    # 写着 qqConnected=true，就是这么来的）。
    start = text.rfind("qq-companion 已启动")
    scoped = start >= 0
    if scoped:
        text = text[start:]
    last_on = max(text.rfind("NapCat 已连接"), text.rfind("NapCat 生命周期事件"))
    last_off = text.rfind("NapCat 断开")
    if last_on < 0 and last_off < 0:
        # 本次启动之后一条连接记录都没有：会话是新的、还没连上，明确是 False。
        # 连启动横幅都找不到才叫「看不出来」（None）。
        return False if scoped else None
    return last_on > last_off


def status(*, config_path: Optional[Path] = None) -> Dict[str, Any]:
    """一次把定位结果、任务状态、QQ 进程、连接状态都取回来。绝不抛异常。"""
    shell = find_shell()
    qq = find_qq()
    uin = quick_uin(shell)
    conn = None
    if config_path is not None:
        try:
            sys.path.insert(0, str(ROOT))
            from app.config import load_config
            conn = connection_state(load_config(Path(config_path)).log_path())
        except Exception:
            conn = None
    return {
        "shell": str(shell) if shell else "",
        "qq": str(qq) if qq else "",
        "uin": uin,
        "qqRunning": qq_running(),
        "task": task_exists(),
        "taskName": TASK_NAME,
        "connected": conn,
    }


# ──────────────────────────────────────────────────────────────────────
#  命令行（计划任务的动作就是 `-m app.napcat launch`）
# ──────────────────────────────────────────────────────────────────────
def _say(msg: str) -> None:
    """pythonw 下没有 stdout，直接 print 会抛 AttributeError 把进程带崩。

    计划任务的动作就是 pythonw，所以这里必须容错：能 print 就 print，
    不能就追加到 data/boot-napcat.log（和 NapCat 自己的输出同一个文件）。
    """
    try:
        print(msg)
        try:
            sys.stdout.flush()
        except Exception:
            pass
        return
    except Exception:
        pass
    try:
        log = ROOT / "data" / "boot-napcat.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"[launcher] {msg}\n")
    except Exception:
        pass


def _launch_cli() -> int:
    # 排查开关：data/napcat-nohide.flag 存在时**完全不碰窗口**。
    nohide = (ROOT / "data" / "napcat-nohide.flag").exists()
    # 看门狗模式：每 5 分钟被任务叫一次，QQ 还在就直接退（不重复注入）。
    if qq_running():
        # 看门狗这条路也要尊重 nohide —— 否则「扫码时别藏窗口」的开关
        # 会被每 5 分钟的巡检悄悄破坏（窗口刚露出来又被按下去）。
        hidden = 0 if nohide else hide_qq_windows_once()
        _say("QQ 已经在跑，跳过" + (f"（顺手藏了 {hidden} 个窗口）" if hidden else ""))
        return 0
    shell = find_shell()
    if shell is None:
        _say("× 找不到 NapCat.Shell（里面应该有 NapCatWinBootMain.exe）")
        return 2
    if not (shell / START_BAT).is_file():
        _say(f"× 找不到 {shell / START_BAT}")
        return 2

    uin = quick_uin(shell)
    # 排查开关：data/napcat-qr.flag 存在时强制走扫码（丢掉 -q）。
    # 快速登录失败后 QQ 有时会停在「等待确认」不吭声，直接出码比干等快。
    qr_only = (ROOT / "data" / "napcat-qr.flag").exists()
    if qr_only:
        uin = ""
    log = ROOT / "data" / "boot-napcat.log"
    # 排查开关：data/napcat-direct.flag 存在时绕开 bat，直接调注入器。
    # 用来区分「bat 这层有问题」还是「QQ 本身起不来」。
    direct = (ROOT / "data" / "napcat-direct.flag").exists()
    try:
        if direct:
            qq = find_qq()
            if qq is None:
                _say("× 找不到 QQ.exe（直接模式）")
                return 2
            pid = spawn(shell, qq, uin, log_path=log, hide=True)
        else:
            pid = spawn_via_bat(shell, uin, log_path=log, hide=True)
    except Exception as e:
        _say(f"× 拉不起来：{e}")
        return 2
    _say(f"✓ 已通过 {'直接注入' if direct else START_BAT} 拉起（PID {pid}，"
         f"{'强制扫码（data/napcat-qr.flag）' if qr_only else ('快速登录 ' + uin if uin else '没配快速登录号，会走扫码')}）")

    # 窗口守护：QQ 从「登录窗」切到「主窗」时会自己再 ShowWindow 一次，
    # 所以不是隐藏一遍就够，得盯一段时间。
    #
    # QQ 的 UI 初始化对「窗口一创建就被隐藏」很敏感（Electron 会以为
    # 用户不想要界面），登录页可能因此一直不出来。
    if nohide:
        _say("窗口守护已跳过（data/napcat-nohide.flag 存在）")
        return 0 if qq_running() else 1

    conn_log = ROOT / "data" / "companion.log"
    start = time.time()

    def _done() -> bool:
        if time.time() - start < 25:
            return False
        return connection_state(conn_log) is True

    hidden = guard_qq_windows(timeout=150.0, stop=_done)
    _say(f"窗口守护结束：累计隐藏 {hidden} 次；"
         f"QQ 进程{'在' if qq_running() else '不在'}；"
         f"NapCat {'已连接' if connection_state(conn_log) else '还没连上'}")
    return 0 if qq_running() else 1


def spawn_guard(log_path: Path, *, timeout: float = 180.0) -> int:
    """后台起一个只管「把 QQ 窗口藏起来」的进程。

    为什么不直接在当前进程里 `guard_qq_windows()`：调用方（boot.py）还要
    接着拉面板，被一段 3 分钟的循环堵住不合适。计划任务那条路径不需要它
    —— `launch` 自己就是常驻的，顺手守了。
    """
    from app import autostart
    cmd = [str(autostart.pythonw()), "-m", "app.napcat", "guard",
           "--timeout", str(int(max(10, timeout)))]
    return autostart.spawn(cmd, cwd=ROOT, log_path=Path(log_path), hide=True)


def _status_cli(config_path: Optional[Path]) -> int:
    st = status(config_path=config_path)
    print()
    print("  NapCat")
    print("  ────────────────────────────────────────────────")
    print(f"  目录      : {st['shell'] or '× 没找到 NapCat.Shell'}")
    print(f"  QQ        : {st['qq'] or '× 没找到 QQ.exe'}")
    print(f"  快速登录  : {st['uin'] or '（没配，需要扫码）'}")
    print(f"  QQ 进程   : {'在跑' if st['qqRunning'] else '没在跑'}")
    print(f"  计划任务  : {'已装（' + st['taskName'] + '）' if st['task'] else '没装（提权拉不起来）'}")
    if st["connected"] is None:
        print("  连接状态  : 看不出来（日志里还没有记录）")
    else:
        print(f"  连接状态  : {'已连接' if st['connected'] else '未连接'}")
    print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.napcat", description="NapCat 的定位与静默拉起")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("launch", help="静默拉起 NapCat（计划任务用的就是它）")
    gp = sub.add_parser("guard", help="只在后台隐藏 QQ 窗口（不负责启动）")
    gp.add_argument("--timeout", type=float, default=180.0, help="守护多少秒，默认 180")
    sub.add_parser("hide", help="立刻把 QQ 窗口藏起来")
    sub.add_parser("show", help="把 QQ 窗口放出来（扫码登录时用）")
    sub.add_parser("status", help="看定位/任务/连接状态")
    sub.add_parser("install-task", help="创建最高权限的计划任务（需要管理员）")
    sub.add_parser("remove-task", help="删除计划任务")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args(argv)

    cmd = args.cmd or "status"
    if cmd == "launch":
        return _launch_cli()
    if cmd == "guard":
        hidden = guard_qq_windows(timeout=float(args.timeout))
        _say(f"窗口守护结束：累计隐藏 {hidden} 次")
        return 0
    if cmd == "hide":
        print(f"藏了 {hide_qq_windows_once()} 个窗口")
        return 0
    if cmd == "show":
        print(f"放出了 {show_qq_windows_once()} 个窗口")
        return 0
    if cmd == "status":
        return _status_cli(Path(args.config))
    if cmd == "install-task":
        ok, msg = install_task()
        print(("✓ " if ok else "× ") + msg)
        return 0 if ok else 1
    if cmd == "remove-task":
        ok, msg = remove_task()
        print(("✓ " if ok else "× ") + msg)
        return 0 if ok else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
