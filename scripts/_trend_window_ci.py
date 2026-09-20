#!/usr/bin/env python3
"""趋势口径对比: 窗口修正 + 端点法 vs 斜率法 (2026-09-20).

背景
----
线上执行层原本取 daily_close_series(days=7) = days+2 根 klines 的首尾,
含当日未收盘 bar ⇒ 实际是 8 天跨度、且末端价随日内波动; 而回放的
_trend_pct_at 取最近 7 根(6 天跨度)。两者不是同一个门。
本脚本对比三种口径在"禁逆势"上的表现:

  A 旧口径(未修窗口, 首尾法)   logs/shadow_rows_20260920_atr12.csv
  B 修正窗口, 首尾法            logs/shadow_rows_20260920_fix_endpoint.csv
  C 修正窗口, 最小二乘斜率法    logs/shadow_rows_20260920_fix_slope.csv

口径与判定照 docs/实盘开闸禁令: 90% 自助法区间, 跨零 = 没有结论。

用法(仓库根)
    PYTHONPATH=. .venv/bin/python scripts/_trend_window_ci.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.shadow_replay import _is_counter_trend, bootstrap_mean_ci  # noqa: E402

CASES = [
    ("A 旧口径(8天跨度,首尾)", "logs/shadow_rows_20260920_atr12.csv"),
    ("B 修正窗口,首尾法", "logs/shadow_rows_20260920_fix_endpoint.csv"),
    ("C 修正窗口,斜率法", "logs/shadow_rows_20260920_fix_slope.csv"),
]


def load(path):
    with open(path, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r["filled"] == "True"]
    for r in rows:
        r["net"] = float(r["net"])
        r["trend_pct"] = float(r["trend_pct"]) if r["trend_pct"] else None
    return [r for r in rows if r["trend_pct"] is not None]


def show(label, sub):
    if not sub:
        print(f"  {label:<34} {0:>4}")
        return
    nets = [r["net"] for r in sub]
    lo, hi = bootstrap_mean_ci(nets)
    print(f"  {label:<34} {len(sub):>4} {sum(nets):>+9.2f} {sum(nets) / len(sub):>+7.2f} "
          f"{sum(1 for x in nets if x > 0) / len(sub) * 100:>5.0f}% [{lo:>+6.2f},{hi:>+6.2f}]"
          f" {'PASS' if lo > 0 else ''}")


def main():
    for name, path in CASES:
        if not Path(path).exists():
            print(f"跳过(缺文件): {name} -> {path}")
            continue
        rows = load(path)
        shortfree = [r for r in rows if r["direction"] != "做空"]
        ct = [r for r in shortfree if _is_counter_trend(r, 1.5)]
        keep = [r for r in shortfree if not _is_counter_trend(r, 1.5)]
        print(f"\n=== {name} ===  成交(带标签) {len(rows)}")
        print(f"  {'口径':<34} {'n':>4} {'净U':>9} {'均U':>7} {'胜率':>6} {'90%CI':>17}")
        show("全部成交", rows)
        show("现行: 禁空", shortfree)
        show("禁空 + 禁逆势(1.5%)", keep)
        show("禁空 + 禁逆势(1.0%)", [r for r in shortfree if not _is_counter_trend(r, 1.0)])
        show("禁空 + 禁逆势(2.0%)", [r for r in shortfree if not _is_counter_trend(r, 2.0)])
        print(f"  逆势多被拦: n={len(ct)} 净{sum(r['net'] for r in ct):+.2f}U")
        ordered = sorted(keep, key=lambda r: r["record_time"])
        half = len(ordered) // 2
        print("  对抗检验:")
        show("前半段", ordered[:half])
        show("后半段", ordered[half:])
        for sym in sorted({r["symbol"] for r in keep}):
            sub = [r for r in keep if r["symbol"] != sym]
            show(f"去掉 {sym}", sub)


if __name__ == "__main__":
    main()
