"""长期记忆（P3）：抽取、检索、去重。

════════ 为什么不接 embedding 模型 ════════
DeepSeek 官方**没有** embedding 接口（官方仓库有明确 issue：
deepseek-ai/DeepSeek-V3#806 "there is no dedicated embedding for any of the deep seek models"）。
要在本地跑 embedding 就得装 torch + sentence-transformers，几百 MB、冷启动慢，
对一个"陪我聊天"的小工具来说完全不成比例。

而聊天记忆的量级就是几百到几千条。这里用
    「中文字符二元组 + IDF + 时间衰减 + 重要度」
纯字面检索，**零 token、零延迟、零新依赖**，够用。

真正的语义泛化不靠检索 —— 靠摘要和画像（每次对话都在上下文里）。
召回只负责一件事：**他刚提到某个词时，把相关的那件旧事翻出来。**
这恰好是字面匹配最擅长的。

════════ 为什么每条记忆还带「关键词」 ════════
光靠字面匹配会漏。他说"甲方又让我改稿"，而旧记忆写的是"他在广告公司做设计"，
两边一个字都不重合，匹配度为 0。所以抽事实的时候让模型**顺手给出关键词**
（`关键词=改稿,甲方,加班`）—— 关键词进检索索引但不进正文，
等于免费得到了一个同义词桥。不额外花钱，因为跟事实是同一次调用产出的。

════════ 打分 ════════
    准入：sim >= min_sim
    排序：score = sim × 时间衰减 × 重要度
      sim      查询里的信息量有多少落在这条记忆上（0~1）
     时间衰减 0.5 ** (天数 / half_life_days)
     重要度   1~5 归一化到 0.52~1.0

★ 为什么 sim 是"覆盖率"而不是余弦 ★
中文二元组切出来一堆碎词（"甲方又让我改稿" → 甲方/方又/又让/让我/我改/改稿）。
余弦要除以两边的模长，一条 15 字的记忆模长很大，真正的相关会被这堆碎词稀释到 0.1 以下，
阈值就变得没法设。所以这里用：

    sim = 共现词元的 idf 之和 / 查询里"已知词元"的 idf 之和

"已知"= 这个词在记忆库里出现过。从没出现过的词匹配不到任何东西，
留在分母里只会拉低真正相关的那些词 —— 直接当 0。
这样 sim 有清晰的物理含义：**他这句话的信息量，有百分之多少被这条记忆接住了。**

★ 为什么 min_sim 只卡 sim，不卡最终 score ★
时间衰减会一直乘下去。要是拿 score 卡准入，一条半年前但完全对得上的记忆
（decay 已经掉到 0.1）就会被判为"不相关"丢掉 —— 那长期记忆就白做了。
衰减和重要度只该决定**排序**，决定"想起来哪几条"，不该决定"要不要想起来"。
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .context import PROACTIVE_MARK, as_dialogue, strip_wrapper
from .llm import DeepSeek
from .store import Store

log = logging.getLogger("companion.memory")

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9]{2,}")

EXTRACT_SYSTEM = "你是一个对话记忆整理器。只输出规定格式，不要任何解释、不要编号、不要空话。"

EXTRACT_PROMPT = """【最近对话】
{dialogue}

【已经记住的】
{known}

请从上面的对话里挑出**值得长期记住**的信息，关于"他"的稳定事实。
值得记：身份、职业、家人朋友、喜好厌恶、重要经历、约定好的事、正在持续的麻烦。
不值得记：寒暄、临时情绪、一次性的吃喝、你刚说过的话、上面【已经记住的】里已有的。
最多 {max_facts} 条。一条都没有就只输出：无

每条一行，严格按这个格式，不要有别的话：
事实=<不超过25字的一句话>|关键词=<3~6个词，逗号分隔，用来日后检索>|重要度=<1-5>

示例：
事实=他在一家广告公司做平面设计|关键词=广告,设计,上班,公司|重要度=4
事实=他养了只橘猫叫布丁|关键词=猫,布丁,宠物|重要度=3"""


# 群聊版。最大的区别是**每条事实都必须带说话人的名字** ——
# 不然几个人混在一起，记忆会串成一锅粥，回复就张冠李戴。
EXTRACT_PROMPT_GROUP = """【最近的群聊记录】
{dialogue}

【已经记住的】
{known}

这是一个多人群聊。前面每行「名字：内容」里的名字就是说话的人。

请挑出**值得长期记住**的信息，最多 {max_facts} 条。一条都没有就只输出：无

特别注意：
- 必须写清是谁的事，事实要以名字开头。写「小明在广告公司做平面设计」，
  不要写「他在广告公司做平面设计」。
- 不要把 A 说的话安到 B 头上。拿不准是谁说的，这条就别记。
- 只记群友本人的稳定信息：身份、职业、喜好、正在持续的麻烦。
- 不记寒暄、临时情绪、一次性的吃喝，也不记机器人自己说过的话。
- 上面【已经记住的】里已经有的，不要重复。

