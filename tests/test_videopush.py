"""视频推送的离线测试：搜索解析 → AI 筛选 → 笔记清洗 → 入库 → 推送。

    python tests/test_videopush.py

全程不联网、不调模型、不碰 QQ：
  - BiliSearch 只在构造时建 httpx 客户端，测试里不调用 search()
  - BiliNote 用假的 generate()
  - 模型用假的 chat()
  - 推送用假的 adapter.call_api()
数据库落在 data/_videopush/，跑完删掉。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bilisearch import (  # noqa: E402
    BiliRiskError, BiliSearch, BiliSearchError, VideoHit, clean_title,
    human_duration, human_play, parse_duration,
)
from app.config import Config, load_config  # noqa: E402
from app.onebot import IncomingMessage  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.store import Store  # noqa: E402
from app.videopush import (  # noqa: E402
    NOW_FLAG, KV_LAST_RUN, Topic, VideoPush, _extract_json, _fmt_candidates,
    clean_note, format_message, group_targets, parse_topics,
)
from panel import configio  # noqa: E402

TMP = ROOT / "data" / "_videopush"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail and not cond else ""))


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(2, 46 - len(title)))


class FakeLLM:
    """按预设返回 JSON 的假模型，记录调用参数。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})

        class R:
            pass

        r = R()
        r.text = self.text
        return r


class FakeNote:
    def __init__(self, markdown: str = "# 标题\n\n### 一 *Content-[00:42]*\n- 讲了 A\n- 讲了 B") -> None:
        self.markdown = markdown
        self.calls: list[tuple[str, str | None]] = []

    async def generate(self, url, *, style=None, timeout=None):
        self.calls.append((url, style))
        return {"task_id": "fake-task", "markdown": self.markdown, "meta": {}}

    async def aclose(self) -> None:
        pass


class FakeAdapter:
    def __init__(self, connected: bool = True) -> None:
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self.sent: list[tuple[str, dict]] = []
        self.texts: list[str] = []
        self.fail_uids: set[int] = set()

    async def call_api(self, action, params, timeout=20):
        who = params.get("user_id")
        if who is None:
            who = params.get("group_id")
        if int(who) in self.fail_uids:
            raise RuntimeError("假装发送失败")
        self.sent.append((action, params))
        return {"status": "ok"}

    async def send_text(self, msg, text: str):
        self.texts.append(text)
        self.sent.append(("send_text", {"text": text}))
        return {"status": "ok"}


def make_cfg(db: Path) -> Config:
    cfg = Config()
    cfg.database.path = str(db)
    cfg.whitelist.masters = [10001]
    cfg.videopush.enabled = True
    cfg.videopush.topics = ["算法竞赛|要真讲题", "AI 大模型"]
    cfg.videopush.times = ["09:00", "21:00"]
    cfg.videopush.max_per_day = 3
    cfg.videopush.max_per_topic = 2
    cfg.videopush.min_score = 70
    cfg.videopush.note_max_chars = 200
    return cfg


