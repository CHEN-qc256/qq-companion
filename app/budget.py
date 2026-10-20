"""预算守卫：把"一天花 10 块"变成"不可能发生"。

三档：
  normal  正常聊
  saver   超了日预算 → 砍历史窗口、砍输出上限、只回短句
  stop    超了硬上限 → 不调模型，只记日志（避免雪崩式烧钱）
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

from .config import Config
from .store import Store

log = logging.getLogger("companion.budget")

DAY = 86400.0
MONTH = 86400.0 * 30


class Budget:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._last_call: Dict[str, float] = {}
        self._cached_mode: str = "normal"
        self._mode_checked_at: float = 0.0

    async def _raw_mode(self) -> str:
        now = time.time()
        # 30 秒内复用上次结果，避免每条消息都查库
        if now - self._mode_checked_at < 30:
            return self._cached_mode

        day_spent = await self.store.spent_since(now - DAY)
        month_spent = await self.store.spent_since(now - MONTH)
        b = self.cfg.budget

        if month_spent >= b.monthly_yuan * b.hard_stop_multiplier or \
           day_spent >= b.daily_yuan * b.hard_stop_multiplier:
            mode = "stop"
        elif month_spent >= b.monthly_yuan or day_spent >= b.daily_yuan:
            mode = "saver"
        else:
            mode = "normal"

        if mode != self._cached_mode:
            log.warning("预算模式切换：%s → %s（今日 %.3f 元 / 本月 %.3f 元）",
                        self._cached_mode, mode, day_spent, month_spent)
        self._cached_mode = mode
        self._mode_checked_at = now
        return mode

    async def mode(self) -> str:
        return await self._raw_mode()

    async def wait_slot(self, session_id: str) -> float:
        """同一会话的最小请求间隔，防止连点/刷屏把预算烧穿。

        这里选"等"而不是"丢"：丢一条消息会让聊天凭空少一句话，
        在陪聊场景里是明显破绽；等待同样能限流，但不会掉话。
        返回实际等待秒数。
        """
        gap = self.cfg.budget.min_interval_seconds
        if gap <= 0:
            return 0.0
        wait = gap - (time.time() - self._last_call.get(session_id, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call[session_id] = time.time()
        return max(wait, 0.0)

    async def params(self) -> Dict[str, int]:
        """按当前模式给出本轮该用的 history / max_tokens。"""
        mode = await self.mode()
        c = self.cfg
        if mode == "saver":
            return {
                "history_tokens": c.budget.saver_history_tokens,
                "max_tokens": c.budget.saver_max_tokens,
            }
        return {
            "history_tokens": c.context.history_tokens,
            "max_tokens": c.llm.max_tokens,
        }