每条一行，严格按这个格式，不要有别的话：
事实=<不超过25字，以说话人的名字开头>|关键词=<3~6个词，逗号分隔>|重要度=<1-5>

示例：
事实=小明在一家广告公司做平面设计|关键词=小明,广告,设计,上班|重要度=4
事实=小红养了只橘猫叫布丁|关键词=小红,猫,布丁,宠物|重要度=3"""


# ---------------- 分词与打分 ----------------

def tokenize(text: str) -> List[str]:
    """中文切字符二元组，英文数字切词。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    toks: List[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    toks.extend(_WORD.findall(text))
    return toks


def _idf(docs: List[List[str]]) -> Dict[str, float]:
    """词元在记忆库里的稀有度。出现得越少，idf 越大，说明它越像"专属名词"。"""
    n = len(docs)
    df = Counter()
    for d in docs:
        df.update(set(d))
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def _sim(query_tokens: List[str], fact_tokens: List[str],
         idf: Dict[str, float]) -> float:
    """他这句话的信息量，有百分之多少被这条记忆接住了。"""
    q = {t for t in query_tokens if t in idf}
    if not q:
        return 0.0                      # 一句话里没有一个词是记忆库里见过的
    f = set(fact_tokens)
    known = sum(idf[t] for t in q)
    shared = sum(idf[t] for t in (q & f))
    return shared / known if known > 0 else 0.0


def _decay(created_at: float, now: float, half_life_days: float) -> float:
    age_days = max(0.0, (now - created_at) / 86400.0)
    return 0.5 ** (age_days / max(half_life_days, 0.1))


def _imp(importance: float) -> float:
    return 0.4 + 0.12 * max(0.0, min(5.0, float(importance or 1.0)))


# ---------------- 解析抽取结果 ----------------

def parse_facts(text: str) -> List[Tuple[str, List[str], float]]:
    """解析「事实=…|关键词=…|重要度=…」。解析不出来就跳过，不抛错。"""
    out: List[Tuple[str, List[str], float]] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•*0123456789. ").strip()
        if not line or line.startswith("无"):
            continue
        if "事实" not in line:
            continue
        content, keywords, imp = "", [], 3.0
        for part in line.split("|"):
            if "=" not in part and "：" not in part:
                continue
            k, v = re.split(r"[=：]", part, maxsplit=1)
            k, v = k.strip(), v.strip()
            if k.startswith("事实"):
                content = v
            elif k.startswith("关键词"):
                keywords = [w.strip() for w in re.split(r"[,，、\s]+", v) if w.strip()]
            elif k.startswith("重要度"):
                try:
                    imp = float(re.findall(r"\d+(?:\.\d+)?", v)[0])
                except (IndexError, ValueError):
                    imp = 3.0
        if content:
            out.append((content[:60], keywords[:8], max(1.0, min(5.0, imp))))
    return out


# ---------------- 主类 ----------------

class Memory:
    def __init__(self, cfg: Config, store: Store, llm: DeepSeek):
        self.cfg = cfg
        self.store = store
        self.llm = llm

    # ---------- 检索 ----------

    async def recall(self, session_id: str, query: str,
                     top_k: Optional[int] = None) -> List[str]:
        """给一句话，翻出相关的旧记忆。全程本地计算，不花一分钱 token。"""
        m = self.cfg.memory
        if not m.enabled or not query.strip():
            return []
        rows = await self.store.facts(session_id)
        if not rows:
            return []

        docs = [self._tokens_of(r) for r in rows]
        idf = _idf(docs)
        q = tokenize(query)
        now = time.time()

        scored: List[Tuple[float, float, int, str]] = []
        for row, toks in zip(rows, docs):
            sim = _sim(q, toks, idf)
            if sim < m.min_sim:
                continue
            rank = sim * _decay(row["created_at"], now, m.half_life_days) * _imp(row["importance"])
            scored.append((rank, sim, int(row["id"]), row["content"]))

        scored.sort(key=lambda x: -x[0])
        picked = scored[: top_k or m.top_k]
        if picked:
            await self.store.touch_facts([p[2] for p in picked])
            log.debug("召回 %d 条（最高 %.2f / sim %.2f）：%s",
                      len(picked), picked[0][0], picked[0][1],
                      [p[3][:14] for p in picked])
        return [p[3][: m.max_chars] for p in picked]

    @staticmethod
    def _tokens_of(row: Dict[str, Any]) -> List[str]:
        raw = row.get("tokens")
        if raw:
            try:
                toks = json.loads(raw)
                if isinstance(toks, list):
                    return [str(t) for t in toks]
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return tokenize(str(row.get("content") or ""))

    # ---------- 抽取 ----------

    async def extract_if_due(self, session_id: str) -> bool:
        """聊够轮数就抽一次。返回是否真的抽了。"""
        m = self.cfg.memory
        if not m.enabled:
            return False
        covered = await self.store.kv_get_int(f"mem_until:{session_id}", 0)
        turns = await self.store.count_turns(session_id, after_id=covered)
        if turns < m.extract_min_turns and turns < m.extract_every_turns:
            return False
        if turns < m.extract_every_turns and covered > 0:
            return False
        return await self.extract(session_id)

    async def extract(self, session_id: str) -> bool:
        """把最近的对话喂给模型，抽出几条长期事实。

        这是本项目里**唯一**一个"额外"的模型调用（压缩也是额外，但它顺便产出摘要）。
        所以它很低频：默认 20 轮一次，单次约 1000 输入 token。
        """
        m = self.cfg.memory
        covered = await self.store.kv_get_int(f"mem_until:{session_id}", 0)
        history = await self.store.history(session_id, after_id=covered)
        if len(history) < 2:
            return False

        is_group = session_id.startswith("group:")
        dialogue = "\n".join(
            d for d in (as_dialogue(h["role"], h["content"], group=is_group)
                        for h in history[-40:]) if d
        )
        known_rows = await self.store.facts(session_id)
        known = "\n".join(f"- {r['content']}" for r in known_rows[-30:]) or "（还没有）"

        prompt = (EXTRACT_PROMPT_GROUP if is_group else EXTRACT_PROMPT).format(
            dialogue=dialogue[:3000],
            known=known[:1200],
            max_facts=m.extract_max_facts,
        )
        try:
            res = await self.llm.chat(
                [{"role": "system", "content": EXTRACT_SYSTEM},
                 {"role": "user", "content": prompt}],
                purpose="memory",
                session_id=session_id,
                max_tokens=m.extract_max_tokens,
                temperature=0.2,
                thinking=False,          # 抽事实不需要思考
            )
        except Exception as e:            # 记忆抽失败不值得让聊天挂掉
            log.warning("记忆抽取失败，跳过本轮：%s", e)
            return False

        facts = parse_facts(res.text)
        added = 0
        for content, keywords, imp in facts:
            if await self._store_one(session_id, content, keywords, imp):
                added += 1

        newest = int(history[-1]["id"])
        await self.store.kv_set(f"mem_until:{session_id}", newest)
        await self.store.kv_set(f"mem_turns:{session_id}", 0)
        log.info("长期记忆：+%d 条（本轮抽到 %d 条），水位到消息 #%d", added, len(facts), newest)
        return True

    async def _store_one(self, session_id: str, content: str,
                         keywords: List[str], importance: float,
                         source_msg_id: Optional[int] = None) -> bool:
        """写一条记忆；跟已有的太像就更新它，不重复堆。"""
        m = self.cfg.memory
        content = (content or "").strip()
        if not content:
            return False
        toks = tokenize(content + " " + " ".join(keywords))
        if not toks:
            return False

        rows = await self.store.facts(session_id)
        if rows:
            docs = [self._tokens_of(r) for r in rows]
            idf = _idf(docs + [toks])
            best, best_row = 0.0, None
            for row, d in zip(rows, docs):
                sim = _sim(toks, d, idf)
                if sim > best:
                    best, best_row = sim, row
            if best >= m.dedup_threshold and best_row is not None:
                merged = list(dict.fromkeys(self._tokens_of(best_row) + toks))[:160]
                await self.store.update_fact(
                    int(best_row["id"]), content,
                    max(float(best_row["importance"]), importance),
                    json.dumps(merged, ensure_ascii=False))
                log.debug("记忆合并（相似度 %.2f）：%s", best, content[:20])
                return False

        await self.store.add_fact(session_id, content, importance,
                                  json.dumps(toks, ensure_ascii=False), source_msg_id)
        log.info("记住：%s", content)
        return True

    # ---------- 给主动搭话用 ----------

    async def topics(self, session_id: str, limit: int = 3) -> List[str]:
        """挑几条"可以主动提起"的旧事：重要度高、又很久没碰的优先。"""
        rows = await self.store.facts(session_id)
        if not rows:
            return []
        now = time.time()
        rows.sort(key=lambda r: -(
            _imp(r["importance"]) * _decay(r["created_at"], now, self.cfg.memory.half_life_days)
            * (1.0 / (1.0 + 0.6 * int(r["access_count"] or 0)))
        ))
        return [str(r["content"])[:40] for r in rows[:limit]]

    async def count(self, session_id: str) -> int:
        return len(await self.store.facts(session_id))


def _plain(content: str) -> str:
    """剥掉存进去的动态包装，只留内容本体。

    实现搬到 app/context.py 的 strip_wrapper 了 —— 那边同样要剥（压缩摘要时），
    而且群聊的「昵称：内容」结构必须由同一个函数保证一致。这里留个薄壳兼容老测试。
    """
    return strip_wrapper(content)
