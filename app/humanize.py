"""拟人化：让机器人的输出"不像 AI"。

分两半：
  - sanitize：把模型吐出来的书面语/AI 腔洗掉（本地正则，不额外花钱）
  - pacing：读延迟、打字时长、分条间隔（纯本地，不额外花钱）
两半都不调用模型 —— 所有"洗稿"都本地做，这是省钱的关键纪律。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from typing import List, Optional

from .config import HumanizeConfig

# ---------------- 输出清洗 ----------------

# AI 味的固定句式，出现就删掉整句
_AI_PHRASES = [
    r"希望(这|以上)?(对你|能)?(有所)?帮助[。！!~～]?",
    r"有(什么|任何)(其他|别的)?(问题|需要|可以帮)[^。！？!?]*[。！？!?]?",
    r"作为(一个)?(AI|人工智能|语言模型)[^。！？!?]*[。！？!?]?",
    r"我(只)?是(一个)?(AI|人工智能|语言模型)[^。！？!?]*[。！？!?]?",
    r"感谢(你的)?(提问|咨询|信任)[。！!~～]?",
    r"总之[，,]?",
    r"综上(所述)?[，,]?",
    r"首先[，,]?",
    r"其次[，,]?",
    r"最后[，,]?",
    r"另外[，,]?",
    r"需要(注意|强调)的是[，,]?",
    r"值得(注意|一提)的是[，,]?",
]

_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), ""),        # 代码块
    (re.compile(r"`([^`]*)`"), r"\1"),           # 行内代码
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),     # 粗体
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.M), ""),       # 标题
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),     # 无序列表
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),   # 有序列表
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # 链接
    (re.compile(r"^\s*>\s?", re.M), ""),         # 引用
    (re.compile(r"^\s*[-=]{3,}\s*$", re.M), ""), # 分割线
    (re.compile(r"^\s*\|.*\|\s*$", re.M), ""),   # 表格
]

_AI_RE = [re.compile(p) for p in _AI_PHRASES]


def sanitize(text: str, *, max_chars: int = 300) -> str:
    """把一段模型输出洗成"人在 QQ 上打字"的样子。纯本地，零 token。"""
    if not text:
        return ""

    t = text.strip()

    # 去掉思考模式可能残留的思维链标记（万一 thinking 被打开了）
    t = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", t, flags=re.S | re.I)
    t = re.sub(r"^\s*(思考过程|推理过程)[:：].*$", "", t, flags=re.M)

    for pat, rep in _MD_PATTERNS:
        t = pat.sub(rep, t)

    for pat in _AI_RE:
        t = pat.sub("", t)

    # 破折号 / 书名号式书面语
    t = t.replace("——", "，").replace("—", "，")
    t = t.replace("……", "…").replace("...", "…")

    # 收缩空白与多余换行
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    t = "\n".join(line.strip() for line in t.split("\n") if line.strip())
    t = t.strip(" \t\n")

    # 全角标点收敛：句尾的"。"在聊天里往往不加
    if len(t) > 1 and t.endswith("。"):
        t = t[:-1]

    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + "…"

    return t.strip()


def normalize_for_compare(text: str) -> str:
    """用于去重/自检的归一化：去掉空白与标点，便于判断"是不是在重复同一句"。"""
    t = unicodedata.normalize("NFKC", text or "")
    stripped = "".join(ch for ch in t if not unicodedata.category(ch).startswith(("P", "Z")))
    return stripped


# ---------------- 节奏 ----------------

def read_delay(cfg: HumanizeConfig) -> float:
    """看到消息到"开始打字"之间的延迟。"""
    return random.uniform(cfg.read_delay_min, cfg.read_delay_max)


def typing_duration(text: str, cfg: HumanizeConfig) -> float:
    """模拟手机打字耗时：按字数算，再夹到合理区间。"""
    per_char = random.uniform(cfg.typing_per_char_min, cfg.typing_per_char_max)
    dur = len(text) * per_char
    return max(cfg.typing_min, min(cfg.typing_max, dur))


def segment_gap(cfg: HumanizeConfig) -> float:
    return random.uniform(cfg.segment_gap_min, cfg.segment_gap_max)


def split_segments(text: str, cfg: HumanizeConfig,
                   *, max_segments: Optional[int] = None) -> List[str]:
    """把长回复切成 1~N 条短消息。真人不会一次性甩 200 字。

    max_segments 用来单独压上限：群里默认只发 1 条（连发在群里很显眼）。
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= cfg.segment_max_len:
        return [text]

    # 优先按换行，其次按句末标点，最后按长度硬切
    parts: List[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= cfg.segment_max_len:
            parts.append(para)
            continue
        buf = ""
        for chunk in re.split(r"(?<=[。！？!?…~～])", para):
            if not chunk:
                continue
            if len(buf) + len(chunk) <= cfg.segment_max_len:
                buf += chunk
            else:
                if buf:
                    parts.append(buf)
                buf = chunk
        if buf:
            parts.append(buf)

    cap = max(1, int(max_segments or cfg.segment_max_count))
    if len(parts) > cap:
        # 超出的部分合并回最后一条，避免刷屏（也避免被当成机器人）
        keep = parts[: cap - 1]
        keep.append("".join(parts[cap - 1:]))
        parts = keep

    return [p.strip() for p in parts if p.strip()]


# ---------------- 发送 ----------------

async def send_humanized(cfg, adapter, msg, text: str,
                         *, skip_read_delay: bool = False,
                         max_segments: Optional[int] = None,
                         log: Optional[logging.Logger] = None) -> int:
    """把一段回复拆成几条、按人的节奏发出去。返回实际发了几条。

    「安静时段」不在这里判 —— 那是 app/life.py 的活。这里只管
    「先愣一下 → 打字 → 发 → 喘口气 → 再发」。
    """
    h = cfg.humanize
    segments = split_segments(text, h, max_segments=max_segments)
    if not segments:
        return 0

    if not skip_read_delay:
        await asyncio.sleep(read_delay(h))

    sent = 0
    for i, seg in enumerate(segments):
        await asyncio.sleep(typing_duration(seg, h))
        await adapter.send_text(msg, seg)
        sent += 1
        if log:
            log.info("→ %s: %s", msg.session_id, seg.replace("\n", " ")[:60])
        if i < len(segments) - 1:
            await asyncio.sleep(segment_gap(h))
    return sent
