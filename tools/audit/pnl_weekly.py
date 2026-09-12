"""D1: 盈亏周报 — 一条命令看清当前纪元的盈利/胜率/出血点。

用法:
    uv run python tools/audit/pnl_weekly.py [--days 7] [--era ""]

口径与 tools/audit/over_loss_attribution.py 一致, 数据源 trade_records/outcomes.csv。
输出: 总账 → 平仓原因 → 方向 → 置信桶 → 周期位置 → 超计划亏损计数 → 费用占比。
"""
from __future__ import annotations

import argparse
import csv
import time
from collections import defaultdict
from pathlib import Path

OUTCOMES = Path("trade_records/outcomes.csv")


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _bucket(rows: list[dict], key_fn) -> dict[str, list]:
    by: dict[str, list] = defaultdict(lambda: [0, 0.0, 0])
    for r in rows:
        k = key_fn(r)
        by[k][0] += 1
        by[k][1] += _f(r["net_usdt"])
        by[k][2] += 1 if _f(r["net_usdt"]) > 0 else 0
    return by


def _print_bucket(title: str, by: dict[str, list], order: list[str] | None = None) -> None:
    print(f"\n=== {title}")
    items = order and [(k, by[k]) for k in order if k in by] or sorted(
        by.items(), key=lambda x: -x[1][0]
    )
    for k, (n, p, w) in items:
        print(f"  {k:22s} {n:3d}笔 净{p:+9.2f}U 胜率{w / n * 100 if n else 0:3.0f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--era", default="", help="只统计该纪元; 空 = 当前 settings 纪元")
    args = ap.parse_args()

    era = args.era
    if not era:
        try:
            from pa_agent.config.settings import load_settings

            era = load_settings().general.strategy_era
        except Exception:
            era = ""
    cutoff = (time.time() - args.days * 86400) * 1000
    rows = [
        r for r in csv.DictReader(open(OUTCOMES, encoding="utf-8-sig"))
        if _f(r["ts_open"]) >= cutoff and (not era or r.get("era", era) == era or not r.get("era"))
    ]
    if not rows:
        print(f"窗口 {args.days} 天 (era={era or '-'}) 无数据")
        return

    net = sum(_f(r["net_usdt"]) for r in rows)
    fees = sum(_f(r["fees_usdt"]) for r in rows)
    wins = sum(1 for r in rows if _f(r["net_usdt"]) > 0)
    risk_total = sum(_f(r["risk_usdt"]) for r in rows if _f(r["risk_usdt"]) > 0)
    over1r = sum(
        1 for r in rows
        if _f(r["risk_usdt"]) > 0 and _f(r["net_usdt"]) < 0
        and _f(r["net_usdt"]) / _f(r["risk_usdt"]) <= -1.0
    )
    print(f"=== 周报 era={era or '-'} 近{args.days}天")
    print(f"  {len(rows)} 笔 | 净 {net:+.2f}U | 胜率 {wins / len(rows) * 100:.0f}% | "
          f"期望 {net / risk_total * 100 if risk_total else 0:+.1f}%/风险金")
    print(f"  手续费 {fees:.2f}U ({abs(fees / net * 100) if abs(net) > 1e-9 else 0:.0f}% of 净额) | "
          f"超1R亏损 {over1r} 笔")

    _print_bucket("按平仓原因", _bucket(rows, lambda r: r["close_reason"] or "?"))
    _print_bucket("按方向", _bucket(rows, lambda r: (r["direction"] or "?")[:4]))
    _print_bucket(
        "按置信桶", _bucket(
            rows, lambda r: "<40" if _f(r["conf"]) < 40 else "40-55" if _f(r["conf"]) < 55
            else "55-70" if _f(r["conf"]) < 70 else ">=70"),
        ["<40", "40-55", "55-70", ">=70"],
    )
    _print_bucket("按周期位置", _bucket(rows, lambda r: (r["cycle_position"] or "?")[:18]))


if __name__ == "__main__":
    main()
