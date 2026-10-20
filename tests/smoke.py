"""离线冒烟测试：不连 QQ、不花钱，验证骨架是通的。

    python tests/smoke.py

重点验证三件事（出错就说明省 token 的设计被破坏了）：
  1. 前缀稳定性 —— 第 N+1 轮发出的 messages，前 N 轮的内容必须是它的**逐字节前缀**。
     这是缓存能命中的充要条件。一旦不成立，成本会涨 50 倍。
  2. 动态内容只出现在最后一条 —— 时间戳/心情不能溜进 system。
  3. 本地规则回复确实没有调模型（0 token）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.budget import Budget  # noqa: E402
from app.config import Config, load_config  # noqa: E402
from app.context import ContextBuilder, MASTER_IDENTITY, SPEAKING_RULES  # noqa: E402
from app.life import Life  # noqa: E402
from app.llm import DeepSeek  # noqa: E402
from app.memory import Memory  # noqa: E402
from app.onebot import IncomingMessage  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.store import Store  # noqa: E402

TMP = ROOT / "data" / "_smoke"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail and not cond else ""))


class StubAdapter:
    self_id = 10001

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, msg, text: str) -> None:
        self.sent.append((msg.session_id, text))


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
  trivial_replies:
    - ['^(在|在吗|在不在)[？?！!。.]*$', "在"]
life:
  enabled: false
  proactive:
    enabled: false
memory:
  enabled: true
  extract_every_turns: 2
database:
  path: "{(cfg_dir / 'companion.db').as_posix()}"
logging:
  level: "WARNING"
  file: "{(cfg_dir / 'companion.log').as_posix()}"
""", encoding="utf-8")
    return p


