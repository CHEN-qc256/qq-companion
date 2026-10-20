"""作息与心情。

为什么要这个：真人不是 24 小时待命的。如果不管几点都秒回、回得一样长，
"像个人"这件事立刻崩。作息决定了三件事：

  1. **该不该回** —— 睡了就攒着，睡醒一起回
  2. **回多长**   —— 上班摸鱼回得短、敷衍；有空的时候能多聊两句
  3. **什么语气** —— 晚上懒懒的、慢半拍

状态只有四个，够用了：
    sleeping       睡觉（不回）
    working        上班（短、敷衍）
    winding_down   晚上收尾（懒）
    free           有空
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .config import CST, Config

log = logging.getLogger("companion.life")

STATES = ("sleeping", "working", "winding_down", "free")

_STATE_CN = {
    "sleeping": "睡觉",
    "working": "上班",
    "winding_down": "晚上歇着",
    "free": "有空",
}


def _in_range(hour: int, lo: int, hi: int) -> bool:
    """支持跨零点，比如 [23, 7]。"""
    if lo == hi:
        return False
    if lo < hi:
        return lo <= hour < hi
    return hour >= lo or hour < hi


class Life:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ---------------- 状态 ----------------

    def state(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(CST)
        life = self.cfg.life
        if not life.enabled:
            return "free"

        lo, hi = (list(life.sleeping) + [1, 8])[:2]
        if _in_range(now.hour, int(lo), int(hi)):
            return "sleeping"

        if not life.weekday_only_work or now.weekday() < 5:
            for rng in (life.working or []):
                if len(rng) >= 2 and _in_range(now.hour, int(rng[0]), int(rng[1])):
                    return "working"

        if now.hour >= 22 or now.hour < 1:
            return "winding_down"
        return "free"

    def mood(self, now: Optional[datetime] = None) -> str:
        """给模型的一句话提示，会进动态块（不破坏缓存）。"""
        st = self.state(now)
        return (self.cfg.life.moods or {}).get(st, "") or ""

    def awake(self, now: Optional[datetime] = None) -> bool:
        return self.state(now) != "sleeping"

    # ---------------- 睡多久 ----------------

    def quiet_delay(self, now: Optional[datetime] = None) -> float:
        """睡了的话，返回"睡到醒"还要多少秒；醒着返回 0。"""
        now = now or datetime.now(CST)
        life = self.cfg.life
        if not life.enabled:
            return 0.0
        lo, hi = (list(life.sleeping) + [1, 8])[:2]
        if not _in_range(now.hour, int(lo), int(hi)):
            return 0.0
        target = now.replace(hour=0, minute=0, second=0, microsecond=0) \
                   + timedelta(hours=int(hi) % 24)
        if target <= now:
            target += timedelta(days=1)
        return max(0.0, (target - now).total_seconds())

    # ---------------- 主动搭话的时间窗 ----------------

    def in_active_hours(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(CST)
        p = self.cfg.life.proactive
        if not self.cfg.life.enabled:
            return True
        lo, hi = (list(p.active_hours) + [10, 23])[:2]
        return _in_range(now.hour, int(lo), int(hi))

    # ---------------- 展示 ----------------

    def describe(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(CST)
        st = self.state(now)
        txt = f"{_STATE_CN[st]}（{now:%H:%M}）"
        q = self.quiet_delay(now)
        if q > 0:
            txt += f"，还要 {q / 3600:.1f} 小时才醒"
        return txt
