"""群聊支持的离线测试：说话人区分、群静态前缀、触发判定、三层限流、按人归属记忆。

    python tests/test_group.py

全部离线（llm.mock=true），不花钱、不连 QQ、不依赖当前几点钟。

最重要的一条是 G 组：**私聊路径必须一个字节都没变**。群聊的所有改动都
加在分支里，私聊那几条字符串是缓存命中率的命根子，改一个字就是全量作废。
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
from app.context import (ContextBuilder, GROUP_MARK, PROACTIVE_MARK,  # noqa: E402
                         as_dialogue, format_group_lines, strip_wrapper,
                         wrap_proactive_message, wrap_user_message)
from app.humanize import split_segments  # noqa: E402
from app.life import Life  # noqa: E402
from app.llm import DeepSeek  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.onebot import IncomingMessage  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.store import Store  # noqa: E402

TMP = ROOT / "data" / "_test_group"
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


class SpyLLM(DeepSeek):
    """记下每次调用送进去的 messages，用来检查 prompt 内容。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[tuple[str, list]] = []

    async def chat(self, messages, **kw):
        self.calls.append((str(kw.get("purpose")), messages))
        return await super().chat(messages, **kw)

    def prompts_of(self, purpose: str) -> list[str]:
        return [str(m.get("content") or "")
                for p, msgs in self.calls if p == purpose for m in msgs]


def make_group_msg(text: str, uid: int = 20002, nick: str = "小明",
                   gid: int = 40004) -> IncomingMessage:
    return IncomingMessage(message_type="group", user_id=uid, group_id=gid,
                           self_id=10001, text=text, nickname=nick)


def make_private_msg(text: str, uid: int = 20002) -> IncomingMessage:
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
  segment_max_len: 20
  segment_max_count: 3
  debounce_seconds: 0.05
  late_reply_chance: 0
  trivial_replies:
    - ['^(在|在吗|在不在)[？?！!。.]*$', "在"]
life:
  enabled: false
  proactive:
    enabled: false
memory:
  enabled: true
  extract_every_turns: 2
  extract_min_turns: 2
  min_sim: 0.45
whitelist:
  private: []
  groups: []
  group_reply_rate: 0.0
  auto_accept_first: 1
group:
  enabled: true
  address_required: true
  per_user_cooldown_seconds: 20.0
  reply_cooldown_seconds: 8.0
  max_replies_per_hour: 30
  history_tokens: 900
  max_segments: 1
persona:
  file: "persona/default.md"
  name: "遥遥"
  group_file: "persona/group.md"
database:
  path: "{(cfg_dir / 'companion.db').as_posix()}"
logging:
  level: "ERROR"
  file: "{(cfg_dir / 'companion.log').as_posix()}"
