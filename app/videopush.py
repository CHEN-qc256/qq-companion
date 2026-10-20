"""B 站视频搜索 → AI 筛选 → BiliNote 总结 → QQ 推送。

整条链路只干一件事：**每天在固定时间，替主人去 B 站找几条真值得看的视频，
生成笔记，私聊发过去。** 宁可少推，也不凑数。

分工：
  - BiliSearch   负责「搜得到」：WBI 签名 + 搜索接口（app/bilisearch.py）
  - 本模块的 AI   负责「筛得准」：带思考模式逐条判断，只留干货
  - BiliNote     负责「看得懂」：下载 → 转写 → LLM 生成 Markdown（app/bilinote.py）
  - OneBotAdapter 负责「送得到」：私聊发文本

去重是全局的：一个 bvid 推过就不再推；被 AI 毙掉的也会记下来，45 天后清掉
再给一次机会（不然同一批候选会被永久拉黑，池子越用越干）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bilinote import BiliNote, BiliNoteError
from .bilisearch import BiliRiskError, BiliSearch, BiliSearchError, VideoHit
from .config import Config, apply_hot, load_config
from .llm import DeepSeek
from .onebot import OneBotAdapter
from .store import Store

log = logging.getLogger("videopush")

KV_LAST_RUN = "videopush.last_run"
NOW_FLAG = ".videopush-now"

# 对话指令的开关状态直接落在 config.yaml 里（而不是数据库里另存一份），
# 这样面板、配置文件、对话三处看到的永远是同一个值，也不会出现
# 「面板关了但对话那边还有一个 override 在偷偷推」这种鬼故事。
CFG_ENABLED = "videopush.enabled"
CFG_GROUPS = "videopush.groups"

# 发布不满 3 天的新视频还没有播放量，min_play 对它不生效 —— 否则刚上传的
# 题解（实测有一条 0 天、568 秒、play=1 的）会被播放量门槛直接误杀。
FRESH_PLAY_GRACE_DAYS = 3.0

# 被 AI 毙掉的记录保留多少天；过期清掉，让老候选以后还有一次机会。
REJECTED_TTL_DAYS = 45


# ──────────────────────────────────────────────────────────────────────
#  话题与提示词
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Topic:
    keyword: str
    hint: str = ""


def parse_topics(raw: List[str]) -> List[Topic]:
    """把面板里的字符串列表解析成话题。格式：`关键词|筛选要求`，竖线可以省略。"""
    out: List[Topic] = []
    seen = set()
    for line in raw or []:
        text = str(line or "").strip()
        if not text or text.startswith("#"):
            continue
        for sep in ("|", "｜"):
            if sep in text:
                kw, hint = text.split(sep, 1)
                break
        else:
            kw, hint = text, ""
        kw, hint = kw.strip(), hint.strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        out.append(Topic(kw, hint))
    return out


# ──────────────────────────────────────────────────────────────────────
#  对话指令用的开关
# ──────────────────────────────────────────────────────────────────────
def _save_field(cfg: Config, path: str, value: Any) -> None:
    """把一项配置写回 config.yaml，并让当前进程立刻用上新值。

    不在数据库里另存一份「运行态开关」，是因为那样迟早会出现
    「面板关了、数据库说还开着」的分裂。这里只有 config.yaml 一个真源：
    面板读它、机器人热加载它、对话指令写它。
    """
    src = Path(str(getattr(cfg, "source_path", "") or ""))
    if not src or not src.exists():
        raise RuntimeError("找不到配置文件（source_path 为空），改不了开关")
    from panel import configio  # 延迟导入：只有真改配置时才需要
    raw = configio.load(src)
    configio.set_path(raw, path, value)
    configio.save(src, raw, backup=True)
    apply_hot(cfg, load_config(src))
    # 叫醒 main.py 的热更新循环：它看到标记会再读一次盘，别处的 cfg 也同步。
    try:
        (cfg.db_path().parent / ".panel-reload").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def push_targets(cfg: Config) -> List[int]:
    """私聊收件人：videopush.targets，没配就退到 whitelist.masters。"""
    raw = cfg.videopush.targets or cfg.whitelist.masters
    return _ids(raw)


def group_targets(cfg: Config) -> List[int]:
    """群收件人：在群里发过「视频推送 开」的那些群。"""
    return _ids(cfg.videopush.groups)


def _ids(raw: Any) -> List[int]:
    out: List[int] = []
    for x in raw or []:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v and v not in out:
            out.append(v)
    return out


def set_enabled(cfg: Config, on: bool) -> None:
    _save_field(cfg, CFG_ENABLED, bool(on))


def add_group(cfg: Config, gid: int) -> None:
    groups = group_targets(cfg)
    if int(gid) not in groups:
        groups.append(int(gid))
    _save_field(cfg, CFG_GROUPS, groups)


def remove_group(cfg: Config, gid: int) -> None:
    groups = [g for g in group_targets(cfg) if g != int(gid)]
    _save_field(cfg, CFG_GROUPS, groups)


FILTER_SYSTEM = """你是 B 站内容筛选助手。用户时间有限，只想看真正有信息量的视频。
你的判断标准很严：宁可一条都不推，也不要拿垃圾凑数。
你只输出 JSON，不输出任何多余的字。"""

FILTER_RULES = """判断标准（按重要性从高到低）：
1. 与关键词「{keyword}」强相关，从标题或简介能看出具体讲了什么，不是泛泛而谈。
2. 有真实信息量：有观点、有细节、有演示、有数据、有可复现的步骤或一手经验。
3. 拒绝营销：课程招生、训练营、带货、引流加群、纯广告、合集搬运，一律不要。
4. 拒绝噪音：纯新闻播报、情绪输出、八卦、低质 AI 配音、拼接剪辑、标题党。
5. 播放量和时长只作参考，不要因为播放量高就选，也不要因为播放量低就漏掉。
6. 同一主题下优先选信息密度高的，而不是面面俱到的入门科普。"""


def _fmt_candidates(hits: List[VideoHit]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        desc = re.sub(r"\s+", " ", h.desc or "").strip()
        if len(desc) > 120:
            desc = desc[:120] + "…"
        lines.append(
            f"[{i}] {h.title}（作者：{h.author}，播放 {h.play_text}，"
            f"时长 {h.duration_text}，发布 {datetime.fromtimestamp(h.pubdate).strftime('%m-%d') if h.pubdate else '未知'}）"
            + (f"\n    简介：{desc}" if desc else "")
        )
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """模型偶尔会包 ```json，或者前后带解释。尽量把第一个 JSON 对象抠出来。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


# ──────────────────────────────────────────────────────────────────────
#  笔记 → QQ 文本
# ──────────────────────────────────────────────────────────────────────
_IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MD = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HEAD_MD = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BOLD_MD = re.compile(r"\*\*(.+?)\*\*")
_ITAL_MD = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)")
_TS_MD = re.compile(r"\*?Content-\[(\d{1,2}:\d{2}(?::\d{2})?)\]\*?")
_HTML = re.compile(r"<[^>]+>")


def clean_note(md: str, max_chars: int = 1200) -> str:
    """把 BiliNote 的 Markdown 压成 QQ 里能看的纯文本。"""
    s = (md or "").replace("\r\n", "\n")
    s = _IMG_MD.sub("", s)
    s = _LINK_MD.sub(r"\1", s)
    s = _TS_MD.sub(lambda m: f"（{m.group(1)}）", s)
    s = _HTML.sub("", s)
    s = _HEAD_MD.sub("", s)
    s = _BOLD_MD.sub(r"\1", s)
    s = _ITAL_MD.sub(r"\1", s)
    s = re.sub(r"^\s*[-*+]\s+", "· ", s, flags=re.M)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s*(---+|___+)\s*$", "——————", s, flags=re.M)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = "\n".join(line.rstrip() for line in s.split("\n")).strip()
    if max_chars > 0 and len(s) > max_chars:
        cut = s[:max_chars]
        nl = cut.rfind("\n")
        if nl > max_chars * 0.6:
            cut = cut[:nl]
        s = cut.rstrip() + "…"
    return s


def format_message(hit: VideoHit, note_md: str, reason: str,
                   max_chars: int = 1200) -> str:
    when = ""
    if hit.pubdate:
        when = " ｜ " + datetime.fromtimestamp(hit.pubdate).strftime("%m-%d")
    head = (
        f"🎬 B站推荐 · {hit.keyword}\n\n"
        f"📺 {hit.title}\n"
        f"👤 {hit.author} ｜ ▶ {hit.play_text} ｜ ⏱ {hit.duration_text}{when}\n"
        f"🔗 {hit.url}"
    )
    if reason:
        head += f"\n\n💡 {reason}"
    note = clean_note(note_md, max_chars)
    if note:
        head += f"\n\n📝 内容总结\n{note}"
    else:
        head += "\n\n（这条的笔记还没生成出来，先看视频吧）"
    return head


# ──────────────────────────────────────────────────────────────────────
#  主类
# ──────────────────────────────────────────────────────────────────────
class VideoPush:
    def __init__(self, cfg: Config, store: Store, llm: DeepSeek,
                 adapter: OneBotAdapter):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.adapter = adapter
        self.search = BiliSearch(cfg)
        self.bilinote = BiliNote(cfg)
        self._busy = False
        self._last_warn = 0.0

    # ---------------- 生命周期 ----------------

    async def aclose(self) -> None:
        await self.search.aclose()

    async def run_forever(self) -> None:
        """每分钟看一眼：到点了就跑一轮；面板点「立即跑一次」会留一个标记文件。"""
        await asyncio.sleep(5.0)
        while True:
            try:
                if self._flag_set():
                    self._clear_flag()
                    log.info("视频推送：收到「立即跑一次」的信号。")
                    await self._cycle(force=True)
                elif self._due():
                    await self._cycle()
                else:
                    # NapCat 断线（比如 QQ 掉登录）期间攒下的「ready」笔记，
                    # 一连上就补发，不用等到下一个推送时间点。
                    await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("视频推送：这一轮出了异常，下一轮继续。")
            await asyncio.sleep(max(15, int(self.cfg.videopush.check_interval)))

    def _flag_path(self) -> Path:
        return self.cfg.db_path().parent / NOW_FLAG

    def _flag_set(self) -> bool:
        try:
            return self._flag_path().exists()
        except OSError:
            return False

    def _clear_flag(self) -> None:
        try:
            self._flag_path().unlink(missing_ok=True)
        except OSError:
            pass

    # ---------------- 时间调度 ----------------

    def _slots(self, day: datetime) -> List[datetime]:
        out = []
        for s in self.cfg.videopush.times or []:
            m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(s))
            if not m:
                continue
            hh, mm = int(m.group(1)), int(m.group(2))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                out.append(day.replace(hour=hh, minute=mm, second=0, microsecond=0))
        return sorted(out)

    def _due(self, now: Optional[datetime] = None) -> bool:
        if not self.cfg.videopush.enabled or not parse_topics(self.cfg.videopush.topics):
            return False
        now = now or datetime.now()
        last = self._last_run()
        window = float(getattr(self.cfg.videopush, "catchup_hours", 0) or 0)
        for slot in reversed(self._slots(now)):
            if slot > now or (last is not None and last >= slot):
                continue
            # 补跑窗口：错过太久的点不再补，免得半夜开机把早上的量补到晚上
            if window > 0 and (now - slot).total_seconds() > window * 3600:
                continue
            return True
        return False

    def _last_run(self) -> Optional[datetime]:
        raw = self.store._kv_get(KV_LAST_RUN) or ""
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    def _mark_run(self) -> None:
        self.store._kv_set(KV_LAST_RUN, datetime.now().replace(microsecond=0).isoformat())

    # ---------------- 主流程 ----------------

    async def _cycle(self, force: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            cfg = self.cfg.videopush
            topics = parse_topics(cfg.topics)
            if not topics:
                return
            targets = self._targets() + self._group_targets()
            if not targets:
                log.warning("视频推送：没有推送对象（videopush.targets / groups 和 "
                            "whitelist.masters 都是空的）。")
                return
            today = self._today_count()
            budget = max(0, int(cfg.max_per_day) - today)
            if budget <= 0 and not force:
                log.info("视频推送：今天已经推过 %d 条，到上限了。", today)
                self._mark_run()
                return

            self._mark_run()
            log.info("视频推送：开始一轮，话题 %d 个，今天还剩 %d 条额度。",
                     len(topics), budget)

            # 被 AI 毙掉的记录只留 45 天，过期清掉，不然老候选被永久拉黑
            dropped = self.store.vpush_prune(REJECTED_TTL_DAYS)
            if dropped:
                log.info("视频推送：清掉 %d 条 %d 天前的旧记录，让它们以后还能再被挑一次。",
                         dropped, REJECTED_TTL_DAYS)

            # 先把上次没做完的补上
            await self._resume_unfinished()
            await self._flush()

            picked: List[dict] = []
            for topic in topics:
                if budget <= 0:
                    break
                try:
                    hits = await self._search_topic(topic)
                except BiliRiskError as e:
                    log.warning("视频推送：「%s」被 B 站风控拦了：%s", topic.keyword, e)
                    continue
                except BiliSearchError as e:
                    log.warning("视频推送：「%s」搜索失败：%s", topic.keyword, e)
                    continue
                if not hits:
                    log.info("视频推送：「%s」没有新的候选。", topic.keyword)
                    continue
                picks = await self._filter(topic, hits)
                fresh = [p for p in picks if p.get("status") == "pending"]
                if not fresh:
                    log.info("视频推送：「%s」AI 没挑出值得推的（候选 %d 条）。",
                             topic.keyword, len(hits))
                    continue
                log.info("视频推送：「%s」AI 挑了 %d 条：%s", topic.keyword, len(fresh),
                         "、".join(p["bvid"] for p in fresh))
                for p in fresh[:max(1, int(cfg.max_per_topic))]:
                    if budget <= 0:
                        break
                    await self._make_note(p["id"])
                    budget -= 1
                    await self._flush()

            self._mark_run()
        finally:
            self._busy = False

    def _targets(self) -> List[int]:
        return push_targets(self.cfg)

    def _group_targets(self) -> List[int]:
        return group_targets(self.cfg)

    def _today_count(self) -> int:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.store.vpush_count_since(start, ("sent", "ready", "pending"))

    # ---------------- 搜索 + 本地过滤 ----------------

    async def _search_topic(self, topic: Topic) -> List[VideoHit]:
        cfg = self.cfg.videopush
        size = max(10, int(cfg.candidates))
        # 两路取候选：totalrank 两页 + 按发布时间一页，合并去重。
        # 只翻 totalrank 第一页的话，返回的永远是同一批「高播放老课」，
        # 30 天 + 播放量门槛一过就是 0 条（实测「算法竞赛」26 条全灭）。
        hits: List[VideoHit] = []
        seen: set = set()
        first_error: Optional[Exception] = None
        for order, pages in (("totalrank", 2), ("pubdate", 1)):
            try:
                part = await self.search.search(topic.keyword, pages=pages,
                                                page_size=size, order=order)
            except (BiliRiskError, BiliSearchError) as e:
                if first_error is None:
                    first_error = e
                continue
            for h in part:
                if h.bvid and h.bvid not in seen:
                    seen.add(h.bvid)
                    hits.append(h)
        if not hits and first_error is not None:
            raise first_error
        known = self.store.vpush_known([h.bvid for h in hits])
        now = time.time()
        out: List[VideoHit] = []
        for h in hits:
            if not h.bvid or h.bvid in known:
                continue
            if h.duration and (h.duration < cfg.min_duration or h.duration > cfg.max_duration):
                continue
            age = (now - h.pubdate) if h.pubdate else None
            if cfg.max_age_days > 0 and age is not None and age > cfg.max_age_days * 86400:
                continue
            if cfg.min_play > 0 and h.play < cfg.min_play:
                # 刚发布的新视频还没有播放量，别一上来就砍
                if age is None or age > FRESH_PLAY_GRACE_DAYS * 86400:
                    continue
            title = h.title or ""
            if any(w and w in title for w in (cfg.block_words or [])):
                continue
            h.keyword = topic.keyword
            out.append(h)
        # 一半按最新、一半按最热：既照顾刚发的新题解，也保留高播放的经典
        limit = max(6, int(cfg.candidates))
        keep: List[VideoHit] = []
        seen2: set = set()
        fresh = sorted((h for h in out if h.pubdate), key=lambda x: x.pubdate, reverse=True)
        hot = sorted(out, key=lambda x: x.play, reverse=True)
        for half in (fresh[: max(1, limit // 2)], hot):
            for h in half:
                if h.bvid in seen2:
                    continue
                seen2.add(h.bvid)
                keep.append(h)
                if len(keep) >= limit:
                    break
            if len(keep) >= limit:
                break
        return keep

    async def _filter(self, topic: Topic, hits: List[VideoHit]) -> List[dict]:
        cfg = self.cfg.videopush
        prompt = (
            f"关键词：{topic.keyword}\n"
            + (f"用户的额外要求：{topic.hint}\n" if topic.hint else "用户的额外要求：无\n")
            + f"\n候选视频（最多挑 {max(1, int(cfg.max_per_topic))} 条）：\n"
            + _fmt_candidates(hits) + "\n\n"
            + FILTER_RULES.format(keyword=topic.keyword) + "\n\n"
            + '输出示例：{"picks":[{"i":2,"score":86,"reason":"用真实项目讲了多智能体协作的坑"}]}\n'
            + f"score 低于 {int(cfg.min_score)} 的不要写进 picks；没有就返回 {{\"picks\": []}}。"
        )
        messages = [
            {"role": "system", "content": FILTER_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        try:
            res = await self.llm.chat(
                messages, purpose="videopush",
                max_tokens=max(800, int(cfg.filter_max_tokens)),
                thinking=bool(cfg.thinking),
                reasoning_effort=(str(cfg.filter_effort).strip() or None),
            )
        except Exception as e:
            log.warning("视频推送：筛选请求失败：%s", e)
            return []
        data = _extract_json(res.text)
        if not isinstance(data, dict):
            log.warning("视频推送：AI 返回的不是 JSON（%d 字），这次跳过。", len(res.text or ""))
            return []

        raw_picks = data.get("picks")
        if not isinstance(raw_picks, list):
            return []
        picked: List[dict] = []
        chosen = set()
        for item in raw_picks:
            if not isinstance(item, dict):
                continue
            try:
                i = int(item.get("i"))
                score = int(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if i < 1 or i > len(hits) or i in chosen:
                continue
            if score < int(cfg.min_score):
                continue
            chosen.add(i)
            hit = hits[i - 1]
            reason = str(item.get("reason") or "").strip()[:80]
            vid = self.store.vpush_add(
                bvid=hit.bvid, keyword=topic.keyword, title=hit.title,
                author=hit.author, url=hit.url, duration=hit.duration,
                play=hit.play, pubdate=hit.pubdate, score=score, reason=reason,
                status="pending",
            )
            if vid:
                picked.append({"id": vid, "bvid": hit.bvid, "status": "pending"})

        # 被 AI 看过的其余候选记成 rejected，免得明天再花钱重判
        skipped = [h for i, h in enumerate(hits, 1) if i not in chosen]
        self.store.vpush_mark_rejected(
            [(h.bvid, topic.keyword, h.title, h.author, h.url, h.duration, h.play, h.pubdate)
             for h in skipped])
        return picked

    # ---------------- 生成笔记 ----------------

    async def _resume_unfinished(self) -> None:
        """上次进程被杀 / 生成到一半的，重新捡起来。"""
        for row in self.store.vpush_rows(statuses=("pending", "generating"), limit=10):
            if time.time() - float(row["updated_at"] or 0) < 60:
                continue
            await self._make_note(int(row["id"]))

    async def _make_note(self, vid: int) -> None:
        row = self.store.vpush_row(vid)
        if not row or row["status"] in ("sent", "rejected"):
            return
        cfg = self.cfg.videopush
        self.store.vpush_update(vid, status="generating", error=None)
        try:
            note = await self.bilinote.generate(
                row["url"], style=cfg.note_style or None, timeout=float(cfg.note_timeout))
            md = (note.get("markdown") or "").strip()
            path = self._note_path(row["bvid"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(md, encoding="utf-8")
            self.store.vpush_update(vid, status="ready", note_path=str(path),
                                    task_id=note.get("task_id"), error=None)
            log.info("视频推送：笔记好了 %s（%d 字）", row["bvid"], len(md))
        except Exception as e:
            attempts = int(row["attempts"] or 0) + 1
            self.store.vpush_update(vid, status="failed", attempts=attempts,
                                    error=str(e)[:300])
            log.warning("视频推送：%s 生成笔记失败（第 %d 次）：%s",
                        row["bvid"], attempts, e)

    def _note_path(self, bvid: str) -> Path:
        return self.cfg.db_path().parent / "notes" / f"{bvid}.md"

    # ---------------- 推送 ----------------

    async def _flush(self) -> None:
        rows = self.store.vpush_rows(statuses=("ready",), limit=10)
        if not rows:
            return
        if not self.adapter.connected.is_set():
            log.info("视频推送：NapCat 还没连上，%d 条笔记先存着。", len(rows))
            return
        for row in rows:
            text = format_message(
                VideoHit(bvid=row["bvid"], title=row["title"] or "", author=row["author"] or "",
                         play=int(row["play"] or 0), duration=int(row["duration"] or 0),
                         pubdate=int(row["pubdate"] or 0), keyword=row["keyword"] or ""),
                self._read_note(row), row["reason"] or "",
                max_chars=int(self.cfg.videopush.note_max_chars),
            )
            sent_to = self._sent_to(row)
            # 私聊和群分开算：群里订阅了不代表私聊推过，反过来也一样。
            pending = [
                ("send_private_msg", {"user_id": uid, "message": text}, uid)
                for uid in self._targets() if uid not in sent_to
            ] + [
                ("send_group_msg", {"group_id": gid, "message": text}, gid)
                for gid in self._group_targets() if gid not in sent_to
            ]
            if not pending:
                self.store.vpush_update(int(row["id"]), status="sent",
                                        sent_at=time.time())
                continue
            ok = True
            for action, params, who in pending:
                try:
                    await self.adapter.call_api(action, params, timeout=30)
                    sent_to.append(who)
                except Exception as e:
                    ok = False
                    log.warning("视频推送：发给 %s 失败：%s", who, e)
            if ok:
                self.store.vpush_update(int(row["id"]), status="sent",
                                        sent_at=time.time(), sent_to=sent_to)
            else:
                self.store.vpush_update(int(row["id"]), sent_to=sent_to)
                break

    def _read_note(self, row: Dict[str, Any]) -> str:
        p = row.get("note_path")
        if not p:
            return ""
        try:
            return Path(p).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _sent_to(self, row: Dict[str, Any]) -> List[int]:
        raw = row.get("sent_to")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [int(x) for x in data if str(x).isdigit()] if isinstance(data, list) else []

    # ---------------- 面板用的小工具 ----------------

    def status_text(self) -> str:
        cfg = self.cfg.videopush
        topics = parse_topics(cfg.topics)
        last = self._last_run()
        rows = self.store.vpush_rows(limit=10)
        lines = [
            f"开关：{'开' if cfg.enabled else '关'}",
            f"话题：{'、'.join(t.keyword for t in topics) or '（没配）'}",
            f"推送时间：{'、'.join(cfg.times) or '（没配）'}",
            f"私聊：{'、'.join(str(u) for u in self._targets()) or '（没配）'}",
            f"群：{'、'.join(str(g) for g in self._group_targets()) or '（没订阅）'}",
            f"上次运行：{last.strftime('%Y-%m-%d %H:%M') if last else '还沒跑过'}",
            f"今天已推：{self._today_count()} 条（上限 {cfg.max_per_day}）",
        ]
        if rows:
            lines.append("最近：")
            for r in rows[:5]:
                lines.append(f"  · {r['bvid']} {r['status']} {str(r['title'] or '')[:24]}")
        return "\n".join(lines)
