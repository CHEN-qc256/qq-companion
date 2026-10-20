"""qq-companion —— 把 AI 接进 QQ，像真人一样陪你说话。

这个文件除了声明包，还顺手做两件 Windows 上必须做的事：

1. **把标准输出改成 UTF-8**。中文版 Windows 的控制台默认是 GBK，脚本里打印一个 "✓"
   就会 `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'` 直接崩掉。
2. **打开行缓冲**。输出被重定向到文件时 Python 默认块缓冲，父进程的话会攒到退出才写，
   于是子进程（setup.py 里跑的 doctor.py）的输出会诡异地跑到父进程标题前面去。

放在包的 __init__ 里，所有入口（main.py / setup.py / 各种 tools）都能白拿这两个修复。
"""
from __future__ import annotations

import sys

__version__ = "1.0.0"


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            enc = (getattr(stream, "encoding", "") or "").lower()
            if enc in ("utf-8", "utf8"):
                stream.reconfigure(line_buffering=True)
            else:
                stream.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True)
        except Exception:
            pass          # 重定向到文件、被别的程序接管等情况下就算了


_force_utf8()
