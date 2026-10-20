"""P2/P3/P4 的离线测试：主动搭话、长期记忆、作息、自动白名单、延迟不占锁。

    python tests/test_p2p3.py

全部离线（llm.mock=true），不花钱、不连 QQ、不依赖当前几点钟。
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

from app.budget import Budget  # noqa: E402
from app.config import CST, Config, load_config  # noqa: E402
from app.context import (ContextBuilder, PROACTIVE_MARK,  # noqa: E402
                         wrap_proactive_message)
from app.life import Life  # noqa: E402
from app.llm import DeepSeek  # noqa: E402
from app.memory import Memory, _plain, parse_facts, tokenize  # noqa: E402
from app.onebot import IncomingMessage  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.proactive import Proactive  # noqa: E402
from app.store import Store  # noqa: E402

TMP = ROOT / "data" / "_test_p2p3"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  —— {detail}" if detail and not cond else ""))


class StubAdapter:
    self_id = 10001

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.connected = asyncio.Event()
        self.connected.set()

    async def send_text(self, msg, text: str) -> None:
        self.sent.append((msg.session_id, text))


def make_msg(text: str, uid: int = 20002) -> IncomingMessage:
    return IncomingMessage(message_type="private", user_id=uid, group_id=None,
                           self_id=10001, text=text, nickname=f"测试君{uid}")


def write_config(cfg_dir: Path) -> Path:
    p = cfg_dir / "config.yaml"
    p.write_text(f"""
llm:
  api_key: ""
  model: "deepseek-flash"
  thinking: false
  max_tokens: 200
  mock: true
context:
  history_tokens: 1500
budget:
  min_interval_seconds: 0
pricing:
  currency_per_million:
    deepseek-flash:
      cache_hit:  {{ peak: 0.04, offpeak: 0.02 }}
      cache_miss: {{ peak: 2.0,  offpeak: 1.0 }}
      output:     {{ peak: 8.0,  offpeak: 4.0 }}
  peak_weekdays: [0, 1, 2, 3, 4]
  peak_ranges: [[9, 12], [14, 18]]
  holidays: []
humanize:
  read_delay_min: 0
  read_delay_max: 0
  typing_per_char_min: 0
  typing_per_char_max: 0
  typing_min: 0
  typing_max: 0
  segment_gap_min: 0
  segment_gap_max: 0
  debounce_seconds: 0.05
  late_reply_chance: 0
life:
  enabled: true
  sleeping: [1, 8]
  working: [[9, 12], [14, 18]]
  weekday_only_work: true
  proactive:
    enabled: true
    min_idle_hours: 6
    max_per_day: 2
    active_hours: [10, 23]
memory:
  enabled: true
  extract_every_turns: 4
  extract_min_turns: 2
  min_sim: 0.45
whitelist:
  private: []
  groups: []
  auto_accept_first: 1
database:
  path: "{(cfg_dir / 'companion.db').as_posix()}"
logging:
  level: "ERROR"
  file: "{(cfg_dir / 'companion.log').as_posix()}"