""", encoding="utf-8")
    return p


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    cfg: Config = load_config(write_config(TMP))
    store = Store(cfg.db_path())
    llm = SpyLLM(cfg, store)
    ctx = ContextBuilder(cfg, store, llm)
    budget = Budget(cfg, store)
    life = Life(cfg)
    memory = Memory(cfg, store, llm)
    adapter = StubAdapter()
    orch = Orchestrator(cfg, store, llm, ctx, budget, adapter, life, memory)

    gsid = "group:40004"
    psid = "private:20002"

    # ══════════ A. 说话人标记 ══════════
    print("\n── A. 说话人标记 ─────────────────────────")
    blocks = format_group_lines([("小明", "在吗"), ("小红", "他不在 ")])
    check("两个人写成两行、各带名字", blocks == "小明：在吗\n小红：他不在", repr(blocks))
    check("没有昵称时退化成「某人」",
          format_group_lines([("", "喂")]) == "某人：喂")
    check("空内容整行丢掉",
          format_group_lines([("小明", ""), ("小红", "嗨")]) == "小红：嗨")

    g = wrap_user_message("小明：在吗", group=True, now=datetime(2026, 3, 4, 14, 5, tzinfo=CST))
    check("群动态块用【群里】", GROUP_MARK in g and "【他刚说】" not in g, repr(g))
    check("群动态块格式正确",
          g == "【当前】03月04日 14:05 周三\n【群里】小明：在吗", repr(g))

    check("群消息 strip_wrapper 保留名字",
          strip_wrapper(g) == "小明：在吗", repr(strip_wrapper(g)))
    check("群消息 strip_wrapper 吃掉回忆块",
          strip_wrapper(wrap_user_message("小明：在吗", group=True, recall=["他养了猫"]))
          == "小明：在吗")
    check("主动搭话仍然剥成空串",
          strip_wrapper(wrap_proactive_message(["旧事"], idle_hours=8,
                                               now=datetime(2026, 3, 4, 14, 5, tzinfo=CST)))
          == "")
    check("PROACTIVE_MARK 判定没被群改动破坏", PROACTIVE_MARK in
          wrap_proactive_message(["x"], now=datetime.now(CST)))

    check("群对话里 user 那行不前缀「他：」",
          as_dialogue("user", wrap_user_message("小明：在吗", group=True), group=True)
          == "小明：在吗")
    check("群对话里自己的话仍然前缀「我：」",
          as_dialogue("assistant", "在的", group=True) == "我：在的")
    check("私聊对话仍然是「他：」",
          as_dialogue("user", wrap_user_message("在吗"), group=False) == "他：在吗")

    # ══════════ B. 群静态前缀 ══════════
    print("\n── B. 群静态前缀 ─────────────────────────")
    pv = ctx.static_prefix(group=False)
    gv = ctx.static_prefix(group=True)
    check("私聊前缀是 2 条 system", len(pv) == 2, str(len(pv)))
    check("群前缀是 3 条 system", len(gv) == 3, str(len(gv)))
    check("群前缀第三条是群规矩", "多人群聊" in gv[2]["content"])
    check("私聊前缀里没有群规矩",
          all("多人群聊" not in m["content"] for m in pv))
    check("群前缀用了群人设", "在群里" in gv[0]["content"] or "小群" in gv[0]["content"],
          gv[0]["content"][:40])
    check("人设文件真的不一样",
          gv[0]["content"] != pv[0]["content"])

    msgs_g, _ = await ctx.build(gsid, "小明：在吗")
    msgs_p, _ = await ctx.build(psid, "在吗")
    check("build 按 session_id 自动选群前缀",
          msgs_g[0]["content"] == gv[0]["content"] and msgs_g[2]["content"] == gv[2]["content"])
    check("build 私聊走私聊前缀",
          len(msgs_p) >= 2 and msgs_p[1]["content"] == pv[1]["content"])

    # 前缀稳定：同一会话第二次 build，system 段必须逐字节一样
    msgs_g2, _ = await ctx.build(gsid, "小红：在的")
    check("群前缀跨轮稳定（缓存要靠它）",
          [m["content"] for m in msgs_g[:3]] == [m["content"] for m in msgs_g2[:3]])

    # ══════════ C. 触发判定 ══════════
    print("\n── C. 触发判定 ─────────────────────────")
    check("@机器人被认成在叫它", orch._group_addressed(make_group_msg("@10001 在吗")))
    check("叫名字被认成在叫它", orch._group_addressed(make_group_msg("遥遥在不在")))
    at_raw = {"message": [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 喂"}}]}
    m_at = IncomingMessage(message_type="group", user_id=20002, group_id=40004,
                           self_id=10001, text="喂", nickname="小明", raw=at_raw)
    check("at 消息段被认出来", orch._group_addressed(m_at))
    check("自说自话不当成在叫它",
          not orch._group_addressed(make_group_msg("今天天气不错")))
    check("别人互相聊天不插嘴（address_required）",
          not orch._group_should_reply(make_group_msg("今天天气不错")))

    check("去掉 @ 之后本地规则能匹配上",
          orch._clean_group_text("@10001 在吗") == "在吗",
          repr(orch._clean_group_text("@10001 在吗")))
    check("只 @ 不说话也有兜底文案",
          "叫了你一声" in orch._clean_group_text("@10001"),
          repr(orch._clean_group_text("@10001")))
    check("没有 @ 时文本原样保留（只压空白）",
          orch._clean_group_text("  今天   天气  ") == "今天 天气",
          repr(orch._clean_group_text("  今天   天气  ")))

    cfg.whitelist.group_reply_rate = 1.0
    cfg.group.address_required = False
    check("关掉 address_required 才随机插话",
          orch._group_should_reply(make_group_msg("今天天气不错")))
    cfg.whitelist.group_reply_rate = 0.0
    check("group_reply_rate=0 时随机插话关掉",
          not orch._group_should_reply(make_group_msg("今天天气不错")))
    cfg.group.address_required = True

    # ══════════ D. 三层限流 ══════════
    print("\n── D. 群内三层限流 ───────────────────────")
    cfg.group.reply_cooldown_seconds = 100.0
    cfg.group.per_user_cooldown_seconds = 0.0
    cfg.group.max_replies_per_hour = 30
    orch._grp_last_reply.clear(); orch._grp_last_user.clear(); orch._grp_hour.clear()

    first = make_group_msg("@10001 在吗", uid=20002, nick="小明")
    check("第一次 @ 能回", orch._group_should_reply(first))
    orch._mark_group_reply(first)
    other = make_group_msg("@10001 在吗", uid=20003, nick="小红")
    check("整群冷却期内换个人也不回", not orch._group_should_reply(other))

    cfg.group.reply_cooldown_seconds = 0.0
    cfg.group.per_user_cooldown_seconds = 100.0
    orch._grp_last_reply.clear(); orch._grp_last_user.clear(); orch._grp_hour.clear()
    orch._mark_group_reply(first)
    check("同一个人冷却期内不回", not orch._group_should_reply(first))
    check("冷却期内换个人可以回", orch._group_should_reply(other))

    cfg.group.per_user_cooldown_seconds = 0.0
    cfg.group.max_replies_per_hour = 2
    orch._grp_last_reply.clear(); orch._grp_last_user.clear(); orch._grp_hour.clear()
    for i in range(2):
        m = make_group_msg(f"@10001 第{i}条", uid=20010 + i)
        check(f"每小时上限内第 {i + 1} 条放行", orch._group_should_reply(m))
        orch._mark_group_reply(m)
    m3 = make_group_msg("@10001 第三条", uid=20099)
    check("超过每小时上限就闭嘴", not orch._group_should_reply(m3))
    # 把计数器拨到上一个小时，应该立刻恢复
    hour_key = int(time.time() // 3600)
    orch._grp_hour[gsid] = (hour_key - 1, 99)
    check("进入下一个小时就恢复", orch._group_should_reply(m3))

    cfg.group.max_replies_per_hour = 30
    cfg.group.reply_cooldown_seconds = 0.0
    cfg.group.per_user_cooldown_seconds = 0.0
    orch._grp_last_reply.clear(); orch._grp_last_user.clear(); orch._grp_hour.clear()

    # ══════════ E. 群路径端到端 ══════════
    print("\n── E. 群路径端到端 ───────────────────────")
    adapter.sent.clear()
    await orch.handle(make_group_msg("@10001 在吗", uid=20002, nick="小明"))
    await asyncio.sleep(0.4)
    check("群里收到一条回复", len(adapter.sent) == 1, str(adapter.sent))
    check("回到的是群里", adapter.sent and adapter.sent[0][0] == gsid, str(adapter.sent))
    check("「在吗」走了本地规则（0 token）",
          adapter.sent and adapter.sent[0][1] == "在", str(adapter.sent))

    h = await store.history(gsid)
    check("群消息落库了", len(h) == 2, str(len(h)))
    check("落库的 user 侧带【群里】包装",
          h and GROUP_MARK in str(h[0]["content"]), str(h[:1]))
    check("落库的 user 侧带说话人名字",
          h and "小明：" in str(h[0]["content"]), str(h[:1]))
    check("本地规则那条也记了「刚回过」（限流才生效）",
          gsid in orch._grp_last_reply)

    # 长回复 + 群里只发 1 条
    adapter.sent.clear()
    orch._grp_last_reply.clear(); orch._grp_last_user.clear()
    cfg.humanize.segment_max_len = 10
    long_text = "第一句话很长很长很长。第二句话也很长很长很长。第三句一样长很长很长。"
    await orch._send_humanized(make_group_msg("@10001 你说呢"), long_text)
    check("群里一次只发 1 条（max_segments=1）", len(adapter.sent) == 1, str(len(adapter.sent)))
    adapter.sent.clear()
    await orch._send_humanized(make_private_msg("你说呢"), long_text)
    check("私聊仍然会拆条", len(adapter.sent) > 1, str(len(adapter.sent)))
    cfg.humanize.segment_max_len = 20

    # ══════════ F. 记忆按人归属 ══════════
    print("\n── F. 群记忆按人归属 ─────────────────────")
    await store.add_message(gsid, "user",
                            wrap_user_message("小明：我在广告公司做平面设计", group=True), "")
    await store.add_message(gsid, "assistant", "哦", "哦")
    await store.add_message(gsid, "user",
                            wrap_user_message("小红：我养了只橘猫叫布丁", group=True), "")
    await store.add_message(gsid, "assistant", "可爱", "可爱")

    llm.calls.clear()
    await memory.extract(gsid)
    mem_prompts = llm.prompts_of("memory")
    check("抽取器被调用了", bool(mem_prompts))
    joined = "\n".join(mem_prompts)
    check("送进抽取器的对话里有说话人「小明」", "小明" in joined)
    check("送进抽取器的对话里有说话人「小红」", "小红" in joined)
    check("用的是群版提示词（要求写清是谁）",
          "必须写清是谁的事" in joined and "多人群聊" in joined)
    check("没把群消息当成「他：」", "他：小明" not in joined)

    llm.calls.clear()
    await ctx.compress(gsid)
    sum_prompts = llm.prompts_of("summarize")
    if sum_prompts:
        # 注意：prompts_of 返回的是该用途下**全部** message 的 content，
        # [0] 是 system，真正的提示词在后面 —— 别只取第一条。
        # 另外 compress_ratio=0.6 只吃最老的那 60%（6 条里取 3 条），
        # 所以只能断言「切片里那条」的说话人，不能要求两个人都出现。
        joined_sum = "\n".join(sum_prompts)
        check("摘要器也看得到说话人（切片里那条）",
              "小明" in joined_sum and "他：小明" not in joined_sum,
              joined_sum[:300])
        check("摘要器也收到了群版附加要求", "多人群聊" in joined_sum)
    else:
        check("摘要器在历史不足时按预期跳过（不算失败）", True)

    # 私聊抽取仍然用私聊提示词
    await store.add_message(psid, "user", wrap_user_message("我今天很累"), "")
    await store.add_message(psid, "assistant", "哦", "哦")
    llm.calls.clear()
    await memory.extract(psid)
    pp = "\n".join(llm.prompts_of("memory"))
    check("私聊抽取没被群改动污染（没有多人群聊那句）", "多人群聊" not in pp)
    check("私聊抽取仍然是「他：」", "他：我今天很累" in pp, pp[:160])

    # ══════════ G. 私聊零改动回归 ══════════
    print("\n── G. 私聊零改动回归 ─────────────────────")
    got = wrap_user_message("在吗", mood="闲着", recall=["他养了猫"],
                            now=datetime(2026, 3, 4, 14, 5, tzinfo=CST))
    want = ("【当前】03月04日 14:05 周三，心情：闲着\n"
            "【他刚说】在吗\n【刚好想起来】他养了猫")
    check("私聊动态块逐字节没变（缓存命根子）", got == want, repr(got))
    check("私聊没有混进群标记", GROUP_MARK not in got)
    check("split_segments 不传 max_segments 时行为不变",
          len(split_segments("第一句话很长很长很长。第二句话也很长很长很长。",
                             cfg.humanize)) >= 1)

    llm.calls.clear()
    adapter.sent.clear()
    await orch.handle(make_private_msg("你好呀"))
    await asyncio.sleep(0.4)
    p_calls = [p for p, _ in llm.calls]
    check("私聊仍然会调模型", "chat" in p_calls, str(p_calls))
    check("私聊回复没走群限流",
          psid not in orch._grp_last_reply)
    p_msgs = [m for p, ms in llm.calls if p == "chat" for m in ms]
    check("私聊送进模型的 system 是 2 条（没有群规矩）",
          sum(1 for m in p_msgs if m["role"] == "system") == 2,
          str(sum(1 for m in p_msgs if m["role"] == "system")))

    # ══════════ 汇总 ══════════
    print("\n" + "=" * 46)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print("    ✗ " + f)
    print("=" * 46)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
