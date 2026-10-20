"""成本报表：看看钱到底花在哪了。

    python tools/report.py              # 今天
    python tools/report.py --days 7     # 最近 7 天
    python tools/report.py --sessions   # 带上每个会话的明细

真正值得盯的只有一个数：**缓存命中率**。
它掉下去，账单就会涨几十倍，而其它指标看起来都还正常。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CST, load_config  # noqa: E402
from app.store import Store  # noqa: E402


def fmt_yuan(v: float) -> str:
    if v == 0:
        return "0"
    if v < 0.01:
        return f"{v:.4f}"
    return f"{v:.2f}"


def bar(ratio: float, width: int = 24) -> str:
    filled = int(round(ratio * width))
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser(description="qq-companion 成本报表")
    ap.add_argument("--config", default=None)
    ap.add_argument("--days", type=int, default=1, help="看最近几天，默认 1（今天）")
    ap.add_argument("--sessions", action="store_true", help="附上分会话明细")
    args = ap.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    store = Store(cfg.db_path())

    now = datetime.now(CST)
    days = max(1, args.days)
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    since_ts = since.timestamp()

    rows = store.query(
        "SELECT ts, session_id, model, purpose, cache_hit, cache_miss, output_tokens, "
        "cost_yuan, latency_ms FROM usage WHERE ts >= ? ORDER BY ts",
        (since_ts,),
    )
    store.close()

    print()
    print(f"  qq-companion 成本报表   {since:%Y-%m-%d} → {now:%Y-%m-%d}   "
          f"（当前{'高峰' if cfg.pricing.is_peak(now) else '空闲'}时段）")
    print("  " + "=" * 62)

    if not rows:
        print("\n  这段时间没有任何调用记录。")
        print("  跑起来之后再看：python main.py\n")
        return 0

    calls = len(rows)
    hit = sum(r["cache_hit"] for r in rows)
    miss = sum(r["cache_miss"] for r in rows)
    out = sum(r["output_tokens"] for r in rows)
    cost = sum(r["cost_yuan"] for r in rows)
    total_in = hit + miss
    hit_rate = hit / total_in if total_in else 0.0
    avg_latency = sum(r["latency_ms"] for r in rows) / calls

    print(f"\n  请求数        {calls}")
    print(f"  模型          {rows[-1]['model']}")

    # —— 唯一真正重要的指标 ——
    print(f"\n  缓存命中率    {hit_rate * 100:.1f}%   {bar(hit_rate)}")
    print(f"                命中 {hit:,} tok / 输入 {total_in:,} tok")
    if hit_rate < 0.5:
        print("                ✗ 偏低！未命中的输入比命中贵 50 倍，去查这三件事：")
        print("                  1. 历史里有没有插时间戳之类的动态内容（必须放最后一条）")
        print("                  2. 是不是用了滑动窗口，把最老的一轮丢了")
        print("                  3. 每轮 system 内容是否逐字节一致")

    # 如果这些输入全都没命中，会多花多少
    model = rows[-1]["model"]
    t = cfg.pricing.currency_per_million.get(model)
    if t:
        whatif = cfg.pricing.cost(model, 0, total_in, out, now)
        print(f"\n  实际花费      {fmt_yuan(cost)} 元")
        print(f"  假如零命中    {fmt_yuan(whatif)} 元"
              f"   ← 差 {whatif / cost:.1f} 倍" if cost > 0 else "")
        print(f"  ├ 输入(命中)  {fmt_yuan(cfg.pricing.cost(model, hit, 0, 0, now))} 元"
              f"   {hit:>10,} tok")
        print(f"  ├ 输入(未命中){fmt_yuan(cfg.pricing.cost(model, 0, miss, 0, now))} 元"
              f"   {miss:>10,} tok")
        print(f"  └ 输出        {fmt_yuan(cfg.pricing.cost(model, 0, 0, out, now))} 元"
              f"   {out:>10,} tok")

    print(f"\n  平均延迟      {avg_latency:.0f} ms")
    print(f"  每轮平均      {fmt_yuan(cost / calls)} 元 / {total_in / calls:.0f} tok 输入")

    # —— 分日 ——
    by_day: dict[str, dict] = {}
    for r in rows:
        d = datetime.fromtimestamp(r["ts"], CST).strftime("%m-%d")
        b = by_day.setdefault(d, {"calls": 0, "hit": 0, "miss": 0, "out": 0, "cost": 0.0})
        b["calls"] += 1
        b["hit"] += r["cache_hit"]
        b["miss"] += r["cache_miss"]
        b["out"] += r["output_tokens"]
        b["cost"] += r["cost_yuan"]

    if len(by_day) > 1:
        print("\n  分日：")
        print("    日期     请求    命中率    花费")
        for d, b in sorted(by_day.items()):
            tot = b["hit"] + b["miss"]
            hr = b["hit"] / tot * 100 if tot else 0
            print(f"    {d}   {b['calls']:>5}    {hr:>5.1f}%   {fmt_yuan(b['cost']):>8}")

    # —— 投影 ——
    span_days = max((now.date() - since.date()).days + 1, 1)
    per_day = cost / span_days
    print(f"\n  按当前节奏    {fmt_yuan(per_day)} 元/天")
    print(f"                月 ≈ {fmt_yuan(per_day * 30)} 元"
          f"，年 ≈ {fmt_yuan(per_day * 365)} 元")
    print(f"  日预算        {fmt_yuan(cfg.budget.daily_yuan)} 元（超了自动进省电模式）")

    # —— 分会话 ——
    if args.sessions:
        by_sess: dict[str, dict] = {}
        for r in rows:
            b = by_sess.setdefault(r["session_id"], {"calls": 0, "hit": 0, "miss": 0, "cost": 0.0})
            b["calls"] += 1
            b["hit"] += r["cache_hit"]
            b["miss"] += r["cache_miss"]
            b["cost"] += r["cost_yuan"]
        print("\n  分会话：")
        for sid, b in sorted(by_sess.items(), key=lambda kv: -kv[1]["cost"]):
            tot = b["hit"] + b["miss"]
            hr = b["hit"] / tot * 100 if tot else 0
            print(f"    {sid:<18} {b['calls']:>5} 轮  命中 {hr:>5.1f}%  "
                  f"{fmt_yuan(b['cost']):>8} 元")

    print("  " + "=" * 62 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
