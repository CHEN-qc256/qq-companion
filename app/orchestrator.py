"""编排：一条消息从进来到出去，中间要过哪些闸门。

顺序是刻意排的，每一步都在省钱：
  白名单 → 群聊触发判定 → 防抖合并 → 作息判定（睡觉就挂起）
  → 琐碎输入本地打发（0 token）→ 预算模式 → 记忆压缩判定
  → 长期记忆召回（0 token）→ 组装上下文 → 调模型 → 洗稿 → 拟人化发送
  → 顺手抽一次长期记忆（低频，默认 20 轮一次）

「睡觉不回」和「随机晚回」**不在这里 sleep** —— 见 _deferred_reply 的注释。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .budget import Budget
from .config import CST, Config
from .context import ContextBuilder, format_group_lines, wrap_user_message
from .humanize import read_delay, sanitize, send_humanized
from .life import Life
from .llm import ChatResult, DeepSeek
from .memory import Memory
from .onebot import IncomingMessage, OneBotAdapter
from .store import Store
from .tts import (guess_emotion, load_emotions, reply_denies_voice,
                  service_alive, should_speak, speak, speech_requested)
from .videopush import (NOW_FLAG, add_group, group_targets, parse_topics,
                        push_targets, remove_group, set_enabled)
from .websearch import TOOLS as WEB_TOOLS, run_tool as run_web_tool

log = logging.getLogger("companion.orchestrator")


class Orchestrator:
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

        self._buffers: Dict[str, List[IncomingMessage]] = {}
        self._timers: Dict[str, asyncio.Task] = {}
        self._deferred: Dict[str, asyncio.Task] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._trivial = [(re.compile(p), r) for p, r in (cfg.humanize.trivial_replies or [])]
        self._mood: Dict[str, str] = {}

        # 群聊限流用的状态。只在内存里，重启就清 —— 这本来就是软性的礼貌，
        # 不需要跨重启持久化（重启后能第一时间回消息反而是好事）。
        self._grp_last_reply: Dict[str, float] = {}        # session_id -> 上次回复时间
        self._grp_last_user: Dict[str, float] = {}         # sid|uid -> 上次回复这个人的时间
        self._grp_hour: Dict[str, Tuple[int, int]] = {}    # sid -> (小时序号, 该小时已回条数)
        self._grp_seen: set = set()                        # 打过招呼的群，避免重复刷日志

        # 语音服务刚失败过就先冷却一会儿，别让每条回复都去等一次超时。
        # 时间戳：到这个时刻之前不再尝试合成。
        self._voice_down_until: float = 0.0

    # ---------------- 入口 ----------------

    async def handle(self, msg: IncomingMessage) -> None:
        if not await self._allowed(msg):
            return
        # 显式指令（「视频推送 开」）优先：不进退避/防抖，不吃群聊限流，
        # 也不花一个 token —— 这是开关，不是聊天。
        if await self._handle_command(msg):
            return
        if msg.message_type == "group" and not self._group_should_reply(msg):
            return

        sid = msg.session_id
        self._buffers.setdefault(sid, []).append(msg)

        old = self._timers.pop(sid, None)
        if old and not old.done():
            old.cancel()

        self._timers[sid] = asyncio.create_task(self._after_debounce(sid))

    # ---------------- 闸门 ----------------

    async def _allowed(self, msg: IncomingMessage) -> bool:
        w = self.cfg.whitelist
        if msg.message_type != "private":
            if w.groups:
                return msg.group_id in w.groups
            # groups 留空 = 任何群。第一次见到某个群时打一行带群号的日志，
            # 免得用户不知道该怎么把范围收窄。
            if msg.group_id not in self._grp_seen:
                self._grp_seen.add(msg.group_id)
                log.info(
                    "收到群 %s 的消息。要只在这个群活动，就把 %s 填进 config.yaml 的 "
                    "whitelist.groups；群里默认只在被 @ 或叫名字时回（group.address_required）",
                    msg.group_id, msg.group_id,
                )
            return True

        # 1) 配置里点名的
        if msg.user_id in w.private:
            return True
        # 2) 之前已经认下的
        if await self.store.allowlist_has(msg.user_id):
            return True

        limit = int(w.auto_accept_first or 0)
        # 3) 开箱即用：配置里没写人、又还没绑满，就认下第一个私聊的人。
        #    不这么做的话，第一次跑起来「谁说话都不理」，用户会以为坏了。
        if limit > 0 and not w.private and await self.store.allowlist_count() < limit:
            await self.store.allowlist_add(msg.user_id, msg.nickname)
            log.info(
                "还没绑定过任何人，已认下第一个私聊的：%s（%d）。以后只理这一个人；"
                "想加别人就往 config.yaml 的 whitelist.private 里写 QQ 号，"
                "想彻底关掉筛选就把 whitelist.auto_accept_first 设成 0",
                msg.nickname or "这位", msg.user_id,
            )
            return True
        # 4) 开放模式：没写白名单、又关掉了自动绑定 —— 谁都可以
        if limit <= 0 and not w.private:
            return True

        log.info("不在白名单，忽略私聊 %d（要放行就把 QQ 号加进 whitelist.private）", msg.user_id)
        return False

    # ---------------- 视频推送指令 ----------------

    CMD = "视频推送"

    def command_action(self, text: str) -> Optional[str]:
        """把消息认成指令就返回动作，认不出返回 None。

        只认「视频推送」打头（允许前面有 @机器人、允许 # 或 / 开头），
        所以群里聊天提到「视频推送」这四个字不会被误触。
        """
        t = (text or "").strip()
        t = re.sub(r"^(?:@\d+\s*)+", "", t)      # 去掉 @123456789 这类前缀
        t = t.lstrip("#/／").strip()
        if not t.startswith(self.CMD):
            return None
        rest = t[len(self.CMD):].strip(" \t:：,，。.!！")
        if rest in ("", "状态", "情况"):
            return "status"
        if rest in ("开", "开启", "打开", "启用", "on", "1"):
            return "on"
        if rest in ("关", "关闭", "关掉", "停", "停止", "off", "0"):
            return "off"
        if rest in ("推送一次", "推一次", "现在就推", "现在推", "立即", "马上", "跑一次"):
            return "now"
        return "help"

    def _owner_check(self, msg: IncomingMessage) -> Tuple[bool, str]:
        masters = []
        for x in (self.cfg.whitelist.masters or []):
            try:
                masters.append(int(x))
            except (TypeError, ValueError):
                continue
        if msg.user_id in masters:
            return True, ""
        if not masters:
            # 单人部署：没设主人时，私聊的那个人就是主人。
            # 群里不认这个默认值 —— 不然群里任何人都能关掉推送。
            if msg.message_type == "private":
                return True, ""
            return False, ("群里改开关，得先在 config.yaml 的 whitelist.masters "
                           "里写上主人的 QQ 号。")
        return False, "这个只有主人能改。"

    async def _say(self, msg: IncomingMessage, text: str) -> None:
        try:
            await self.adapter.send_text(msg, text)
        except Exception:
            log.exception("视频推送指令：回复失败")

    def _videopush_status(self) -> str:
        vp = self.cfg.videopush
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            today = self.store.vpush_count_since(start, ("sent", "ready", "pending"))
        except Exception:
            today = 0
        topics = "、".join(t.keyword for t in parse_topics(vp.topics)) or "（没配）"
        priv = "、".join(str(u) for u in push_targets(self.cfg)) or "（没配）"
        grp = "、".join(str(g) for g in group_targets(self.cfg)) or "（没订阅）"
        return "\n".join([
            f"视频推送：{'开' if vp.enabled else '关'}",
            f"话题：{topics}",
            f"时间：{'、'.join(vp.times) or '（没配）'}｜今天已推 {today}/{vp.max_per_day}",
            f"收件人：私聊 {priv}｜群 {grp}",
        ])

    async def _handle_command(self, msg: IncomingMessage) -> bool:
        action = self.command_action(msg.text or "")
        if action is None:
            return False
        ok, why = self._owner_check(msg)
        if not ok:
            log.info("视频推送指令被拒：uid=%s 群=%s 动作=%s", msg.user_id,
                     msg.group_id, action)
            await self._say(msg, why)
            return True
        vp = self.cfg.videopush
        gid = int(msg.group_id) if (msg.message_type == "group" and msg.group_id) else 0
        try:
            if action == "on":
                set_enabled(self.cfg, True)
                extra = ""
                if gid:
                    add_group(self.cfg, gid)
                    extra = f"这个群（{gid}）也订上了，总结会发到群里；私聊那边照常。"
                left = max(0, int(vp.max_per_day) - int(
                    self.store.vpush_count_since(
                        datetime.now().replace(hour=0, minute=0, second=0,
                                               microsecond=0).timestamp(),
                        ("sent", "ready", "pending"))))
                names = "、".join(t.keyword for t in parse_topics(vp.topics)) or "没配话题"
                await self._say(msg, f"好，视频推送开着。{extra}今天的额度还剩 {left} 条，"
                                     f"到点我按「{names}」搜一轮。")
            elif action == "off":
                if gid:
                    remove_group(self.cfg, gid)
                    if not push_targets(self.cfg) and not group_targets(self.cfg):
                        set_enabled(self.cfg, False)
                        await self._say(msg, "好，这个群是最后一个收件人，视频推送整体关了。"
                                             "想再开就发「视频推送 开」。")
                    else:
                        await self._say(msg, "好，这个群不再收推送了，别处照常。")
                else:
                    set_enabled(self.cfg, False)
                    await self._say(msg, "好，视频推送关了。已经攒着的笔记先不发；"
                                         "想继续就发「视频推送 开」。")
            elif action == "now":
                if not vp.enabled:
                    await self._say(msg, "现在是关着的，先发「视频推送 开」。")
                else:
                    flag = self.cfg.db_path().parent / NOW_FLAG
                    flag.parent.mkdir(parents=True, exist_ok=True)
                    flag.write_text(str(time.time()), encoding="utf-8")
                    await self._say(msg, "好，这就去搜一轮，挑中的会在这里发（几分钟）。")
            elif action == "status":
                await self._say(msg, self._videopush_status())
            else:
                await self._say(msg, "用法：视频推送 开 / 关 / 状态 / 现在就推。"
                                     "群里发「开」会把本群加进收件人。")
        except Exception as e:
            log.exception("视频推送指令执行失败：%s", action)
            await self._say(msg, f"没改成：{e}。这次不算数，可以再试一次。")
        return True

    def _group_addressed(self, msg: IncomingMessage) -> bool:
        """是不是在跟它说话（@ 它，或者叫它的名字）。"""
        text = msg.text or ""
        if self.adapter.self_id and f"@{self.adapter.self_id}" in text:
            return True
        name = self.cfg.talk_name(group=True)
        if name and name in text:
            return True
        if any(
            seg.get("type") == "at"
            and str((seg.get("data") or {}).get("qq")) == str(self.adapter.self_id)
            for seg in (msg.raw.get("message") or []) if isinstance(seg, dict)
        ):
            return True
        return False

    def _group_slot_free(self, msg: IncomingMessage, *, addressed: bool) -> bool:
        """群内限流。三层：整群冷却 → 这个人冷却 → 每小时总上限。

        被点名的情况记 info（用户可能正在等回复，得知道为什么没回），
        没点名就记 debug（那种消息本来就该被忽略，不值得刷屏）。
        """
        g = self.cfg.group
        now = time.time()
        sid = msg.session_id
        level = log.info if addressed else log.debug

        gap = now - self._grp_last_reply.get(sid, 0.0)
        if gap < g.reply_cooldown_seconds:
            level("群里刚回过（%.0fs 前），这条先不接", gap)
            return False

        ukey = f"{sid}|{msg.user_id}"
        ugap = now - self._grp_last_user.get(ukey, 0.0)
        if ugap < g.per_user_cooldown_seconds:
            level("%s 刚被回过（%.0fs 前），等等再说", msg.nickname or msg.user_id, ugap)
            return False

        hour_key = int(now // 3600)
        stored_hour, count = self._grp_hour.get(sid, (hour_key, 0))
        if stored_hour != hour_key:
            stored_hour, count = hour_key, 0
            self._grp_hour[sid] = (stored_hour, count)
        if count >= g.max_replies_per_hour:
            log.info("这个群这一小时已经回了 %d 条，到量了，下个小时再理", count)
            return False
        return True

    def _group_should_reply(self, msg: IncomingMessage) -> bool:
        g = self.cfg.group
        if not g.enabled:
            return False
        addressed = self._group_addressed(msg)
        if not addressed:
            if g.address_required:
                return False
            # 老的随机插话行为，只有显式关掉 address_required 才走这里
            if random.random() >= self.cfg.whitelist.group_reply_rate:
                return False
        return self._group_slot_free(msg, addressed=addressed)

    def _mark_group_reply(self, msg: IncomingMessage) -> None:
        """记一笔"刚在这个群回了话"，供上面三层限流使用。"""
        now = time.time()
        sid = msg.session_id
        self._grp_last_reply[sid] = now
        self._grp_last_user[f"{sid}|{msg.user_id}"] = now
        hour_key = int(now // 3600)
        stored_hour, count = self._grp_hour.get(sid, (hour_key, 0))
        if stored_hour != hour_key:
            stored_hour, count = hour_key, 0
        self._grp_hour[sid] = (stored_hour, count + 1)

    def _clean_group_text(self, text: str) -> str:
        """去掉 @机器人的那串 id，顺带把空白压平。

        `@10001 在吗` → `在吗`。留着那串数字对模型没意义，还会让
        humanize 的本地规则（正则锚定 `^在吗$`）匹配不上，白丢一次省钱机会。
        """
        raw = text or ""
        had_at = False
        if self.adapter.self_id:
            marker = f"@{self.adapter.self_id}"
            if marker in raw:
                had_at = True
            raw = raw.replace(marker, " ")
        cleaned = " ".join(raw.split())
        if not cleaned and had_at:
            return "（只是叫了你一声，没说话）"
        return cleaned

    def _trivial_reply(self, text: str) -> Optional[str]:
        t = (text or "").strip()
        for pat, reply in self._trivial:
            if pat.search(t):
                return reply
        return None

    # ---------------- 什么时候回 ----------------

    def _reply_due_at(self, last: Optional[IncomingMessage] = None) -> Tuple[float, str]:
        """(到点时间戳, 原因)。0 表示立刻就回。"""
        now = datetime.now(CST)
        quiet = self.life.quiet_delay(now)
        if quiet > 0:
            return time.time() + quiet, "安静时段（睡了）"
        # 「随机晚回」是用来演「人不是随时守着手机」的，但它有 8% 概率把回复
        # 拖到 25 分钟后。对方正在等的情况下（明确要语音、被点名问话）这就是
        # 故障而不是拟人 —— 踩过：要语音之后「安静」了三分钟，用户以为卡死了。
        if last is not None and speech_requested(last.text):
            return 0.0, ""
        if random.random() < self.cfg.humanize.late_reply_chance:
            secs = random.uniform(self.cfg.humanize.late_reply_min,
                                  self.cfg.humanize.late_reply_max)
            return time.time() + secs, "随机晚回"
        return 0.0, ""

    async def _after_debounce(self, sid: str) -> None:
        try:
            await asyncio.sleep(self.cfg.humanize.debounce_seconds)
        except asyncio.CancelledError:
            return  # 又来新消息了，重新计时

        msgs = self._buffers.pop(sid, [])
        self._timers.pop(sid, None)
        if not msgs:
            return

        due, reason = self._reply_due_at(msgs[-1])
        if due <= 0:
            await self._run(sid, msgs)
            return

        # 关键：**不能占着会话锁睡觉**。凌晨那条消息要是把锁按住 6 小时，
        # 后面每条消息都会排在它后面各睡一次，回复时间越拖越离谱。
        # 所以挂成一个不持锁的独立任务，期间来的消息继续往 _buffers 里攒，
        # 醒了一次性回完。
        self._buffers[sid] = msgs
        task = self._deferred.get(sid)
        if task and not task.done():
            return
        self._deferred[sid] = asyncio.create_task(self._deferred_reply(sid, due, reason))

    async def _deferred_reply(self, sid: str, due: float, reason: str) -> None:
        wait = max(0.0, due - time.time())
        log.info("%s：%s，%.1f 分钟后再回（消息挂着，不占锁）", sid, reason, wait / 60)
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        self._deferred.pop(sid, None)
        msgs = self._buffers.pop(sid, [])
        if not msgs:
            return
        await self._run(sid, msgs)

    async def _run(self, sid: str, msgs: List[IncomingMessage]) -> None:
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            try:
                await self._process(sid, msgs)
            except Exception:
                log.exception("处理 %s 时炸了", sid)

    # ---------------- 联网搜索（P5） ----------------

    async def _chat_with_tools(
        self, messages: List[Dict[str, Any]], *, sid: str, max_tokens: int,
    ) -> ChatResult:
        """带工具的一轮回复。

        模型不调工具就一次返回（跟以前完全一样）；
        调了就先执行工具、把结果喂回去再问一轮，最多 max_tool_rounds 轮。

        ★ 为什么要模型自己决定，而不是每句都去搜：
          每句都搜等于每句多花一轮请求的钱，而且会把闲聊变成念搜索结果。
          工具描述里已经写清「日常闲聊别调」。
        """
        if not getattr(self.cfg.llm, "web_search", False):
            return await self.llm.chat(messages, purpose="chat", session_id=sid,
                                       max_tokens=max_tokens)

        conv: List[Dict[str, Any]] = list(messages)
        result = await self.llm.chat(conv, purpose="chat", session_id=sid,
                                     max_tokens=max_tokens, tools=WEB_TOOLS)

        rounds = max(0, int(getattr(self.cfg.llm, "max_tool_rounds", 2)))
        used = 0
        while result.tool_calls and used < rounds:
            used += 1
            # 带 tool_calls 的 assistant 消息必须原样放回去，
            # 否则后面的 tool 消息找不到主人，接口会直接 400。
            conv.append(result.assistant_message)
            for tc in result.tool_calls:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                log.info("联网工具：%s(%s)", name, json.dumps(args, ensure_ascii=False)[:100])
                try:
                    out = await run_web_tool(name, args)
                except Exception as e:                      # noqa: BLE001
                    out = f"工具执行失败（{type(e).__name__}）。"
                conv.append({"role": "tool",
                             "tool_call_id": str(tc.get("id") or name),
                             "content": out})
            result = await self.llm.chat(conv, purpose="search", session_id=sid,
                                         max_tokens=max_tokens, tools=WEB_TOOLS)

        # 轮次用完还在要工具：去掉工具再问最后一次，逼它开口说话。
        # 否则这一轮会返回空内容，直接变成「不回话」。
        if result.tool_calls and not result.text.strip():
            conv.append(result.assistant_message)
            for tc in result.tool_calls:
                conv.append({"role": "tool",
                             "tool_call_id": str(tc.get("id") or ""),
                             "content": "（搜索次数用完了，直接用你已知的信息回答。）"})
            result = await self.llm.chat(conv, purpose="search", session_id=sid,
                                         max_tokens=max_tokens)
        return result

    # ---------------- 主流程 ----------------

    async def _process(self, sid: str, msgs: List[IncomingMessage]) -> None:
        last = msgs[-1]
        is_group = sid.startswith("group:")

        if is_group:
            # 群里必须逐条标出「谁说的」。合并成一坨的话，模型会把 A 的话
            # 当成 B 的，回复就张冠李戴（这是群聊最典型的翻车方式）。
            merged = format_group_lines(
                [(m.nickname, self._clean_group_text(m.text)) for m in msgs])
        else:
            merged = "\n".join(m.text for m in msgs if m.text).strip()
        if not merged:
            return

        # ---- 1. 琐碎输入本地打发，一次模型都不调 ----
        # 群里拿「最后一条的干净文本」去匹配：merged 每行带「昵称：」前缀，
        # 拿它去对 `^在吗$` 永远对不上，会白白丢掉这个 0 token 的机会。
        canned = self._trivial_reply(
            self._clean_group_text(last.text) if is_group else merged)
        if canned is not None:
            log.info("本地规则回复，省下一次 API 调用")
            # 本地规则**也要存包装后的原文**，理由和主路径一样（见 _persist）：
            # 历史回放时必须逐字节一致才能命中缓存，而且 strip_wrapper 靠
            # 【群里】/【他刚说】标记才能正确剥出内容喂给记忆抽取器。
            self._mood[sid] = self.life.mood()
            wrapped_canned = wrap_user_message(merged, group=is_group,
                                               mood=self._mood.get(sid) or None)
            await self._persist(wrapped_canned, canned, sid, display_user=merged)
            await self._send_humanized(last, canned)
            if is_group:
                self._mark_group_reply(last)
            await self._remember(sid)
            return

        waited = await self.budget.wait_slot(sid)
        if waited > 0:
            log.debug("触发最小间隔，等了 %.2f 秒", waited)

        mode = await self.budget.mode()
        if mode == "stop":
            log.error("已达硬性预算上限，本轮不回复（不改配置就不会恢复）")
            return

        params = await self.budget.params()
        # 群聊上下文给得更紧：群里人多、话题散，记得越多越贵且越容易串味
        hist_budget = params["history_tokens"]
        if is_group and self.cfg.group.history_tokens > 0:
            hist_budget = min(hist_budget, self.cfg.group.history_tokens)

        # ---- 2. 需要的话先压缩历史（块式重建，付一次 cache miss 换长期命中） ----
        compressed = False
        try:
            if await self.ctx.needs_compression(sid, hist_budget):
                compressed = await self.ctx.compress(sid)
        except Exception:
            log.exception("压缩历史失败，继续用现有上下文")

        # ---- 3. 长期记忆召回（纯本地，0 token） ----
        recall: List[str] = []
        try:
            recall = await self.memory.recall(sid, merged)
        except Exception:
            log.exception("长期记忆召回失败，这轮不带旧事")

        # ---- 4. 组装上下文 ----
        self._mood[sid] = self.life.mood()
        wrapped = wrap_user_message(merged, group=is_group,
                                    mood=self._mood.get(sid) or None,
                                    recall=recall or None)
        try:
            messages, stats = await self.ctx.build(sid, wrapped,
                                                   history_tokens=hist_budget)
        except Exception:
            log.exception("组装上下文失败")
            return

        # ---- 5. 调模型 ----
        t0 = time.perf_counter()
        try:
            result = await self._chat_with_tools(
                messages, sid=sid, max_tokens=params["max_tokens"])
        except Exception as e:
            log.error("调用模型失败：%s", e)
            return
        elapsed = time.perf_counter() - t0

        reply = sanitize(result.text)
        if not reply:
            log.warning("模型返回空内容，跳过")
            return

        log.info(
            "回复生成：输入 %d tok（命中 %.0f%%）输出 %d tok，%.5f 元，%dms｜上下文 %d tok / %d 条历史",
            result.prompt_tokens, result.hit_rate * 100, result.output_tokens,
            result.cost_yuan, int(elapsed * 1000),
            stats["est_tokens"], stats["history_msgs"],
        )
        if recall:
            # 本地检索不花 token，但值得单独打出来：以后觉得"它怎么突然提这个"
            # 或者"它怎么忘了"，看一眼这行就知道是哪条记忆被捞出来了。
            log.info("想起 %d 条旧事：%s", len(recall),
                     "；".join(r[:20] for r in recall))

        # ---- 6. 落库 + 拟人化发送 ----
        await self._persist(wrapped, reply, sid, display_user=merged)
        await self._send_humanized(last, reply)
        if is_group:
            self._mark_group_reply(last)

        # ---- 7. 顺手抽一次长期记忆（低频；压缩过就一定要抽） ----
        if compressed:
            try:
                await self.memory.extract(sid)
            except Exception:
                log.exception("记忆抽取失败")
        else:
            await self._remember(sid)

    async def _remember(self, sid: str) -> None:
        try:
            await self.memory.extract_if_due(sid)
        except Exception:
            log.exception("记忆抽取失败")

    async def _persist(self, user_content: str, assistant_content: str, sid: str,
                       display_user: Optional[str] = None) -> None:
        """user 那一侧存的是"实际发出去的原文"（含动态包装）。

        为什么存包装后的版本而不是干净版：下一轮要把历史**原样回放**给 API，
        前缀必须和上一轮逐字节一致才能命中缓存。存干净版再重新包装，
        时间戳一变前缀就废了，缓存命中率会掉到 0。
        """
        try:
            await self.store.add_message(sid, "user", user_content, display_user)
            await self.store.add_message(sid, "assistant", assistant_content, assistant_content)
        except Exception:
            log.exception("写库失败")

    # ---------------- 拟人化发送 ----------------

    async def _send_humanized(self, msg: IncomingMessage, text: str,
                              *, skip_read_delay: bool = False) -> None:
        # 先问一句「这条要不要用语音发」。判断全在本地，不花 token。
        if self.cfg.tts.enabled:
            state = await self._try_voice(msg, text, skip_read_delay=skip_read_delay)
            if state == "sent":
                return
            if state == "fallback":
                # 已经等过「读消息」的时间了，文字别再等一遍
                skip_read_delay = True

        # 群里默认只发一条：连发三条在小群里非常扎眼，而且没有意义
        cap = self.cfg.group.max_segments if msg.session_id.startswith("group:") else None
        try:
            await send_humanized(self.cfg, self.adapter, msg, text,
                                 skip_read_delay=skip_read_delay,
                                 max_segments=cap, log=log)
        except Exception:
            log.exception("发送失败")

    async def _try_voice(self, msg: IncomingMessage, text: str,
                         *, skip_read_delay: bool = False) -> str:
        """试着发语音。返回 sent / fallback / no。

        fallback 的意思是「本来要发语音，但合成或发送失败了」—— 调用方据此
        改走文字路径，并且不再重复等读消息的延迟。
        """
        tts = self.cfg.tts
        is_group = msg.session_id.startswith("group:")
        requested = speech_requested(msg.text)
        always = bool(getattr(tts, "always_voice", False))

        # 群里默认不主动发语音：一条语音弹出来，全群都得听。
        # 全语音模式是用户按下的按钮，那条路也放行（面板上会二次确认）。
        if is_group and not tts.group and not requested and not always:
            return "no"
        if not should_speak(tts, msg.text, text):
            if reply_denies_voice(text):
                # 提示词和实际能力脱节了：模型在说「我发不了语音」，而这条正被要求发语音。
                # 发出去就是「一条语音告诉你我不会发语音」。这里拦住，并且把日志写得
                # 足够显眼 —— 看到这行说明 context.VOICE_RULES 没生效或者人设压过了它。
                log.warning("这条回复在否认自己能发语音，不转语音条：%s", text[:60])
            return "no"

        now = time.time()
        if now < self._voice_down_until:
            log.info("语音服务刚失败过，%.0f 秒内改发文字", self._voice_down_until - now)
            return "no"

        if not skip_read_delay:
            # 真人发语音前也会先愣一下；合成那两三秒正好当「录制」时间
            await asyncio.sleep(read_delay(self.cfg.humanize))

        timeout = float(tts.timeout or 60.0)
        try:
            # 先探端口：服务没开时不能等满 timeout，那是握着会话锁在等，
            # 后面每条消息都会排在它后面 —— 表现就是「整个 bot 卡住了」。
            if not service_alive(tts.api_base):
                raise RuntimeError(f"语音服务没在跑（{tts.api_base}），"
                                   "先在面板「声音」页签点启动")
            available = set(load_emotions(Path(tts.install_dir) / "refs").keys())
            emotion = guess_emotion(text, available) or (tts.default_emotion or "")
            # 再兜一层硬超时：urllib 的超时在个别网络栈下不一定准时生效
            nbytes = await asyncio.wait_for(
                speak(tts, self.adapter, msg, text, emotion=emotion),
                timeout=timeout + 10,
            )
        except Exception as e:
            self._voice_down_until = time.time() + 120
            log.warning("语音没发出去（%s），这条改发文字", e)
            return "fallback"

        log.info("→ %s: [语音 %d KB] %s", msg.session_id,
                 max(1, nbytes // 1024), text[:40])
        return "sent"
