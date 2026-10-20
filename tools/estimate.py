"""算一笔账：这个机器人一天到底要花多少钱。

    python tools/estimate.py                # 默认按 200 轮/天算
    python tools/estimate.py --turns 500
    python tools/estimate.py --compare      # 列出各种做法的对比

它不读数据库，纯粹按 token 模型推演，所以**上线前就能知道账单**。
里面每个数字都能在 config.yaml 里找到对应开关。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config, load_config  # noqa: E402
from app.context import SPEAKING_RULES, est_tokens  # noqa: E402


def simulate(cfg: Config, turns: int, *, model: str, thinking: bool,
             cache: bool, history_tokens: int, out_tokens: int,
             compress: bool = True) -> dict:
    """推演一天。

    token 模型（都是保守估计，实际只会更便宜）：
      静态前缀 = 人设 + 说话规矩          —— 永不变化，跨天命中
      记忆档案 = 摘要 + 画像              —— 只在压缩时变化
      历史逐轮累积                        —— 追加式，命中
      本轮动态包装 + 他的新消息            —— 每轮唯一会 miss 的部分
    """
    static = est_tokens(cfg.persona_text) + est_tokens(SPEAKING_RULES)
    memory = 100            # 摘要 + 画像的平均占用
    per_turn_in = 25        # 他一条消息大概这么多 token
    per_turn_out = 30       # 回复大概这么长（受限 max_tokens）
    wrapper = 25            # 【当前】/【他刚说】那层包装

    if thinking:
        per_turn_out += 400  # 思维链按输出价计费，这是最贵的一项

    hit = miss = out = 0
    history = 0
    compress_calls = 0

    for _ in range(turns):
        prefix = static + memory + history
        if cache:
            hit += prefix
            miss += wrapper + per_turn_in
        else:
            miss += prefix + wrapper + per_turn_in
        out += min(per_turn_out, out_tokens if not thinking else out_tokens + 400)

        history += per_turn_in + per_turn_out
        if compress and history > history_tokens * 1.5:
            # 跨过预算：付一次 cache miss 重建前缀，换后面长期命中
            compress_calls += 1
            miss += history          # 把老历史喂给模型生成摘要
            out += 300               # 摘要输出
            history = int(history_tokens * 0.3)   # 压完只剩摘要 + 最近几轮
            memory = 300

    cost = cfg.pricing.cost(model, hit, miss, out)
    return {
        "turns": turns, "hit": hit, "miss": miss, "out": out,
        "cost": cost, "compress_calls": compress_calls,
        "hit_rate": hit / (hit + miss) if (hit + miss) else 0.0,
    }


def y(v: float) -> str:
    if v < 0.01:
        return f"{v:.4f}"
    return f"{v:.2f}"


def row(label: str, r: dict, note: str = "") -> str:
    return (f"  {label:<26} {y(r['cost']):>8} 元   "
            f"{y(r['cost'] * 30):>7} 元/月   命中 {r['hit_rate'] * 100:>5.1f}%   {note}")


def main() -> int:
    ap = argparse.ArgumentParser(description="qq-companion 成本推演")
    ap.add_argument("--config", default=None)
    ap.add_argument("--turns", type=int, default=200, help="每天聊多少轮，默认 200")
    ap.add_argument("--compare", action="store_true", help="各种做法的横向对比")
    args = ap.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    n = args.turns
    peak = cfg.pricing.is_peak()
    model = cfg.llm.model

    print()
    print(f"  成本推演   每天 {n} 轮   {model}   当前是{'高峰' if peak else '空闲'}时段（估价随段浮动）")
    print("  " + "=" * 74)

    base = dict(model=model, thinking=False, cache=True,
                history_tokens=cfg.context.history_tokens, out_tokens=cfg.llm.max_tokens)
    a = simulate(cfg, n, **base)
    print("\n  【本项目的默认配置】")
    print(row("thinking 关 + 缓存命中", a, "← 你现在的配置"))
    print(f"     ├ 命中输入 {a['hit']:>12,} tok")
    print(f"     ├ 未命中输入 {a['miss']:>10,} tok")
    print(f"     ├ 输出 {a['out']:>15,} tok")
    print(f"     └ 其中触发了 {a['compress_calls']} 次记忆压缩")

    if args.compare:
        print("\n  【各种做法对比，同样每天 %d 轮】" % n)

        b = simulate(cfg, n, **{**base, "thinking": True})
        print(row("把 thinking 打开", b, "思维链按输出价烧"))

        c = simulate(cfg, n, **{**base, "cache": False})
        print(row("没有缓存优化", c, "动态内容塞 system / 滑动窗口"))

        d = simulate(cfg, n, **{**base, "cache": False, "thinking": True})
        print(row("两个坑都踩", d, "flash 便宜，这样也才一块钱"))

        e = simulate(cfg, n, **{**base, "history_tokens": 600, "out_tokens": 80})
        print(row("再叠省电模式", e, "上下文砍到 600、回复压到 80"))

        f = simulate(cfg, n, **{**base, "model": "deepseek-v4-pro"})
        print(row("换成 deepseek-v4-pro", f, "贵 6 倍，闲聊没必要"))

        # 复现"朋友一天 10 块"：贵模型 + 没缓存 + thinking 开着 + 上下文长
        g = simulate(cfg, n, model="deepseek-v4-pro", thinking=True, cache=False,
                     history_tokens=4000, out_tokens=200)
        g_turns = int(round(10.0 / max(g["cost"], 1e-9) * n))
        print(row("10 元/天 的典型配方", g,
                  f"v4-pro + 无缓存 + thinking，约 {g_turns} 轮/天就到 10 元"))

        print(f"\n  → 最好和最差差 {g['cost'] / max(a['cost'], 1e-9):.0f} 倍。"
              f"差距不在模型选择，而在配置。")
        print(f"    同样 {n} 轮，本项目的配置 {y(a['cost'])} 元，"
              f"换成上面那套配方 {y(g['cost'])} 元。")

        # 把账换算成"钱能买多少 token"，最直观
        for m in ("deepseek-flash", "deepseek-v4-pro"):
            t = cfg.pricing.currency_per_million.get(m)
            if not t:
                continue
            k = "peak" if peak else "offpeak"
            miss_price = t["cache_miss"][k]
            hit_price = t["cache_hit"][k]
            out_price = t["output"][k]
            print(f"\n  {m}（{'高峰' if peak else '空闲'}价，元/百万 token）")
            print(f"    10 元 = 未命中输入 {10 / miss_price * 1e6 / 1000:>9,.0f}K tok"
                  f"  |  命中输入 {10 / hit_price * 1e6 / 1e6:>7,.1f}M tok"
                  f"  |  输出 {10 / out_price * 1e6 / 1000:>7,.0f}K tok")
            print(f"    → 同样的 10 元，命中比未命中多买 {miss_price / hit_price:.0f} 倍。")

    print("\n  【结论】")
    print(f"    按现在这套配置，每天 {n} 轮大约 {y(a['cost'])} 元，"
          f"一个月 {y(a['cost'] * 30)} 元。")
    print(f"    日预算保护设在 {y(cfg.budget.daily_yuan)} 元，"
          f"花到 {y(cfg.budget.daily_yuan * cfg.budget.hard_stop_multiplier)} 元会硬停。")
    if peak:
        print("    现在算的是高峰价；晚上和周末是半价，实际会更低。")
    print("    想再降：① 聊天时关掉 llm.thinking ② 少发长回复（max_tokens）")
    print("              ③ 别用滑动窗口 ④ 本地规则回复能加就加")
    print("  " + "=" * 74 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