def hit(bvid: str, title: str, *, play: int = 10000, duration: int = 600,
        pubdate: int = 0, keyword: str = "算法竞赛") -> VideoHit:
    return VideoHit(bvid=bvid, title=title, author="某 UP", mid=1, play=play,
                    duration=duration, pubdate=pubdate or int(time.time()) - 3600,
                    desc="简介", pic="", keyword=keyword)


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "companion.db"

    # ────────────────────────────────────────────────────────────
    section("A 关键词解析")
    # ────────────────────────────────────────────────────────────
    ts = parse_topics(["算法竞赛|要真讲题、讲思路", "AI 大模型", "   ", "# 注释",
                       "算法竞赛|重复的", "动态规划｜蓝桥杯真题"])
    check("三条有效话题（空行/注释/重复被丢掉）", len(ts) == 3, str([t.keyword for t in ts]))
    check("竖线后面是筛选要求", ts[0].hint == "要真讲题、讲思路", repr(ts[0].hint))
    check("可以不写竖线", ts[1].keyword == "AI 大模型" and ts[1].hint == "")
    check("全角竖线也认", ts[2].keyword == "动态规划" and ts[2].hint == "蓝桥杯真题",
          repr(ts[2]))
    check("都是 Topic", all(isinstance(t, Topic) for t in ts))
    check("空列表不炸", parse_topics([]) == [] and parse_topics(None) == [])  # type: ignore[arg-type]

    # ────────────────────────────────────────────────────────────
    section("B 笔记清洗（Markdown → QQ 纯文本）")
    # ────────────────────────────────────────────────────────────
    md = (
        "# 标题\n\n"
        "![封面](http://x/y.png)\n"
        "### 第一个小点 *Content-[00:42]*\n"
        "- **重点**：先想清楚状态定义\n"
        "- 参考 [链接](https://b23.tv/abc)\n"
        "> 引用一句话\n"
        "---\n"
        "普通段落，带 *斜体*。\n"
    )
    out = clean_note(md, 2000)
    check("去掉图片", "![" not in out and "y.png" not in out)
    check("去掉 # 标题记号", "#" not in out)
    check("去掉 ** 粗体记号", "**" not in out)
    check("去掉 * 斜体记号", "*" not in out)
    check("链接只留文字", "https://b23.tv/abc" not in out and "链接" in out)
    check("时间戳变成中文括号", "（00:42）" in out, out[:80])
    check("列表变成 ·", "· " in out and "\n- " not in out)
    check("引用记号没了", not out.startswith(">"))
    check("分隔线换成中文横线", "——————" in out)
    long_out = clean_note("第一行\n" + "字" * 500, 100)
    check("超长会截断并加省略号", len(long_out) <= 101 and long_out.endswith("…"),
          f"{len(long_out)} 字")
    check("空输入安全", clean_note("") == "" and clean_note(None) == "")  # type: ignore[arg-type]

    # ────────────────────────────────────────────────────────────
    section("C 推送文案")
    # ────────────────────────────────────────────────────────────
    h = hit("BV1xx411c7mD", "题解：线段树入门")
    msg = format_message(h, md, "用真实例题讲了懒标记")
    check("带视频链接", "https://www.bilibili.com/video/BV1xx411c7mD" in msg)
    check("带标题和作者", "题解：线段树入门" in msg and "某 UP" in msg)
    check("带推荐理由", "用真实例题讲了懒标记" in msg)
    check("带总结正文", "📝 内容总结" in msg)
    check("没有 Markdown 记号", "**" not in msg and "# 标题" not in msg)

    # ────────────────────────────────────────────────────────────
    section("D 模型 JSON 抠取")
    # ────────────────────────────────────────────────────────────
    check("纯 JSON", _extract_json('{"picks": []}') == {"picks": []})
    check("```json 围栏", _extract_json('```json\n{"picks":[{"i":1}]}\n```') == {"picks": [{"i": 1}]})
    check("前后带解释", _extract_json('好的，结果如下：{"picks":[]} 以上。') == {"picks": []})
    check("不是 JSON 返回 None", _extract_json("我不知道") is None)
    check("空串返回 None", _extract_json("") is None)

    # ────────────────────────────────────────────────────────────
    section("E B 站字段解析")
    # ────────────────────────────────────────────────────────────
    check("1242:13 → 74533 秒", parse_duration("1242:13") == 74533)
    check("04:21 → 261 秒", parse_duration("04:21") == 261)
    check("1:02:03 → 3723 秒", parse_duration("1:02:03") == 3723)
    check("整数原样", parse_duration(300) == 300)
    check("垃圾值 → 0", parse_duration("abc") == 0 and parse_duration(None) == 0)  # type: ignore[arg-type]
    check("74533 秒显示为 20:42:13", human_duration(74533) == "20:42:13", human_duration(74533))
    check("261 秒显示为 4:21", human_duration(261) == "4:21")
    check("播放量 12345 → 1.2万", human_play(12345) == "1.2万")
    check("播放量 2.5亿", human_play(250_000_000) == "2.5亿")
    check("标题去高亮标签", clean_title('<em class="keyword">线段树</em>入门 &amp; 进阶')
          == "线段树入门 & 进阶")
    check("标题压空白", clean_title("a   b\n c") == "a b c")

    # ────────────────────────────────────────────────────────────
    section("E2 风控回退（v_voucher → 旧接口）")
    # ────────────────────────────────────────────────────────────
    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    class _Client:
        """新版接口只给 v_voucher；旧接口给一条正常结果。"""

        def __init__(self, legacy_result):
            self.legacy_result = legacy_result
            self.calls: list[str] = []

        async def get(self, url, params=None):
            self.calls.append(url)
            if "wbi/search/type" in url:
                return _Resp({"code": 0, "data": {"v_voucher": "voucher-abc"}})
            return _Resp({"code": 0, "data": {"result": self.legacy_result}})

    item = {"bvid": "BV1legacy", "title": '<em class="keyword">旧接口</em>也能用',
            "author": "UP", "mid": 7, "play": 1234, "duration": "1:02:03",
            "pubdate": 1700000000, "description": "d", "pic": "p"}
    s = object.__new__(BiliSearch)
    s._client = _Client([item])
    s._mixin = "m" * 32
    s._mixin_at = time.time()
    s._cookie_ready = True
    hits = await s._search_page("算法竞赛", 1, 20, "totalrank")
    check("新版被挑战时回退旧接口", len(hits) == 1, str([h.bvid for h in hits]))
    check("旧接口字段解析正确", hits[0].bvid == "BV1legacy"
          and hits[0].title == "旧接口也能用"
          and hits[0].duration == 3723 and hits[0].play == 1234)
    check("两次请求都发过", len(s._client.calls) == 2, str(s._client.calls))

    s2 = object.__new__(BiliSearch)
    s2._client = _Client([])          # 旧接口也空 → 应该抛 BiliRiskError
    s2._mixin = "m" * 32
    s2._mixin_at = time.time()
    s2._cookie_ready = True
    try:
        await s2._search_page("算法竞赛", 1, 20, "totalrank")
        raised = False
    except BiliRiskError as e:
        raised = "Cookie" in str(e) or "cookie" in str(e)
    check("两条路都断时抛风控错并提示 Cookie", raised)

    # ────────────────────────────────────────────────────────────
    section("F 入库与去重")
    # ────────────────────────────────────────────────────────────
    store = Store(db)
    vid = store.vpush_add(bvid="BV1aa", keyword="算法竞赛", title="A", author="u",
                          url="https://www.bilibili.com/video/BV1aa", duration=600,
                          play=1000, pubdate=int(time.time()), score=88,
                          reason="干货", status="pending")
    check("新增返回自增 id", vid > 0, str(vid))
    check("同一 bvid 不会重复插入",
          store.vpush_add(bvid="BV1aa", keyword="别的") == 0)
    check("vpush_known 认得它", store.vpush_known(["BV1aa", "BV1bb"]) == {"BV1aa"})
    check("按状态查得到", [r["bvid"] for r in store.vpush_rows(statuses=("pending",))] == ["BV1aa"])
    check("vpush_row 按 id 查", (store.vpush_row(vid) or {}).get("bvid") == "BV1aa")
    store.vpush_update(vid, status="ready", note_path=str(TMP / "a.md"), task_id="t1")
    row = store.vpush_row(vid) or {}
    check("更新状态和笔记路径", row["status"] == "ready" and row["note_path"].endswith("a.md"))
    store.vpush_mark_rejected([("BV1cc", "算法竞赛", "C", "u", "https://b/C", 300, 50,
                                int(time.time()))])
    check("被毙掉的也记下来（明天不再重判）", "BV1cc" in store.vpush_known(["BV1cc"]))
    check("被毙的不算待生成", all(r["bvid"] != "BV1cc"
                          for r in store.vpush_rows(statuses=("pending", "ready"))))
    check("按时间数条数", store.vpush_count_since(time.time() - 3600,
                                   ("ready", "pending", "sent")) == 1)
    check("状态过滤生效", store.vpush_count_since(time.time() - 3600, ("sent",)) == 0)

    # ────────────────────────────────────────────────────────────
    section("G 调度时间")
    # ────────────────────────────────────────────────────────────
    cfg = make_cfg(db)
    llm = FakeLLM('{"picks": []}')
    adapter = FakeAdapter()
    vp = VideoPush(cfg, store, llm, adapter)  # type: ignore[arg-type]
    vp.bilinote = FakeNote()  # type: ignore[assignment]
    now = datetime(2026, 10, 20, 10, 0, 0)
    slots = vp._slots(now)
    check("两个时间点", len(slots) == 2 and slots[0].hour == 9 and slots[1].hour == 21,
          str(slots))
    cfg.videopush.times = ["00:00"]
    store._kv_set(KV_LAST_RUN, "")
    check("从没跑过 → 到点了", vp._due() is True)
    store._kv_set(KV_LAST_RUN, datetime.now().replace(microsecond=0).isoformat())
    check("今天已经跑过 → 不再跑", vp._due() is False)
    store._kv_set(KV_LAST_RUN, (datetime.now().replace(microsecond=0)
                                .replace(year=2000)).isoformat())
    check("很久没跑 → 会补跑", vp._due() is True)
    cfg.videopush.enabled = False
    check("开关关掉 → 不跑", vp._due() is False)
    cfg.videopush.enabled = True
    cfg.videopush.topics = []
    check("没话题 → 不跑", vp._due() is False)
    cfg.videopush.topics = ["算法竞赛"]
    check("推送对象认 whitelist.masters", vp._targets() == [10001])
    cfg.videopush.targets = [1, 1, "2", "x"]
    check("targets 去重 + 跳过垃圾", vp._targets() == [1, 2], str(vp._targets()))
    cfg.videopush.targets = []          # 后面按「发给主人」测

    # ────────────────────────────────────────────────────────────
    section("H AI 筛选")
    # ────────────────────────────────────────────────────────────
    hits = [hit("BV1p1", "讲座：线段树"), hit("BV1p2", "卖课广告"), hit("BV1p3", "题解合集")]
    check("候选表带序号和简介", _fmt_candidates(hits).startswith("[1] 讲座：线段树"))
    llm.text = ('{"picks":[{"i":1,"score":90,"reason":"讲了真实题目"},'
                '{"i":2,"score":40,"reason":"广告"},{"i":9,"score":99}]}')
    cfg.videopush.min_score = 70
    picked = await vp._filter(Topic("算法竞赛", "要真讲题"), hits)
    check("只留下过线的 1 条", len(picked) == 1 and picked[0]["bvid"] == "BV1p1",
          str(picked))
    check("低于门槛的被丢掉", all(p["bvid"] != "BV1p2" for p in picked))
    check("越界序号被忽略", all(p["bvid"] != "BV1p3" for p in picked))
    check("筛选走了思考模式", llm.calls and llm.calls[-1].get("thinking") is True,
          str(llm.calls[-1].get("thinking") if llm.calls else None))
    check("筛选的 purpose 是 videopush", llm.calls[-1].get("purpose") == "videopush")
    check("思考档位是 high（用户明确要求）",
          llm.calls[-1].get("reasoning_effort") == "high",
          str(llm.calls[-1].get("reasoning_effort")))
    check("提示词里有关键词和要求",
          "算法竞赛" in llm.calls[-1]["messages"][1]["content"]
          and "要真讲题" in llm.calls[-1]["messages"][1]["content"])
    before = len(store.vpush_rows(statuses=("pending",)))
    llm.text = '{"picks": []}'
    empty = await vp._filter(Topic("算法竞赛"), hits)
    check("AI 一条都不选时返回空", empty == [])
    check("没选中的会被记成 rejected（省下一次重判）",
          store.vpush_known([h.bvid for h in hits]) >= {h.bvid for h in hits})
    llm.text = "我觉得都不错"
    check("模型胡说时返回空而不是崩", await vp._filter(Topic("算法竞赛"), hits) == [])

    # ────────────────────────────────────────────────────────────
    section("I 生成笔记与推送")
    # ────────────────────────────────────────────────────────────
    cfg.videopush.min_score = 70
    vid2 = store.vpush_add(bvid="BV1note", keyword="算法竞赛", title="要生成笔记的视频",
                           author="u", url="https://www.bilibili.com/video/BV1note",
                           duration=600, play=2000, pubdate=int(time.time()),
                           score=95, reason="好", status="pending")
    await vp._make_note(vid2)
    row = store.vpush_row(vid2) or {}
    check("笔记生成后状态是 ready", row["status"] == "ready", str(row.get("status")))
    note_file = Path(row["note_path"] or "")
    check("笔记落盘", note_file.exists() and "标题" in note_file.read_text(encoding="utf-8"))
    check("笔记请求带上了 URL 和风格",
          vp.bilinote.calls[-1][0].endswith("BV1note") and vp.bilinote.calls[-1][1] == "detailed",
          str(vp.bilinote.calls[-1]))

    adapter.connected.clear()
    await vp._flush()
    check("NapCat 没连上时先存着", (store.vpush_row(vid2) or {})["status"] == "ready")
    adapter.connected.set()
    await vp._flush()
    row = store.vpush_row(vid2) or {}
    check("连上后发出去", row["status"] == "sent", str(row.get("status")))
    check("发的是私聊", adapter.sent and adapter.sent[-1][0] == "send_private_msg")
    check("发给主人", adapter.sent and adapter.sent[-1][1]["user_id"] == 10001)
    check("正文含链接和总结", "BV1note" in adapter.sent[-1][1]["message"]
          and "内容总结" in adapter.sent[-1][1]["message"])
    n_sent = len(adapter.sent)
    await vp._flush()
    check("不会重复发", len(adapter.sent) == n_sent, f"{len(adapter.sent)} 次")

    # 发送失败的场景：一条都不许丢
    vid3 = store.vpush_add(bvid="BV1fail", keyword="算法竞赛", title="发不出去",
                           author="u", url="https://www.bilibili.com/video/BV1fail",
                           duration=600, play=1, pubdate=int(time.time()), score=90,
                           reason="", status="ready")
    note3 = TMP / "notes" / "BV1fail.md"
    note3.parent.mkdir(parents=True, exist_ok=True)
    note3.write_text("笔记", encoding="utf-8")
    store.vpush_update(vid3, note_path=str(note3))
    adapter.fail_uids = {10001}
    await vp._flush()
    check("发送失败时保持 ready（下一轮重试）",
          (store.vpush_row(vid3) or {})["status"] == "ready")
    adapter.fail_uids = set()

    # 标记文件（面板「推送一次」）
    flag = cfg.db_path().parent / NOW_FLAG
    flag.write_text("1", encoding="utf-8")
    check("标记文件能读到", vp._flag_set() is True)
    vp._clear_flag()
    check("清掉标记文件", vp._flag_set() is False)

    # ────────────────────────────────────────────────────────────
    section("J 面板摘要")
    # ────────────────────────────────────────────────────────────
    text = vp.status_text()
    check("摘要里有开关", "开关" in text)
    check("摘要里有话题", "算法竞赛" in text)
    check("摘要里有今天条数", "今天已推" in text)

    # ────────────────────────────────────────────────────────────
    section("K 候选池（多排序合并 + 新视频宽限）")
    # ────────────────────────────────────────────────────────────
    class FakeSearch:
        def __init__(self, table):
            self.table = table
            self.calls = []

        async def search(self, keyword, pages=1, page_size=30, order="totalrank"):
            self.calls.append((order, pages))
            item = self.table.get(order)
            if isinstance(item, Exception):
                raise item
            return list(item or [])

        async def aclose(self) -> None:
            pass

    now_ts = int(time.time())
    a = hit("BVk01", "区间覆盖", pubdate=now_ts - 10 * 86400)
    b = hit("BVk02", "并查集", play=9000, pubdate=now_ts - 8 * 86400)
    fresh = hit("BVk03", "刚发的题解", play=1, pubdate=now_ts - 3600)
    old_low = hit("BVk04", "没人看的老视频", play=10, pubdate=now_ts - 20 * 86400)
    ancient = hit("BVk05", "三年前的课", play=99999, pubdate=now_ts - 400 * 86400)
    yesterday = hit("BVk06", "昨天发布", play=50, pubdate=now_ts - 86400)
    fake = FakeSearch({"totalrank": [a, b, fresh, old_low, ancient],
                       "pubdate": [fresh, b, yesterday]})
    vp.search = fake  # type: ignore[assignment]
    got = await vp._search_topic(Topic("算法竞赛", ""))
    bvids = [h.bvid for h in got]
    check("两路都查了（totalrank 2 页 + pubdate 1 页）",
          fake.calls == [("totalrank", 2), ("pubdate", 1)], str(fake.calls))
    check("两路合并后按 bvid 去重", bvids.count("BVk02") == 1, str(bvids))
    check("刚发布的新视频不受 min_play 限制", "BVk03" in bvids, str(bvids))
    check("1 天前发布的低播放新视频也留下", "BVk06" in bvids, str(bvids))
    check("老的低播放视频被丢掉", "BVk04" not in bvids, str(bvids))
    check("超过 30 天的视频被丢掉", "BVk05" not in bvids, str(bvids))
    check("最新的一半排在前面", bvids.index("BVk03") < bvids.index("BVk01"), str(bvids))

    vp.search = FakeSearch({"totalrank": BiliRiskError("挑战"), "pubdate": [a]})
    got = await vp._search_topic(Topic("算法竞赛", ""))
    check("totalrank 被风控时用 pubdate 的结果", [h.bvid for h in got] == ["BVk01"])

    vp.search = FakeSearch({"totalrank": BiliRiskError("挑战"), "pubdate": BiliSearchError("挂了")})
    try:
        await vp._search_topic(Topic("算法竞赛", ""))
        check("两路都失败要抛错", False)
    except BiliRiskError:
        check("两路都失败抛第一个错误", True)

    # 补跑窗口 catchup_hours
    cfg.videopush.catchup_hours = 12
    cfg.videopush.times = ["09:00"]
    store._kv_set(KV_LAST_RUN, "")
    check("错过 1 小时仍补跑", vp._due(datetime(2026, 10, 20, 10, 0)) is True)
    check("错过 13 小时不再补", vp._due(datetime(2026, 10, 20, 22, 0)) is False)
    cfg.videopush.catchup_hours = 0
    check("窗口设 0 = 不限制补跑", vp._due(datetime(2026, 10, 20, 22, 0)) is True)
    store._kv_set(KV_LAST_RUN, "2026-10-20T21:30:00")
    check("已经跑过的点不重复", vp._due(datetime(2026, 10, 20, 22, 0)) is False)

    # rejected 的 45 天 TTL
    old_id = store.vpush_add(bvid="BVk99", keyword="算法竞赛", title="旧候选", url="u",
                             status="rejected")
    store._conn.execute("UPDATE video_push SET created_at=? WHERE id=?",
                        (time.time() - 50 * 86400, old_id))
    store._conn.commit()
    new_id = store.vpush_add(bvid="BVk98", keyword="算法竞赛", title="新候选", url="u",
                             status="rejected")
    check("过期的 rejected 被清掉", store.vpush_prune(45) == 1)
    check("没到期的 rejected 留着", store.vpush_row(new_id) is not None)

    section("L. 对话指令：开 / 关 / 状态 / 现在就推")
    cfg_path = TMP / "cmd-config.yaml"
    raw = configio.load(ROOT / "config.example.yaml")
    raw.setdefault("videopush", {})
    raw["videopush"].update({
        "enabled": True,
        "topics": ["算法竞赛|要真讲题"],
        "targets": [10001],
        "groups": [],
        "max_per_day": 3,
    })
    raw.setdefault("whitelist", {})
    raw["whitelist"]["masters"] = [10001]
    configio.save(cfg_path, raw, backup=False)
    ccfg = load_config(cfg_path)
    cstore = Store(TMP / "cmd.db")
    cad = FakeAdapter()
    orch = Orchestrator.__new__(Orchestrator)   # 只测指令这条路，不装整套依赖
    orch.cfg, orch.store, orch.adapter = ccfg, cstore, cad

    check("「视频推送 开」→ on", orch.command_action("视频推送 开") == "on")
    check("带 @ 前缀也认", orch.command_action("@123456789 视频推送 关") == "off")
    check("带 / 前缀也认", orch.command_action("/视频推送 状态") == "status")
    check("只说四个字 = 看状态", orch.command_action("视频推送") == "status")
    check("「现在就推」", orch.command_action("视频推送 现在就推") == "now")
    check("聊天里提到不算指令", orch.command_action("聊聊视频推送呗") is None)
    check("词不对给用法", orch.command_action("视频推送 怎么弄") == "help")

    def gm(text: str, *, group: int | None = None, uid: int = 10001) -> IncomingMessage:
        return IncomingMessage(
            message_type="group" if group else "private", user_id=uid,
            group_id=group, self_id=123456789, text=text, nickname="", raw={})

    check("私聊发「关」被接管", await orch._handle_command(gm("视频推送 关")))
    check("关掉了（内存里立刻生效）", ccfg.videopush.enabled is False)
    check("关掉了（config.yaml 里也写了）",
          load_config(cfg_path).videopush.enabled is False)
    check("关的时候回了话", "关了" in cad.texts[-1], cad.texts[-1] if cad.texts else "")

    check("私聊再开回来", await orch._handle_command(gm("视频推送 开")))
    check("开回来了", load_config(cfg_path).videopush.enabled is True)

    await orch._handle_command(gm("视频推送 开", group=100000001))
    check("群里开 → 群号进 groups",
          load_config(cfg_path).videopush.groups == [100000001])
    check("群里开 → 内存立刻多一个群收件人", group_targets(ccfg) == [100000001])

    await orch._handle_command(gm("视频推送 关", group=100000001))
    check("群里关 → 摘掉群号", group_targets(ccfg) == [])
    check("群里关 → 私聊还在，整体仍开着", ccfg.videopush.enabled is True)

    await orch._handle_command(gm("视频推送 状态"))
    status = cad.texts[-1]
    check("状态里有开关", "视频推送：开" in status, status)
    check("状态里有收件人", "私聊 10001" in status, status)

    check("非主人发「关」也会被接管", await orch._handle_command(gm("视频推送 关", uid=123456)))
    check("非主人改不动", load_config(cfg_path).videopush.enabled is True)
    check("非主人收到拒绝", "只有主人" in cad.texts[-1], cad.texts[-1])

    ccfg.whitelist.masters = []
    await orch._handle_command(gm("视频推送 开", group=100000001, uid=123456))
    check("群里没配主人时拒绝", group_targets(ccfg) == [])
    check("拒绝时提示去配 masters", "whitelist.masters" in cad.texts[-1], cad.texts[-1])

    await orch._handle_command(gm("视频推送 现在就推"))
    flag = ccfg.db_path().parent / NOW_FLAG
    check("「现在就推」会留立即执行标记", flag.exists())
    flag.unlink(missing_ok=True)

    # 群推送：私聊 + 群各发一条
    ccfg.whitelist.masters = [10001]
    vp.cfg.videopush.groups = [100000001]
    vp.adapter.fail_uids.clear()
    vp.adapter.sent.clear()
    store.vpush_add(bvid="BVg01", keyword="算法竞赛", title="群推送验证", url="u",
                    status="ready", note_path="")
    await vp._flush()
    actions = [(a, p.get("user_id") or p.get("group_id")) for a, p in vp.adapter.sent]
    check("私聊和群都发了", ("send_private_msg", 10001) in actions
          and ("send_group_msg", 100000001) in actions, str(actions))
    cstore.close()

    store.close()
    await vp.aclose()
    shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 46)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败：" + "、".join(FAIL))
    print("=" * 46 + "\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
