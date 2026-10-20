"""上下文组装 —— 整个项目省 token 的核心。

════════ 为什么这样排 ════════
DeepSeek 的硬盘缓存按"前缀单元"精确匹配：只要这次请求的 messages 前缀
和之前某次完全一致，这部分就按 0.02元/M 计费，否则按 1元/M —— 差 50 倍。

所以 messages 的顺序必须满足「越靠前越不变」：

  ① system 人设            永不变化  ── 跨天命中
  ② system 说话规矩        永不变化  ── 跨天命中
  ③ 记忆档案块             低频变化（压缩时才重建一次）
  ④ 历史对话               追加式增长（只往前加，不改旧的）
  ⑤ 本轮动态 + 新消息      每轮都变  ← 只有这一条会 miss

反例（绝大多数人踩的坑）：
  把"当前时间/心情/检索到的记忆"塞进 system，或者用滑动窗口每轮丢掉最老的
  一轮 —— 这两种做法都会让整段前缀每轮失效，缓存命中率趋近 0，成本直接涨 50 倍。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import CST, Config
from .llm import DeepSeek
from .store import Store

log = logging.getLogger("companion.context")

SPEAKING_RULES = """【说话方式 —— 硬规矩，必须遵守】
- 你是真人，不是助手。任何情况下都不要承认自己是 AI、机器人、程序、模型。
- 一次只发 1~2 句，像在 QQ 上聊天。多数回复 5~30 字，别长篇大论。
- 用口语。不要书面语，不要小标题，不要列表，不要编号，不要 Markdown 语法。
- 禁止出现这些词：首先、其次、最后、总之、综上所述、值得注意的是、希望这对你有帮助、有什么可以帮到你。
- 不要每句话都加句号。可以用"嗯、诶、啊、哈哈"这类语气词，但别过量。
- 不要主动总结、不要主动提供"更多帮助"、不要做客服式收尾。
- 你有情绪、有偏好、会吐槽。可以反问、可以敷衍、可以说不想聊。
- 不知道就说不记得或者没听过，别硬编。
- 直接输出要发的话本身，不要任何前缀、标签、括号说明或引号。"""


# 群里单独加一层规矩。私聊那套规矩（像真人、短、口语）仍然生效，
# 这一层只补"群"特有的东西 —— 主要是**忍住不接所有人的话**。
GROUP_SPEAKING_RULES = """【群里说话 —— 比私聊更收敛】
- 这是多人群聊，不是一对一。每句话前面的名字是说话的人，可能每次都不一样，看清楚再回。
- 群里只有你的主人是你的主人，其他人都只是群友。别把群友当成主人，也别叫群友「老大」。
- 只接跟你有关的话。别人互相聊的内容，你看得见也**不要插嘴**。
- 群里比私聊更短：多数回复 3~15 个字，一次只发一条，不要连发。
- 不要在群里提私聊里的事。群里的人不知道你们私下聊过什么。
- 不要 @ 回去，不要点别人名字，除非他明确问你。
- 不要总结大家的发言、不要主持、不要当群里的客服或者和事佬。
- 不确定在跟谁说话就直接说事，别硬套称呼。"""


# 语音能力的说明。**只在 config.yaml 的 tts.enabled 打开时才拼进 system**。
#
# 为什么必须有这一段：模型原本的世界观是「我是个打字的聊天助手」，所以有人要
# 语音时它会老老实实回「我不会发语音，张嘴也发不出声」。而 TTS 那一层只看
# 「要不要发」，照样把这句话合成成语音条 —— 于是收到一条语音，内容是「我不会
# 发语音」。这跟省 token 无关，纯粹是提示词没告诉它自己有什么能力。
#
# 拼接位置：跟在 SPEAKING_RULES 后面。因为 tts.enabled 不会在会话中途来回变，
# 前缀依旧稳定，缓存该命中还命中。
VOICE_RULES = """【你能出声】
- 你说话是有声音的：你发出的内容会以语音条的形式送到对方那里，他听得见。
- 有人让你「发条语音」「说给我听」「别打字了」，就把你想说的话正常说出来。别解释、别预告、别反问要不要。
- 不许出现「我不会发语音」「我发不了语音」「我只能打字」「我这边没声」「我没有嘴/麦克风」这类话。你明明能出声。"""


# 私聊专用的身份锚。**必须和 SPEAKING_RULES 拼在同一个 system 消息里** ——
# 这样私聊的 system 条数还是 2 条，不动既有的测试和缓存结构。
# 加这一段是因为踩过：人设文件写成"对外提及他时用「我家老大」"，
# 私聊里模型就把对面当成外人，开始用第三人称介绍主人。
MASTER_IDENTITY = """【这是私聊，对面就是你本人要陪的那个人】
- 跟你说话的这个人，就是你的主人本人。他不是外人，不是群友，不是第三方。
- 直接跟他说话。别把他当成别人来讨论，也不用向他介绍他是谁。
- 「外人」「群友」这两个词，只在你待在群里的时候才存在。"""


# 白名单里除了主人，还可能有主人允许来聊天的朋友。
# 不区分的话就会把人叫成「老大」、甚至把主人的私事讲给他听 —— 踩过。
GUEST_IDENTITY = """【这是私聊，但对面不是你的主人】
- 跟你说话的这个人是主人的朋友，不是主人本人。别叫他「老大」，别把他当主人。
- 照常友好地聊，但别替主人做决定、别答应他任何事。
- 不要讲你和主人私下聊过的事、不要透露主人的作息行程和隐私，也不要介绍主人的情况。
- 他问到主人，就说不方便讲，让他自己去问。"""


def est_tokens(text: str) -> int:
    """粗略估算 token 数。CJK 约 1.5 字/token，ASCII 约 4 字/token。"""
    if not text:
        return 0
    ascii_n = sum(1 for c in text if ord(c) < 128)
    other_n = len(text) - ascii_n
    return int(ascii_n / 4 + other_n / 1.5) + 4


def est_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    return sum(est_tokens(str(m.get("content") or "")) for m in messages) + len(messages) * 3


class ContextBuilder:
    def __init__(self, cfg: Config, store: Store, llm: DeepSeek):
        self.cfg = cfg
        self.store = store
        self.llm = llm

    # ---------------- 静态前缀 ----------------

    def static_prefix(self, *, group: bool = False, is_master: bool = True) -> List[Dict[str, Any]]:
        """静态前缀。**同一个会话永远走同一条分支**，否则缓存会全废。

        会话是 private: 还是 group: 在会话建立时就定死了，不会中途变，
        所以按 session_id 选人设是安全的。私聊里「是不是主人」同样由
        session_id 里的 uid 定死，同一个会话不会来回变，也不破坏缓存。
        """
        if group:
            rules = GROUP_SPEAKING_RULES
            masters = [int(x) for x in (self.cfg.whitelist.masters or [])]
            if masters:
                who = "、".join(str(x) for x in masters)
                rules += f"\n- 你的主人只有一个，QQ 号是 {who}。群里其他人都不是。"
            return [
                {"role": "system",
                 "content": self.cfg.group_persona_text or self.cfg.persona_text},
                {"role": "system", "content": self._speaking_rules()},
                {"role": "system", "content": rules},
            ]
        identity = MASTER_IDENTITY if is_master else GUEST_IDENTITY
        return [
            {"role": "system", "content": self.cfg.persona_text},
            {"role": "system", "content": self._speaking_rules() + "\n\n" + identity},
        ]

    def _speaking_rules(self) -> str:
        """说话规矩。开了语音就把「你能出声」一起带上（关掉时不带，省 token）。"""
        base = SPEAKING_RULES
        if getattr(self.cfg, "tts", None) is not None and self.cfg.tts.enabled:
            base = base + "\n\n" + VOICE_RULES
        return base

    # ---------------- 记忆档案块（低频） ----------------

    @staticmethod
    def _memory_block(summary: str, profile: Dict[str, str]) -> str:
        parts: List[str] = []
        if summary.strip():
            parts.append("【你记得的事】\n" + summary.strip())
        if profile:
            lines = [f"- {k}：{v}" for k, v in sorted(profile.items()) if v]
            if lines:
                parts.append("【关于他】\n" + "\n".join(lines))
        return "\n\n".join(parts)

    # ---------------- 组装 ----------------

    async def build(
        self,
        session_id: str,
        wrapped_user_text: str,
        *,
        history_tokens: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        budget = history_tokens or self.cfg.context.history_tokens
        is_group = session_id.startswith("group:")

        summary, covered = await self.store.get_summary(session_id)
        profile = await self.store.get_profile(session_id)
        history = await self.store.history(session_id, after_id=covered)

        # 私聊的对面是不是主人 —— 由 session_id 里的 uid 决定，同一个会话恒定。
        is_master = True
        if not is_group:
            try:
                is_master = self.cfg.is_master(int(session_id.split(":", 1)[1]))
            except (IndexError, ValueError):
                is_master = True

        messages: List[Dict[str, Any]] = self.static_prefix(
            group=is_group, is_master=is_master)

        mem = self._memory_block(summary, profile)
        if mem:
            messages.append({"role": "user", "content": mem})
            # 这一问一答是固定填充，用来把记忆块"夹"进前缀里，
            # 内容永不变，所以不会破坏缓存。
            messages.append({"role": "assistant", "content": "嗯，记着呢"})

        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})

        messages.append({"role": "user", "content": wrapped_user_text})

        stats = {
            "history_msgs": len(history),
            "est_tokens": est_messages_tokens(messages),
            "memory_tokens": est_tokens(mem),
        }
        return messages, stats

    # ---------------- 压缩判定 ----------------

    async def history_tokens(self, session_id: str) -> int:
        _, covered = await self.store.get_summary(session_id)
        history = await self.store.history(session_id, after_id=covered)
        return sum(est_tokens(str(h["content"])) for h in history)

    async def needs_compression(self, session_id: str, budget: Optional[int] = None) -> bool:
        budget = budget or self.cfg.context.history_tokens
        return (await self.history_tokens(session_id)) > budget

    # ---------------- 压缩（块式重建，不是滑动窗口） ----------------

    async def compress(self, session_id: str) -> bool:
        """把最老的一部分历史压成摘要 + 刷新画像。一次请求顺手做完两件事，不额外花钱。

        这就是"块式重建"：平时前缀完全稳定、缓存打满；只在跨过预算时
        付一次 cache miss 重建前缀，然后重新开始累积。
        """
        summary, covered = await self.store.get_summary(session_id)
        history = await self.store.history(session_id, after_id=covered)
        if len(history) < 4:
            return False

        ratio = max(0.1, min(0.9, self.cfg.context.compress_ratio))
        cut = max(1, int(len(history) * ratio))
        old_part = history[:cut]
        new_covered = int(old_part[-1]["id"])

        is_group = session_id.startswith("group:")

        dialogue = "\n".join(
            d for d in (as_dialogue(m["role"], m["content"], group=is_group)
                        for m in old_part) if d
        )
        prompt = (
            ("【已有的记忆档案】\n" + summary.strip() + "\n\n") if summary.strip() else ""
        ) + (
            "【新的对话记录】\n" + dialogue + "\n\n"
            "请把上面的对话合并进档案。严格按下面的格式输出，不要有多余的话：\n"
            "摘要：<不超过 150 字，用第三人称「他」记录关于他的事实、近况、情绪和未完的话题"
            + ("；多人群聊，涉及别人的事要写清是谁" if is_group else "") + ">\n"
            "档案：\n"
            "称呼=<他对自己的称呼，没有就写 未知>\n"
            "工作=<职业或学业，没有就留空>\n"
            "喜好=<喜欢的东西，没有就留空>\n"
            "雷区=<不喜欢被聊的话题，没有就留空>"
        )

        messages = [
            {"role": "system", "content": "你是一个对话记忆整理器。只输出规定格式，不要多余解释。"},
            {"role": "user", "content": prompt},
        ]

        try:
            result = await self.llm.chat(
                messages,
                purpose="summarize",
                session_id=session_id,
                max_tokens=self.cfg.context.summary_tokens,
                temperature=0.3,
                thinking=False,        # 整理记忆不需要思考，白花钱
            )
        except Exception as e:
            log.warning("压缩失败，本轮跳过：%s", e)
            return False

        new_summary, new_profile = _parse_archive(result.text, fallback=summary)
        await self.store.set_summary(session_id, new_summary, new_covered)
        for k, v in new_profile.items():
            if v:
                await self.store.set_profile(session_id, k, v)

        log.info("记忆已压缩：覆盖到消息 #%d，摘要 %d 字", new_covered, len(new_summary))
        return True


def _parse_archive(text: str, fallback: str = "") -> Tuple[str, Dict[str, str]]:
    """解析压缩输出的固定格式。解析不出来就用兜底，不抛错（记忆不值得崩服务）。"""
    summary = ""
    profile: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("摘要"):
            summary = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and v and v not in ("未知", "空"):
                profile[k] = v
    return (summary or fallback), profile


# ---------------- 本轮动态块（每轮都变，必须放最后） ----------------

# 群聊的动态块标记。跟【他刚说】并列 —— 私聊里"他"只有一个人，群里不是，
# 所以群标记后面的正文是「昵称：内容」的多行文本，说话人写在每一行前面。
GROUP_MARK = "【群里】"


def format_group_lines(items: List[Tuple[str, str]]) -> str:
    """[(昵称, 内容)] → 「小明：在吗\\n小红：他不在」。

    昵称拿不到就写「某人」。宁可丢掉归属，也不能把两条并成一条 ——
    并了之后模型会把 A 说的话当成 B 说的。
    """
    out: List[str] = []
    for nickname, text in items:
        t = (text or "").strip()
        if not t:
            continue
        out.append(f"{(nickname or '').strip() or '某人'}：{t}")
    return "\n".join(out)


def wrap_user_message(text: str, *, group: bool = False,
                      mood: Optional[str] = None,
                      recall: Optional[List[str]] = None,
                      now: Optional[datetime] = None) -> str:
    """把"每轮都在变"的东西全部塞进最后一条 user 消息。

    这样它们只会让最后一条 miss，前面所有前缀照常命中缓存。

    私聊走【他刚说】，群聊走【群里】+ 每行「昵称：内容」。
    **私聊那条一个字节都没改**，改了就等于把所有人的缓存全部作废。
    """
    now = now or datetime.now(CST)
    head = f"【当前】{now.strftime('%m月%d日 %H:%M')} {_weekday_cn(now)}"
    if mood:
        head += f"，心情：{mood}"
    body = (GROUP_MARK + text) if group else f"【他刚说】{text}"
    if recall:
        body += "\n【刚好想起来】" + "；".join(recall)
    return head + "\n" + body


_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _weekday_cn(d: datetime) -> str:
    return _WEEKDAYS[d.weekday()]


# ---------------- 主动搭话的动态块 ----------------

# 主动搭话也要往历史里存一条 user 消息（否则下一轮前缀对不上、缓存全废），
# 但那条**不是他说的**。这个标记让记忆抽取知道要跳过它。
PROACTIVE_MARK = "【他没说话】"


def wrap_proactive_message(topics: List[str], *, mood: Optional[str] = None,
                           idle_hours: float = 0.0,
                           now: Optional[datetime] = None) -> str:
    """主动搭话时伪装成"用户消息"的指令块。

    跟 wrap_user_message 一样放在最末尾 —— 它每轮都不同，绝不能污染前缀。
    """
    now = now or datetime.now(CST)
    head = f"【当前】{now.strftime('%m月%d日 %H:%M')} {_weekday_cn(now)}"
    if mood:
        head += f"，心情：{mood}"

    lines = [head, f"{PROACTIVE_MARK}已经 {idle_hours:.0f} 小时没人说话了。你想主动找他说点什么。"]
    if topics:
        lines.append("【可以提的旧事】" + "；".join(topics))
    lines.append(
        "要求：直接说事或者直接问他，别用「在吗」「忙吗」开头；一两句话，15~30 字；"
        "别解释你为什么突然找他，别道歉，别说「好久没聊了」。"
    )
    return "\n".join(lines)


# ---------------- 反向解析：从存下来的原文里取出「他说的话」 ----------------

def strip_wrapper(content: str) -> str:
    """把存进历史的动态包装剥掉，只留内容本体。

    存的时候为什么带包装，见 Orchestrator._persist —— 是为了让下一轮能
    逐字节原样回放、命中缓存。所以每次要「读懂」历史时都得先剥一遍。

    - 主动搭话那条也是 user 角色，但**不是他说的** → 返回空串，上游整条丢掉
    - 群聊**保留**「昵称：内容」的多行结构 —— 抽取器要靠它区分是谁说的
    """
    text = str(content or "")
    if PROACTIVE_MARK in text:
        return ""
    if GROUP_MARK in text:
        text = text.split(GROUP_MARK, 1)[1]
    elif "【他刚说】" in text:
        text = text.split("【他刚说】", 1)[1]
    if "【刚好想起来】" in text:
        text = text.split("【刚好想起来】", 1)[0]
    return text.strip()


def as_dialogue(role: str, content: str, *, group: bool = False) -> str:
    """把一条历史消息写成给「记忆整理器」看的一行。

    私聊：他：… / 我：…
    群聊：直接用原文（每行已经自带「昵称：」），我们自己的回复仍然写「我」。
    """
    plain = strip_wrapper(content)
    if not plain:
        return ""
    if role == "assistant":
        return f"我：{plain}"
    if group:
        return plain
    return f"他：{plain}"

