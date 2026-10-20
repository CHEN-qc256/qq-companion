"""主动搭话（P2）：隔太久了他会自己找你说话。

难点全在**别像推销号**。真人主动找你有几个特征：

  - 有由头 —— 提一件之前没聊完的事，不是干巴巴"在吗"
  - 有上限 —— 一天最多一两次，你没回他就不会再追
  - 挑时候 —— 凌晨、上班最忙的点不会来烦你
  - 你没回就不再发 —— **他回你之前，你最多主动找他一次**

所以这个模块的护栏比功能代码多：
一次 tick 最多发一条；只在活跃时间窗；睡着不发；省电模式不发；
每天有次数上限；上一次主动之后他没回过就不再发；没有可提的旧事就干脆不发。

**搭话给谁也是单独一层。** 朋友可以跟它聊天，但不该被它主动打扰 ——
所以默认只找 `whitelist.masters`（没配 masters 才退回"聊过的私聊对象都可能被搭话"），
想指定就用 `life.proactive.targets`。群聊永远不主动开腔。

**一个人最多被搭一次，不会全砸在同一个人头上。** tick 会把所有够格的会话排出来，
按"上次被搭话的时间"挑最久没被搭的那个（从没搭过的优先）。
早先的写法是直接取列表第一个，而列表是按 session_id 排序的 ——
谁排在前面谁就被永远选中，另一个人一天都等不到一句。

发出去的回复一样会落库（user 侧存的是 `wrap_proactive_message` 的指令块，
不是编造一句"他说过的话"），这样下一轮上下文的**前缀仍然逐字节一致**，
缓存照样命中。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import List, Optional, Tuple

from .budget import Budget
from .config import CST, Config
from .context import ContextBuilder, wrap_proactive_message
from .humanize import sanitize, send_humanized
from .life import Life
from .llm import DeepSeek
from .memory import Memory
from .onebot import IncomingMessage, OneBotAdapter
from .store import Store

log = logging.getLogger("companion.proactive")


def msg_for_session(session_id: str) -> IncomingMessage:
    """光有 session_id 也能造一个能用来发消息的壳。"""
    kind, _, rest = session_id.partition(":")
    if kind == "group":
        return IncomingMessage(message_type="group", user_id=0,
                               group_id=int(rest or 0), self_id=0, text="")
    return IncomingMessage(message_type="private", user_id=int(rest or 0),
                           group_id=None, self_id=0, text="")


class Proactive:
    def __init__(self, cfg: Config, store: Store, llm: DeepSeek,
                 ctx: ContextBuilder, budget: Budget, adapter: OneBotAdapter,
                 life: Life, memory: Memory):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.ctx = ctx
        self.budget = budget
        self.adapter = adapter
        self.life = life
        self.memory = memory
        self._stop = asyncio.Event()

    # ---------------- 后台循环 ----------------

    async def run_forever(self) -> None:
        p = self.cfg.life.proactive
        if not p.enabled:
            log.info("主动搭话已关闭（life.proactive.enabled=false）")
            return
        interval = max(60.0, float(p.check_interval_minutes) * 60.0)
        allow = self.cfg.proactive_targets()
        log.info("主动搭话已开启：%d~%d 点之间、静默超过 %.1f 小时、每天最多 %d 次、两次之间隔 %.1f 小时",
                 int(p.active_hours[0]), int(p.active_hours[1]),
                 p.min_idle_hours, p.max_per_day, p.min_hours_between)
        if allow:
            log.info("主动搭话只会找你指定的这些人：%s"
                     "（来自 life.proactive.targets，或 whitelist.masters）。其他人不会被打扰。",
                     "、".join(str(x) for x in allow))
        else:
            log.info("没指定搭话对象、也没配主人：聊过的私聊对象都可能被主动打扰。"
                     "想只让它找你，填 life.proactive.targets 或 whitelist.masters。")
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("主动搭话轮询出错（不影响聊天）")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ---------------- 一次轮询 ----------------

    async def tick(self, now: Optional[datetime] = None) -> int:
        """扫一遍能搭话的会话，最多发一条。返回发了几条。"""
        p = self.cfg.life.proactive
        if not p.enabled:
            return 0

        now = now or datetime.now(CST)
        if not self.life.in_active_hours(now):
            return 0
        if not self.life.awake(now):
            return 0
        if await self.budget.mode() != "normal":
            log.debug("省电/停用模式，跳过主动搭话")
            return 0
        if not self.adapter.connected.is_set():
            return 0

        ready: List[Tuple[str, float, int]] = []
        for sid in await self.targets():
            info = await self.ready(sid, now)
            if info:
                ready.append((sid, info[0], info[1]))
        if not ready:
            return 0

        # ★ 轮换 ★ 不能直接取第一个：会话是按 session_id 排序的，
        # 谁在前谁就永远被选中，另一个人一天都等不到一句。
        # 按「上次被搭话的时间」升序挑，从没搭过的排最前，并列就按 session_id 稳定排序。
        picked = None
        best: Optional[Tuple[int, str]] = None
        for sid, idle_hours, last_real in ready:
            key = (await self.store.kv_get_int(f"ping_at:{sid}", 0), sid)
            if best is None or key < best:
                best = key
                picked = (sid, idle_hours, last_real)
        if picked is None:
            return 0

        sid, idle_hours, last_real = picked
        if await self.ping(sid, idle_hours=idle_hours, now=now, last_user_id=last_real):
            return 1
        return 0

    async def targets(self, *, private_only: bool = True) -> List[str]:
        """哪些会话可以被主动搭话。

        朋友可以跟它聊天，但**默认不该被它主动打扰** —— 这是两件事。
        所以顺序是：life.proactive.targets > whitelist.masters > 不限制。
        """
        allow = self.cfg.proactive_targets()
        out: List[str] = []
        for sid in await self.store.sessions(private_only=private_only):
            kind, _, rest = sid.partition(":")
            if kind != "private":
                continue          # 群里不主动开腔：没人搭理的自言自语，比不发更减分
            try:
                uid = int(rest or 0)
            except ValueError:
                continue
            if allow and uid not in allow:
                continue
            out.append(sid)
        return out

    async def ready(self, sid: str, now: datetime) -> Optional[Tuple[float, int]]:
        """这个会话现在够不够格被主动搭话。够格返回 (静默小时数, 他最后一句的消息 id)。"""
        p = self.cfg.life.proactive

        last = await self.store.last_message(sid)
        if not last:
            return None                       # 从没聊过，不主动开腔

        idle_hours = (time.time() - float(last["created_at"] or 0)) / 3600.0
        if idle_hours < p.min_idle_hours:
            return None

        # 他回你之前，你最多主动找他一次。
        # 用 last_real_user_id 而不是 last_user_id —— 后者会把我们自己的主动搭话
        # 也算进去，于是水位永远被自己顶高，这条护栏等于没有。
        last_real = await self.store.last_real_user_id(sid)
        if await self.store.kv_get_int(f"ping_since:{sid}", -1) == last_real:
            log.debug("%s：上次主动之后他没回，不再追", sid)
            return None

        # 两次主动之间隔一会儿，别一整天的话都攒给一个人
        if p.min_hours_between > 0:
            last_ping = await self.store.kv_get_int(f"ping_at:{sid}", 0)
            if last_ping and (time.time() - last_ping) / 3600.0 < p.min_hours_between:
                log.debug("%s：距上次主动还不到 %.1f 小时", sid, p.min_hours_between)
                return None

        # 每天的次数上限
        day = now.strftime("%Y%m%d")
        if await self.store.kv_get_int(f"ping_count:{sid}:{day}", 0) >= p.max_per_day:
            return None

        return idle_hours, last_real

    async def _maybe_ping(self, sid: str, now: datetime) -> bool:
        """单个会话的「够格就发」。留着给测试和外部调用用。"""
        info = await self.ready(sid, now)
        if not info:
            return False
        return await self.ping(sid, idle_hours=info[0], now=now, last_user_id=info[1])

    async def ping(self, sid: str, *, idle_hours: float = 8.0,
                   now: Optional[datetime] = None,
                   last_user_id: Optional[int] = None) -> bool:
        """真的发一条。测试可以直接调这个。"""
        now = now or datetime.now(CST)
        topics = await self.memory.topics(sid, self.cfg.life.proactive.topics_from_memory)
        if not topics:
            # 没有由头就不发 —— 干巴巴的"在吗"比不发更减分
            log.debug("%s：没有可提的旧事，这次不主动", sid)
            return False

        wrapped = wrap_proactive_message(topics, mood=self.life.mood(now),
                                        idle_hours=idle_hours, now=now)
        try:
            messages, _ = await self.ctx.build(sid, wrapped)
            result = await self.llm.chat(
                messages, purpose="proactive", session_id=sid,
                max_tokens=self.cfg.budget.saver_max_tokens + 40,
            )
        except Exception as e:
            log.warning("主动搭话生成失败：%s", e)
            return False

        reply = sanitize(result.text)
        if not reply:
            return False

        await self.store.add_message(sid, "user", wrapped, "")
        await self.store.add_message(sid, "assistant", reply, reply)
        await send_humanized(self.cfg, self.adapter, msg_for_session(sid), reply, log=log)

        day = now.strftime("%Y%m%d")
        await self.store.kv_set(f"ping_count:{sid}:{day}",
                                await self.store.kv_get_int(f"ping_count:{sid}:{day}", 0) + 1)
        # 记下这次的时间，tick 靠它做轮换（谁最久没被搭过话就轮到谁）
        await self.store.kv_set(f"ping_at:{sid}", int(time.time()))
        if last_user_id is not None:
            await self.store.kv_set(f"ping_since:{sid}", last_user_id)

        log.info("主动搭话（隔了 %.1f 小时，由头：%s）：%s",
                 idle_hours, topics[0][:12], reply[:40])
        return True