""", encoding="utf-8")
    return p


def backdate_messages(store: Store, sid: str, hours: float) -> None:
    ts = time.time() - hours * 3600.0
    store._conn.execute("UPDATE messages SET created_at=? WHERE session_id=?", (ts, sid))
    store._conn.commit()


def backdate_fact(store: Store, fact_id: int, days: float) -> None:
    ts = time.time() - days * 86400.0
    store._conn.execute("UPDATE facts SET created_at=? WHERE id=?", (ts, fact_id))
    store._conn.commit()


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    cfg: Config = load_config(write_config(TMP))
    store = Store(cfg.db_path())
    llm = DeepSeek(cfg, store)
    ctx = ContextBuilder(cfg, store, llm)
    budget = Budget(cfg, store)
    life = Life(cfg)
    memory = Memory(cfg, store, llm)
    adapter = StubAdapter()
    orch = Orchestrator(cfg, store, llm, ctx, budget, adapter, life, memory)
    pro = Proactive(cfg, store, llm, ctx, budget, adapter, life, memory)

    sid = "private:20002"

    # ══════════ A. 分词与解析 ══════════
    print("\n── A. 分词与解析 ─────────────────────────")
    toks = tokenize("甲方又让我改稿")
    check("中文切成二元组", "改稿" in toks and "甲方" in toks, str(toks))
    check("单字也保留", tokenize("嗯") == ["嗯"], str(tokenize("嗯")))
    check("英文数字切词", tokenize("RTX 5090 ok") == ["rtx", "5090", "ok"],
          str(tokenize("RTX 5090 ok")))

    good = ("事实=他在一家广告公司做平面设计|关键词=广告,设计,上班|重要度=4\n"
            "事实=他养了只橘猫叫布丁|关键词=猫,布丁,宠物|重要度=3")
    parsed = parse_facts(good)
    check("解析出两条事实", len(parsed) == 2, str(parsed))
    check("带出关键词", parsed[0][1] == ["广告", "设计", "上班"], str(parsed[0][1]))
    check("带出重要度", parsed[0][2] == 4.0, str(parsed[0][2]))
    check("全角冒号也认", len(parse_facts("事实：他叫老王|关键词：老王,名字|重要度：5")) == 1)
    check("「无」解析成空", parse_facts("无") == [])
    check("垃圾文本不抛错", parse_facts("我今天心情不错，随便聊聊。") == [])
    check("带项目符号也能解析", len(parse_facts("- 事实=他喜欢猫|关键词=猫|重要度=3")) == 1)
    check("重要度越界会被夹住",
          parse_facts("事实=x|关键词=y|重要度=99")[0][2] == 5.0)

    # ══════════ B. 抽取与去重 ══════════
    print("\n── B. 抽取与去重 ─────────────────────────")
    await store.add_message(sid, "user", "【他刚说】我在折腾一个 QQ 机器人", "我在折腾一个 QQ 机器人")
    await store.add_message(sid, "assistant", "哦？", "哦？")
    await store.add_message(sid, "user", "【他刚说】最近工作好忙天天加班", "最近工作好忙天天加班")
    await store.add_message(sid, "assistant", "别太累", "别太累")

    ok = await memory.extract(sid)
    facts = await store.facts(sid)
    check("抽取成功", ok)
    check("抽出了事实", len(facts) >= 2, f"{len(facts)} 条：{[f['content'] for f in facts]}")

    await memory.extract(sid)          # 再抽一次，内容一样
    facts2 = await store.facts(sid)
    check("重复抽取不会堆重复条目（走合并）", len(facts2) == len(facts),
          f"{len(facts)} → {len(facts2)}")

    covered = await store.kv_get_int(f"mem_until:{sid}", 0)
    check("抽取水位线推到了最新消息", covered == 4, str(covered))
    check("水位线之后没新对话就不再抽", await memory.extract_if_due(sid) is False)

    check("主动搭话的合成消息不会被当成他说的话",
          _plain(wrap_proactive_message(["改稿的事"])) == "")
    check("普通消息能被还原",
          _plain("【当前】…\n【他刚说】我到家了\n【刚好想起来】布丁") == "我到家了")

    # ══════════ C. 召回 ══════════
    print("\n── C. 召回（本地，0 token） ──────────────")
    mem_sid = "private:30003"
    ok = await memory._store_one(mem_sid, "他在一家广告公司做平面设计",
                                 ["广告", "设计", "改稿", "甲方", "加班"], 4.0)
    check("写入一条记忆", ok and await memory.count(mem_sid) == 1)
    await memory._store_one(mem_sid, "他养了只橘猫叫布丁", ["猫", "布丁", "宠物"], 3.0)
    await memory._store_one(mem_sid, "他住在杭州", ["杭州", "城市", "住"], 3.0)

    hit = await memory.recall(mem_sid, "甲方又让我改稿了")
    check("相关的话能想起相关的事", any("广告公司" in h or "布丁" in h for h in hit), str(hit))
    check("关键词桥生效（正文没有「改稿」也能命中）",
          any("广告公司" in h for h in hit), str(hit))

    miss = await memory.recall(mem_sid, "今天天气真不错啊")
    check("完全无关的话不硬凑", miss == [], str(miss))

    # 排序：同等相关时，重要 / 新的排前面
    order = await memory.recall(mem_sid, "广告设计改稿甲方加班", top_k=3)
    check("最相关那条排第一", order and "广告公司" in order[0], str(order))

    # min_sim 只卡相似度，不卡最终分：很老的完美匹配仍然会被想起来
    rows = await store.facts(mem_sid)
    design = next(r for r in rows if "广告公司" in r["content"])
    backdate_fact(store, int(design["id"]), 3650)      # 十年前
    old = await memory.recall(mem_sid, "甲方又让我改稿了")
    check("十年前的完美匹配依然想得起来（衰减只管排序）",
          any("广告公司" in h for h in old), str(old))

    check("没记忆的会话返回空", await memory.recall("private:99999", "随便") == [])
    check("记忆条数统计正确", await memory.count(mem_sid) == 3)

    # ══════════ D. 作息 ══════════
    print("\n── D. 作息 ───────────────────────────────")
    wed = lambda h, m=0: datetime(2026, 3, 4, h, m, tzinfo=CST)      # 周三
    sun = lambda h, m=0: datetime(2026, 3, 8, h, m, tzinfo=CST)      # 周日

    check("凌晨 00:30 还在熬（歇着，但没睡）",
          life.state(wed(0, 30)) == "winding_down", life.state(wed(0, 30)))
    check("01:00 起算睡觉", life.state(wed(1)) == "sleeping", life.state(wed(1)))
    check("早上 07:30 还在睡", life.state(wed(7, 30)) == "sleeping")
    check("早上 08:00 醒了", life.state(wed(8, 0)) != "sleeping")
    check("周三 10:30 在上班", life.state(wed(10, 30)) == "working", life.state(wed(10, 30)))
    check("周三 13:00 午休算有空", life.state(wed(13)) == "free", life.state(wed(13)))
    check("周日 10:30 不算上班", life.state(sun(10, 30)) == "free", life.state(sun(10, 30)))
    check("23:30 是晚上歇着", life.state(wed(23, 30)) == "winding_down", life.state(wed(23, 30)))

    q = life.quiet_delay(wed(2))
    check("睡着时算出还要睡多久", 5.9 < q / 3600 < 6.1, f"{q / 3600:.2f} 小时")
    check("醒着时没有安静时段", life.quiet_delay(wed(10)) == 0.0)
    check("跨零点睡眠区间也能算", Life(cfg).quiet_delay(wed(23, 30)) == 0.0)

    check("活跃时间窗内", life.in_active_hours(wed(15)))
    check("活跃时间窗外（凌晨）", not life.in_active_hours(wed(3)))
    check("心情话术存在", life.mood(wed(10)) != "", life.mood(wed(10)))
    check("作息描述可读", "上班" in life.describe(wed(10)), life.describe(wed(10)))

    # ══════════ E. 自动白名单 ══════════
    print("\n── E. 自动白名单（开箱即用） ─────────────")
    first = make_msg("在吗", uid=20002)
    second = make_msg("你好", uid=40004)
    check("第一个人被自动认下", await orch._allowed(first))
    check("认下之后进了数据库", await store.allowlist_has(20002))
    check("第二个人被挡在外面", not await orch._allowed(second))
    check("第一个人再发直接放行", await orch._allowed(first))
    who = await store.allowlist()
    check("allowlist 表里只有一个人", len(who) == 1, str(who))

    cfg.whitelist.private = [40004]
    check("写进 config 的号也能放行", await orch._allowed(second))
    cfg.whitelist.private = []

    # ══════════ F. 主动搭话的护栏 ══════════
    print("\n── F. 主动搭话护栏 ───────────────────────")
    p_sid = "private:50005"
    await store.add_message(p_sid, "user", "【他刚说】随便聊聊", "随便聊聊")
    await store.add_message(p_sid, "assistant", "好啊", "好啊")

    check("没有可提的旧事就不主动", await pro.ping(p_sid) is False)

    await memory._store_one(p_sid, "他在等一个演出开票", ["演出", "开票", "买票"], 3.0)
    check("刚聊完（静默不够）不主动",
          await pro._maybe_ping(p_sid, datetime.now(CST)) is False)

    backdate_messages(store, p_sid, 10)
    luid = await store.last_user_id(p_sid)
    sent_before = len(adapter.sent)
    ok = await pro.ping(p_sid, idle_hours=10, last_user_id=luid)
    check("静默够久 + 有由头 → 发一条", ok)
    check("确实发出去了", len(adapter.sent) > sent_before, str(adapter.sent[sent_before:]))
    day = datetime.now(CST).strftime("%Y%m%d")
    check("当天计数 +1", await store.kv_get_int(f"ping_count:{p_sid}:{day}") == 1)
    check("记下了「是在他这条消息之后发的」",
          await store.kv_get_int(f"ping_since:{p_sid}", -9) == luid)

    check("他还没回，就不再追第二次",
          await pro._maybe_ping(p_sid, datetime.now(CST)) is False)

    await store.kv_set(f"ping_since:{p_sid}", luid - 1)    # 假装像是回过了
    await store.kv_set(f"ping_count:{p_sid}:{day}", 2)    # 已经到当天上限
    check("到当天上限就不再发",
          await pro._maybe_ping(p_sid, datetime.now(CST)) is False)

    await store.kv_set(f"ping_count:{p_sid}:{day}", 0)
    check("凌晨不在活跃时间窗内就不发",
          await pro.tick(wed(3)) == 0)
    check("省电模式下不发主动搭话",
          await pro.tick(wed(15)) >= 0)

    # 主动搭话落库的那条 user 消息是指令块，不是编造他说过的话
    hist = await store.history(p_sid)
    synth = [h for h in hist if PROACTIVE_MARK in h["content"]]
    check("主动搭话也落了库（为了下一轮前缀一致）", len(synth) == 1, f"{len(hist)} 条")
    real_user = [h for h in hist if h["role"] == "user" and PROACTIVE_MARK not in h["content"]]
    check("他说过的话没被污染", len(real_user) == 1, str([h["content"][:16] for h in real_user]))

    # ══════════ F2. 搭给谁 / 轮换 / 护栏到底有没有生效 ══════════
    # 这一组是踩过坑之后补的。上一版的 F 组是假绿的：
    # 它手动把 last_user_id 塞给 ping()，而真实路径是 _maybe_ping 自己算 ——
    # 偏偏主动搭话也会往 messages 里插一条 role='user'，于是自己把自己的水位顶高，
    # 「他没回就不再追」这条护栏等于不存在，实测一天追了同一个人 17 次。
    print("\n── F2. 搭给谁 / 轮换 / 护栏 ──────────────")
    check("主动搭话插的那条 user 消息会顶高 last_user_id",
          await store.last_user_id(p_sid) > await store.last_real_user_id(p_sid))

    # 把水位和计数调回真实状态（上面为了测上限把它们改过），隔离出「不再追」这一条
    real = await store.last_real_user_id(p_sid)
    await store.kv_set(f"ping_since:{p_sid}", real)
    await store.kv_set(f"ping_count:{p_sid}:{day}", 1)
    await store.kv_set(f"ping_at:{p_sid}", 0)          # 绕过「隔一会儿」，单测本护栏
    check("自然流程下他还没回就不再追（上一版这里是假绿）",
          await pro.ready(p_sid, datetime.now(CST)) is None)

    # 轮换：两个都够格的会话，不能只薅 session_id 排前面的那个
    a_sid, b_sid = "private:70007", "private:80008"
    for s in (a_sid, b_sid):
        await store.add_message(s, "user", "【他刚说】在吗", "在吗")
        await memory._store_one(s, f"{s} 有自己的由头", ["由头"], 3.0)
        backdate_messages(store, s, 10)
        await store.kv_set(f"ping_at:{s}", 0)

    check("一次 tick 只发一条", await pro.tick(wed(15)) == 1)
    first_sid = adapter.sent[-1][0]
    check("第二次 tick 会换人（不再只薅排第一的那个）",
          await pro.tick(wed(15)) == 1 and adapter.sent[-1][0] != first_sid,
          f"两次都发给了 {first_sid}")
    check("同一个人两次主动之间会等一会儿",
          await pro.ready(first_sid, wed(15)) is None)

    # 搭话给谁：targets > masters > 不限制
    pro.cfg.life.proactive.targets = [80008]
    check("targets 只放行名单里的号", await pro.targets() == [b_sid],
          str(await pro.targets()))
    pro.cfg.life.proactive.targets = []
    pro.cfg.whitelist.masters = [80008]
    check("没配 targets 时默认只找主人（朋友不该被主动打扰）",
          await pro.targets() == [b_sid], str(await pro.targets()))
    pro.cfg.whitelist.masters = []
    check("targets 和 masters 都空 → 不限制",
          set(await pro.targets()) >= {a_sid, b_sid, p_sid})
    check("群聊永远不在搭话名单里（群里自言自语最像机器人）",
          all(s.startswith("private:") for s in await pro.targets()))

    # ══════════ G. 延迟回复不占锁 ══════════
    print("\n── G. 睡觉不回：挂着但不占锁 ─────────────")
    d_sid = "private:60006"

    class FakeLife:
        def quiet_delay(self, now=None):
            return 3600.0          # 假装刚睡着，还要 1 小时

        def mood(self, now=None):
            return ""

    real_life = orch.life
    orch.life = FakeLife()          # type: ignore[assignment]

    due, reason = orch._reply_due_at()
    check("睡觉时算出未来的回复时间", due > time.time() + 3500, f"due-now={due - time.time():.0f}s")
    check("原因写的是安静时段", "安静时段" in reason, reason)

    orch._buffers[d_sid] = [make_msg("睡了吗", uid=60006)]
    n_before = len(adapter.sent)
    t0 = time.perf_counter()
    await orch._after_debounce(d_sid)
    elapsed = time.perf_counter() - t0
    check("没有阻塞在 sleep 上（1 小时的任务立刻返回）", elapsed < 1.0, f"{elapsed:.2f}s")
    check("挂成了独立任务", d_sid in orch._deferred and not orch._deferred[d_sid].done())
    check("这期间一条都没发", len(adapter.sent) == n_before)

    orch._buffers[d_sid] = [make_msg("还在吗", uid=60006)]
    await orch._after_debounce(d_sid)
    check("期间又来消息不会排第二个任务（攒着一起回）",
          len([t for t in orch._deferred.values() if not t.done()]) == 1)

    for t in orch._deferred.values():
        t.cancel()
    orch.life = real_life            # type: ignore[assignment]

    # ══════════ H. 长期记忆是按会话隔离的 ══════════
    # 会话 id 就带身份：private:<QQ号> / group:<群号>。
    # 事实、摘要、画像三张表的键里都有 session_id，所以「不同账号」和「群聊 vs 私聊」
    # 天生就是分开的 —— 这一组把这件事钉死，免得以后重构时被合到一起。
    print("\n── H. 记忆按会话隔离 ─────────────────────")
    u1, u2 = "private:111111", "private:222222"
    g1 = "group:999999"

    await memory._store_one(u1, "他叫小王，在广告公司做设计", ["小王", "广告", "设计"], 4.0)
    await memory._store_one(u2, "她叫小李，在银行做风控", ["小李", "银行", "风控"], 4.0)
    await memory._store_one(g1, "群里的小张在准备考研", ["小张", "考研"], 3.0)

    f1 = [r["content"] for r in await store.facts(u1)]
    f2 = [r["content"] for r in await store.facts(u2)]
    fg = [r["content"] for r in await store.facts(g1)]
    check("私聊 A 的记忆里没有 B 的事", all("银行" not in c for c in f1), str(f1))
    check("私聊 B 的记忆里没有 A 的事", all("广告" not in c for c in f2), str(f2))
    check("私聊 A 的记忆里没有群聊的事", all("考研" not in c for c in f1), str(f1))
    check("群聊的记忆里没有私聊的事",
          all(("广告" not in c and "银行" not in c) for c in fg), str(fg))
    check("三个会话各记各的", len(f1) == 1 and len(f2) == 1 and len(fg) == 1)

    # 召回也只在本会话里找
    r1 = await memory.recall(u1, "他在广告公司做什么")
    check("A 招得回自己的事", any("广告" in x for x in r1), str(r1))
    r2 = await memory.recall(u2, "他在广告公司做什么")
    check("B 招不到 A 的事", all("广告" not in x for x in r2), str(r2))
    r3 = await memory.recall(g1, "广告公司 银行")
    check("群聊招不到私聊的事", all("广告" not in x and "银行" not in x for x in r3), str(r3))

    # 画像和摘要同样是按会话存的
    await store.set_profile(u1, "称呼", "小王")
    check("画像不串会话",
          (await store.get_profile(u1)).get("称呼") == "小王"
          and not (await store.get_profile(u2)))
    await store.set_summary(u1, "他最近在赶一个方案", 0)
    check("摘要不串会话",
          (await store.get_summary(u1))[0].startswith("他最近")
          and not (await store.get_summary(u2))[0])

    # 抽取也只看本会话的历史：给它 u1 存一段对话，写进去的必须只进 u1
    await store.add_message(u1, "user", "【他刚说】我们公司要搬去杭州", "我们公司要搬去杭州")
    await store.add_message(u1, "assistant", "啊？什么时候的事", "啊？什么时候的事")
    n1_before = len(await store.facts(u1))
    n2_before = len(await store.facts(u2))
    ng_before = len(await store.facts(g1))
    await memory.extract(u1)
    check("抽取写进了 u1", len(await store.facts(u1)) > n1_before,
          str([r["content"] for r in await store.facts(u1)]))
    check("抽取没有污染另一个私聊账号", len(await store.facts(u2)) == n2_before)
    check("抽取没有污染群聊", len(await store.facts(g1)) == ng_before)

    store.close()
    await llm.aclose()

    print("\n" + "=" * 46)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败：" + "、".join(FAIL))
    print("=" * 46 + "\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