def make_msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_type="private", user_id=20002, group_id=None, self_id=10001,
        text=text, nickname="测试君",
    )


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    cfg: Config = load_config(write_config(TMP))

    print("\n── 配置 ──────────────────────────────────")
    check("mock 模式已开（不联网）", cfg.llm.mock)
    check("思考模式已关闭（省钱开关）", cfg.llm.thinking is False)
    check("人设读到了", len(cfg.persona_text) > 50, f"{len(cfg.persona_text)} 字")
    check("闲置时段判定可用", isinstance(cfg.pricing.is_peak(), bool))
    check("作息模块可用（测试里关掉了，避免半夜跑测试挂 6 小时）",
          Life(cfg).state() == "free")

    store = Store(cfg.db_path())
    llm = DeepSeek(cfg, store)
    ctx = ContextBuilder(cfg, store, llm)
    budget = Budget(cfg, store)
    life = Life(cfg)
    memory = Memory(cfg, store, llm)
    adapter = StubAdapter()
    orch = Orchestrator(cfg, store, llm, ctx, budget, adapter, life, memory)

    calls: list[list[dict]] = []      # 只记 purpose=chat 的（前缀校验只看这些）
    purposes: list[str] = []          # 所有调用的用途，按顺序
    real_chat = llm.chat

    async def spy(messages, **kw):
        purposes.append(str(kw.get("purpose") or "chat"))
        if (kw.get("purpose") or "chat") == "chat":
            calls.append([dict(m) for m in messages])
        return await real_chat(messages, **kw)

    llm.chat = spy  # type: ignore[assignment]

    sid = "private:20002"

    # ---- 第 1 轮：命中本地规则，不该花钱 ----
    print("\n── 第 1 轮：本地规则短路 ─────────────────")
    await orch.handle(make_msg("在吗"))
    await asyncio.sleep(0.5)
    check("回了「在」", any(t == "在" for _, t in adapter.sent), str(adapter.sent))
    check("没有调用模型（0 token）", len(calls) == 0, f"调了 {len(calls)} 次")

    # ---- 第 2、3、4 轮：正常链路 ----
    print("\n── 第 2~4 轮：正常回复 ───────────────────")
    for text in ("今天好累啊，甲方又让我改稿", "你说我是不是该辞职", "算了不说了，打游戏去"):
        await orch.handle(make_msg(text))
        await asyncio.sleep(0.6)

    check("产生了 3 次模型调用", len(calls) == 3, f"实际 {len(calls)}")
    check("每条都发出去了", len(adapter.sent) >= 4, f"共 {len(adapter.sent)} 条")

    # ---- 核心：前缀稳定性 ----
    print("\n── 核心校验：缓存前缀是否逐字节稳定 ──────")
    if len(calls) >= 2:
        m1, m2 = calls[0], calls[1]
        prefix_ok = len(m2) > len(m1) and all(
            m1[i]["role"] == m2[i]["role"] and m1[i]["content"] == m2[i]["content"]
            for i in range(len(m1))
        )
        check("第 1 轮的 messages 是第 2 轮的完整前缀", prefix_ok)
        if not prefix_ok:
            for i in range(min(len(m1), len(m2))):
                if m1[i] != m2[i]:
                    print(f"      第 {i} 条就不一样了：")
                    print(f"        上轮 {m1[i]['role']}: {str(m1[i]['content'])[:70]}")
                    print(f"        这轮 {m2[i]['role']}: {str(m2[i]['content'])[:70]}")
                    break

    if len(calls) >= 3:
        m2, m3 = calls[1], calls[2]
        ok = len(m3) > len(m2) and all(
            m2[i]["role"] == m3[i]["role"] and m2[i]["content"] == m3[i]["content"]
            for i in range(len(m2))
        )
        check("第 2 轮是第 3 轮的前缀（连续命中）", ok)

    if calls:
        m = calls[0]
        check("第 0 条是 system 人设", m[0]["role"] == "system")
        check("第 1 条是 system 说话规矩", m[1]["role"] == "system"
              and m[1]["content"].startswith(SPEAKING_RULES))
        # 私聊的身份锚必须在这个 system 里 —— 少了它模型会把对面当外人
        check("私聊 system 里有身份锚", MASTER_IDENTITY in m[1]["content"])
        check("私聊 system 里没有群规矩", "多人群聊" not in m[1]["content"])
        last = m[-1]["content"]
        check("动态内容只在最后一条", "【当前】" in last and "【他刚说】" in last)
        check("system 里没有时间戳",
              not any("【当前】" in str(x["content"]) for x in m if x["role"] == "system"))

        # 新消息必须原样保存在历史里，否则下一轮回放前缀就变了
        history = await store.history(sid)
        check("最后一轮的用户原文原样入库（含包装）",
              any(h["content"] == last for h in history))

    # ---- 历史与用量 ----
    print("\n── 数据落盘 ──────────────────────────────")
    history = await store.history(sid)
    check("历史条数是偶数（一问一答成对）", len(history) % 2 == 0, f"{len(history)} 条")
    check("user/assistant 交替", all(
        history[i]["role"] == "user" and history[i + 1]["role"] == "assistant"
        for i in range(0, len(history) - 1, 2)), str([h["role"] for h in history]))

    rows = store.query("SELECT cost_yuan, cache_hit, cache_miss, output_tokens, purpose FROM usage")
    check("usage 表有记录", len(rows) >= 3, f"{len(rows)} 行")
    if rows:
        chat_rows = [r for r in rows if r["purpose"] == "chat"]
        total = sum(r["cost_yuan"] for r in rows)
        check("成本算得出来（>0）", total > 0, f"{total} 元")
        print(f"      3 轮合计 {total:.6f} 元 → 折合每天聊 200 轮约 "
              f"{total / max(len(chat_rows), 1) * 200:.3f} 元")
        check("本地规则那轮没进 usage", len(chat_rows) == len(calls),
              f"chat 记账 {len(chat_rows)} 行 vs 调用 {len(calls)} 次")

    # ---- 上下文预算触发压缩 ----
    print("\n── 压缩触发 ──────────────────────────────")
    cfg.context.history_tokens = 60      # 人为压到很低，逼它压缩
    need = await ctx.needs_compression(sid)
    check("低预算下判定为需要压缩", need)
    if need:
        before = len(purposes)
        ok = await ctx.compress(sid)
        check("压缩执行成功", ok)
        check("压缩额外调用了一次模型（purpose=summarize）",
              len(purposes) == before + 1 and purposes[-1] == "summarize",
              str(purposes[before:]))
        summary, covered = await store.get_summary(sid)
        check("摘要写进库了", bool(summary.strip()) or covered > 0,
              f"summary={summary[:40]!r} covered={covered}")
        if covered:
            after = await store.history(sid, after_id=covered)
            check("压缩后未覆盖的历史变短", len(after) < len(history),
                  f"{len(after)} vs {len(history)}")

    # ---- sanitize ----
    print("\n── 洗稿 ──────────────────────────────────")
    from app.humanize import sanitize, split_segments
    dirty = "首先，希望你今天开心。**重点**是：\n1. 多喝热水\n2. 早点睡\n```python\nprint(1)\n```"
    clean = sanitize(dirty)
    print(f"      原文: {dirty!r}")
    print(f"      洗完: {clean!r}")
    check("干掉了 Markdown 粗体", "**" not in clean)
    check("干掉了代码块", "```" not in clean and "print" not in clean)
    check("干掉了列表编号", not any(f"{n}." in clean for n in "123"))
    check("干掉了客服腔", "希望" not in clean or "帮助" not in clean)

    segs = split_segments("嗯。我也觉得。不过你先别急", cfg.humanize)
    check("分条功能可用", len(segs) >= 1, str(segs))

    # ---- 主动搭话的标记常量不许分家 ----
    print("\n── 标记常量 ──────────────────────────────")
    from app.context import PROACTIVE_MARK
    from app.store import _PROACTIVE_MARK as STORE_MARK
    check("store 和 context 的主动搭话标记一致（分家会让护栏失效）",
          STORE_MARK == PROACTIVE_MARK, f"{STORE_MARK!r} != {PROACTIVE_MARK!r}")

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
