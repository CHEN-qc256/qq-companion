"""拉取中国法定节假日，写进 config.yaml 的 pricing.holidays。

    python tools/holidays.py            # 看今年有哪些工作日放假（改价用）
    python tools/holidays.py --year 2027
    python tools/holidays.py --write    # 直接写进 config.yaml

为什么要这个：高峰时段（话费翻倍）只按「周一至周五」算，但法定节假日其实走空闲价。
把这些日子填进 pricing.holidays，一年能省下十几个高峰日的差价。

写进 config.yaml 时会用 `python -m pip install` 之外的零依赖方式（httpx 已在依赖里）。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT  # noqa: E402

API = "https://timor.tech/api/holiday/year/{year}"
API_FALLBACK = "https://api.apihubs.cn/holiday/get?year={year}&size=500&field=date,holiday"


def fetch_timor(year: int) -> Optional[Dict[str, str]]:
    """返回 {"2026-01-01": "元旦", ...}，只含「工作日放假」的那些天。"""
    import httpx

    try:
        r = httpx.get(API.format(year=year), timeout=15.0,
                      headers={"User-Agent": "qq-companion/holidays"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  timor.tech 取不到：{e}")
        return None

    if data.get("code") != 0:
        print(f"  timor.tech 返回异常：code={data.get('code')}")
        return None

    out: Dict[str, str] = {}
    for _, item in (data.get("holiday") or {}).items():
        if not item.get("holiday"):        # holiday=false 是「调休上班」，不是放假
            continue
        d = str(item.get("date") or "")
        if not d:
            continue
        try:
            if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
                continue                    # 周末本来就不算高峰，不用填
        except ValueError:
            continue
        out[d] = str(item.get("name") or "放假")
    return out


def fetch_apihubs(year: int) -> Optional[Dict[str, str]]:
    """备用源。返回结构和 fetch_timor 一样。"""
    import httpx

    try:
        r = httpx.get(API_FALLBACK.format(year=year), timeout=15.0,
                      headers={"User-Agent": "qq-companion/holidays"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  apihubs.cn 取不到：{e}")
        return None

    items = ((data.get("data") or {}).get("list")) or []
    if not items:
        print("  apihubs.cn 没有数据")
        return None

    out: Dict[str, str] = {}
    for it in items:
        raw = str(it.get("date") or "")
        if len(raw) != 8 or not raw.isdigit():
            continue
        d = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        if str(it.get("holiday")) not in ("1", "True", "true"):
            continue
        try:
            if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
                continue
        except ValueError:
            continue
        out[d] = "放假"
    return out


def write_into_config(cfg_path: Path, days: List[str]) -> bool:
    """只替换 pricing 下面那一行 holidays:，其余注释原样保留。"""
    if not cfg_path.exists():
        print(f"  找不到 {cfg_path.name}，先跑 python setup.py")
        return False

    text = cfg_path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^\s*holidays:", text):
        print(f"  {cfg_path.name} 里没有 holidays: 这一行")
        return False

    # 一口气把 holidays: 后面的内容（可能换行）全部吃掉，直到下一个顶格键或文件尾
    lines = "\n".join(f'             "{d}",' for d in days)
    body = f"  holidays: [\n{lines}\n           ]"
    new = re.sub(r"(?m)^\s*holidays:.*?(?=^\S|\Z)", body + "\n", text, count=1, flags=re.S)
    if new == text:
        print("  没变化")
        return False
    cfg_path.write_text(new, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="拉取中国法定节假日并写进配置")
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--write", action="store_true", help="写进 config.yaml")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    print(f"\n{cwd_show()}  ·  查 {args.year} 年的法定节假日\n")
    # 用 not days 而不是 is None：源返回空 dict 说明「这次没拿到可用的数据」
    # （接口变了 / 被限流 / 解析对不上），跟彻底连不上一样，都该换下一个源。
    days = fetch_timor(args.year)
    if not days:
        days = fetch_apihubs(args.year)
    if not days:
        print("\n  两个源都没取到。检查一下网络，或者手动填 pricing.holidays。")
        return 1

    ordered = sorted(days)
    print(f"  工作日放假共 {len(ordered)} 天：")
    for d in ordered:
        try:
            wd = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
        except ValueError:
            wd = "?"
        print(f"    {d}  周{wd}  {days[d]}")

    if args.write:
        # 绝不拿空列表去覆盖：空表多半是上面取数失败，而不是「真的没有节假日」。
        # 覆盖掉的后果是节假日按高峰价计费 —— 白多花一倍钱，而用户根本不会发现。
        if not ordered:
            print("\n  结果是空的，不覆盖配置里的节假日（怕把原来填好的洗掉）。")
            return 1
        if write_into_config(Path(args.config), ordered):
            print(f"\n  已写入 {args.config}")
            print("  效果：这些日子按空闲价计费（便宜一半）。")
        return 0

    print("\n  加 --write 就能写进 config.yaml。")
    return 0


def cwd_show() -> str:
    return ROOT.name


if __name__ == "__main__":
    raise SystemExit(main())
