"""开机自启：往用户级注册表里写一条启动项，并提供一个「不弹窗」的启动封装。

为什么是 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`：
  - **用户级、不需要管理员**。QQ / NapCat 都是用户态程序，不能做成系统服务
    （服务跑在 Session 0，看不见桌面，QQ 登不上）。所以「随系统启动」这个选项
    本来就不成立，能做的只有「随用户登录启动」。
  - 写/删各一次 winreg 调用，失败能立刻回滚，验证也只要一条 `reg query`。
  - 备选方案都不要：启动文件夹要造 .lnk（得走 COM 或 PowerShell，路径一挪
    就变成指向空文件的死快捷方式）；任务计划程序要 schtasks 拼参数，路径带
    空格时引号极易出错，而且它会「静默失败」—— 看不出到底有没有生效。

为什么命令里用 `pythonw.exe` 而不是 `python.exe`：
  python.exe 是**控制台**子系统的程序，由系统拉起时一定会先分配一个控制台，
  于是登录瞬间会闪一下黑框（哪怕进程马上就把窗口藏了）。pythonw.exe 是 GUI
  子系统，天然没有控制台，连闪都不会闪。

⚠ 一条容易踩的坑：pythonw 下 `sys.stdout` 是 None，任何一句 print 都会
`AttributeError: 'NoneType' object has no attribute 'write'`。所以这里所有
`spawn()` 都强制把子进程的 stdout 重定向到文件 —— 不只是为了留日志，更是为了
让子进程的 `sys.stdout` 变成一个**真文件**，不会因为一句无关的 print 就崩。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 用户级 Run 键。HKEY_CURRENT_USER 下，没有管理员权限也能写。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "qq-companion"

# 引导器文件名。注册表里指向的就是它。
BOOT_SCRIPT = "boot.py"

ROOT = Path(__file__).resolve().parent.parent


class AutostartError(Exception):
    """自启开关失败了。消息是给用户看的中文，调用方直接显示即可。"""


# ──────────────────────────────────────────────────────────────────────
#  路径 / 命令拼装
# ──────────────────────────────────────────────────────────────────────
def pythonw(python: Optional[str] = None) -> Path:
    """和当前解释器配套的 pythonw.exe。找不到就退回 python.exe。

    退回是有代价的：python.exe 在登录时会闪一下黑框（窗口在进程启动后才隐藏，
    总有那么几十毫秒）。所以 status() 会把这种情况标成 warn，面板上能看见。
    """
    exe = Path(python or sys.executable)
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return cand
    found = shutil.which("pythonw")
    if found:
        return Path(found)
    return exe


def boot_script(root: Optional[Path] = None) -> Path:
    return Path(root or ROOT) / BOOT_SCRIPT


def command(root: Optional[Path] = None, python: Optional[str] = None) -> str:
    """注册表里那一整条命令。路径带空格也没问题 —— 两段各自加引号。"""
    pyw = pythonw(python)
    return f'"{pyw}" "{boot_script(root)}" --auto'


# ──────────────────────────────────────────────────────────────────────
#  注册表读写
# ──────────────────────────────────────────────────────────────────────
def _winreg():
    if os.name != "nt":
        raise AutostartError("开机自启只在 Windows 上支持。")
    import winreg  # noqa: WPS433  —— 只在 Windows 上才存在
    return winreg


def _reg_exe() -> str:
    """系统自带的 reg.exe。写注册表被策略挡住时用它兜底。"""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "reg.exe")


def _reg_add(key_path: str, value_name: str, cmd: str, cause: Exception) -> None:
    """winreg 被拒时的兜底：调 reg.exe 写。

    为什么需要这条路：有些机器上 uv 装的 python.exe 被安全策略套了一个
    「写受限」令牌，winreg.CreateKeyEx 一律 WinError 5；但 reg.exe 在普通
    桌面会话里是能写的。写入内容完全相同（REG_SZ + 同一条命令）。
    """
    try:
        p = subprocess.run(
            [_reg_exe(), "add", f"HKCU\\{key_path}", "/v", value_name,
             "/t", "REG_SZ", "/d", cmd, "/f"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise AutostartError(f"写注册表失败：{cause}；改调用 reg.exe 也没起来：{e}")
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip() or f"exit={p.returncode}"
        raise AutostartError(
            f"写注册表失败：{cause}\nreg.exe 也失败：{detail}\n"
            f"当前机器的安全策略把 python 挡在注册表外时，"
            f"请从资源管理器里双击运行项目根目录的 install_autostart.bat"
            f"（它用 cmd 的 reg 命令写，不受这个限制）。")


def _reg_delete(key_path: str, value_name: str, cause: Exception) -> None:
    """winreg 删不掉时的兜底。值本来就不存在不算失败。"""
    try:
        p = subprocess.run(
            [_reg_exe(), "delete", f"HKCU\\{key_path}", "/v", value_name, "/f"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise AutostartError(f"删注册表失败：{cause}；reg.exe 也没起来：{e}")
    if p.returncode != 0:
        text = (p.stderr or p.stdout or "").lower()
        if "unable to find" in text or "找不到" in text or "不存在" in text:
            return
        raise AutostartError(f"删注册表失败：{cause}\nreg.exe：{text.strip()}")


def read(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> str:
    """读当前登记的命令。没登记（或系统不是 Windows）就返回空字符串。"""
    if os.name != "nt":
        return ""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            val, _ = winreg.QueryValueEx(k, value_name)
            return str(val or "")
    except FileNotFoundError:
        return ""          # 键或值不存在 —— 正常情况，就是「没开」
    except OSError:
        return ""          # 权限被挡 / 注册表被策略锁住，当作没开


def is_enabled(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> bool:
    return bool(read(key_path, value_name).strip())


def enable(root: Optional[Path] = None, *, python: Optional[str] = None,
           key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> Dict[str, Any]:
    """写启动项。**写完立刻读回来核对** —— 不核对等于没验证。"""
    winreg = _winreg()
    root = Path(root or ROOT)
    script = boot_script(root)
    if not script.is_file():
        raise AutostartError(f"找不到引导器 {script}，没法设自启。")
    cmd = command(root, python)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        _reg_add(key_path, value_name, cmd, e)

    got = read(key_path, value_name)
    if got != cmd:
        raise AutostartError(
            f"注册表写进去了但读回来不一致（可能被安全软件回滚）。\n"
            f"写入：{cmd}\n读回：{got or '（空）'}")
    return status(root, python=python, key_path=key_path, value_name=value_name)


def disable(root: Optional[Path] = None, *,
            key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> Dict[str, Any]:
    """删启动项。本来就没有也算成功（幂等）。"""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, value_name)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    except OSError as e:
        _reg_delete(key_path, value_name, e)

    if is_enabled(key_path, value_name):
        raise AutostartError("删了但注册表里还在，可能被安全软件拦了。")
    return status(root, key_path=key_path, value_name=value_name)


# ──────────────────────────────────────────────────────────────────────
#  状态
# ──────────────────────────────────────────────────────────────────────
def status(root: Optional[Path] = None, *, python: Optional[str] = None,
           key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> Dict[str, Any]:
    """一次把面板/体检需要的现状都取回来。绝不抛异常。"""
    root = Path(root or ROOT)
    cmd = read(key_path, value_name)
    expect = command(root, python)
    pyw = pythonw(python)
    warn: List[str] = []

    if not pyw.name.lower().startswith("pythonw"):
        warn.append("没找到 pythonw.exe，登录时可能会闪一下黑框"
                    "（功能不受影响，只是不干净）。")
    if not boot_script(root).is_file():
        warn.append(f"引导器 {BOOT_SCRIPT} 不在，自启打开也不会生效。")

    return {
        "supported": os.name == "nt",
        "enabled": bool(cmd.strip()),
        "command": cmd,
        "expected": expect,
        # stale = 注册表里有值，但指向的是**别的目录**（项目被挪走了 / 换了解释器）
        "stale": bool(cmd.strip()) and cmd.strip().lower() != expect.strip().lower(),
        "pythonw": str(pyw),
        "bootPy": str(boot_script(root)),
        "runKey": f"HKCU\\{key_path}",
        "valueName": value_name,
        "warn": warn,
    }


# ──────────────────────────────────────────────────────────────────────
#  上次自启的结果
# ──────────────────────────────────────────────────────────────────────
def read_state(data_dir: Path) -> Optional[Dict[str, Any]]:
    """读 boot.py 写下的那一份结果。没有 / 读坏了都返回 None。"""
    import json
    try:
        return json.loads((Path(data_dir) / "boot-state.json").read_text(encoding="utf-8"))
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
#  无窗口启动
# ──────────────────────────────────────────────────────────────────────
def hidden_kwargs(hide: bool = True) -> Dict[str, Any]:
    """给 Popen 的那套「别弹窗」参数。

    hide=True  → CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
                 DETACHED_PROCESS 让子进程**根本没有控制台**（不是「藏起来」，
                 是压根不分配），所以不可能有黑框，也不可能被父进程的 Ctrl+C
                 连带杀掉。项目里面板启动机器人用的也是这套。
    hide=False → 给它一个独立的新控制台，调试时能看见输出。

    为什么不用 `CREATE_NO_WINDOW`：MSDN 明确写着它和 DETACHED_PROCESS 同时
    出现时会被忽略 —— 两个一起写只是心理安慰。要隐藏就用 DETACHED_PROCESS，
    要留窗口就用 CREATE_NEW_CONSOLE。
    """
    if os.name != "nt":
        return {"start_new_session": True}
    if hide:
        return {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                  | subprocess.DETACHED_PROCESS)}
    return {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                             | subprocess.CREATE_NEW_CONSOLE)}


def spawn(cmd: List[str], *, cwd: Path, log_path: Path,
          env: Optional[Dict[str, str]] = None, hide: bool = True) -> int:
    """拉起一个子进程，输出写进 log_path，返回 PID。

    子进程的 stdout/stderr 一定指向**文件**（哪怕调用方并不关心日志）：
    pythonw 下没有控制台，如果让它继承一个空句柄，子进程里任何一句 print
    都会拿 None 去 write 然后崩掉。
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if env is None:
        # PYTHONUNBUFFERED：被重定向到文件时 Python 默认块缓冲，
        # 子进程在「加载中」那段会一个字都不写，看着像卡死。设上就能实时看到。
        env = dict(os.environ, PYTHONUNBUFFERED="1")
    fh = open(log_path, "ab")
    try:
        fh.write(f"\n===== {_now()} 启动 {Path(str(cmd[0])).name} =====\n".encode("utf-8"))
        fh.flush()
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
            **hidden_kwargs(hide),
        )
    finally:
        # 父进程关掉不影响子进程：句柄已经复制给子了
        try:
            fh.close()
        except Exception:
            pass
    return int(p.pid)


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
