#!/usr/bin/env python3
"""A2: 空单条件化放行的自助法区间(2026-09-20).

背景
----
\`block_short_entry=true\` 把全部空单一刀切。依据是 45 天回放里"空单净 -29.01U /
胜率 32%", 但那批样本是单边牛市(30 天 +30%)且未按周期位置与顺势/逆势细分。
本脚本把空单切成「顺势/逆势 × 周期」子集, 检验是否存在一个子集,
放行它不劣于现行"全禁"。

口径
----
区间 = tools.shadow_replay.bootstrap_mean_ci(20000 次, alpha=0.10, seed=7)。
判定照 docs/实盘开闸禁令 1: 下界 > 0 = PASS; 跨零 = 没有结论。

用法(仓库根)
    PYTHONPATH=. .venv/bin/python scripts/_a2_short_conditional_ci.py
    PYTHONPATH=. .venv/bin/python scripts/_a2_short_conditional_ci.py --csv logs/shadow_rows_20260920_atr15.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.shadow_replay import _is_counter_trend, bootstrap_mean_ci  # noqa: E402

CH = ("broad_channel", "normal_channel", "trading_range")
NEUTRAL = 1.5
DEFAULT_CSV = "logs/shadow_rows_countertrend_scan.csv"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path):
    with open(path, encoding="utf-8-sig") as fh:
        rows = []
        for r in csv.DictReader(fh):
            if r["filled"] != "True":
                continue
            r["net"] = num(r["net"]) or 0.0
            r["trend_pct"] = num(r["trend_pct"])
            r["rr"] = num(r["rr"])
            rows.append(r)
    return rows


def line(label, rows):
    nets = [r["net"] for r in rows]
    n = len(nets)
    if n == 0:
        print(f"{label:<34} {0:>4} {0.0:>+9.2f} {0.0:>+7.2f} {'-':>5} {0.0:>+9.2f} {-0.0:>+8.2f} {0.0:>+8.2f}  {'-':<6}")
        return
    net = sum(nets)
    mean = net / n
    wins = sum(1 for r in rows if r["net"] > 0)
    lo, hi = bootstrap_mean_ci(nets)
    verdict = "PASS" if lo > 0 else "fail"
    print(f"{label:<34} {n:>4} {net:>+9.2f} {mean:>+7.2f} {wins / n * 100:>4.0f}% "
          f"{lo:>+9.2f} {hi:>+8.2f} {mean:>+8.2f}  {verdict:<6}")


def header():
    print(f"{'子集':<34} {'n':>4} {'净U':>9} {'均U':>7} {'胜率':>5} {'90%下':>9} {'90%上':>8} {'净/风险':>8}  判定")
    print("-" * 96)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    args = ap.parse_args()

    rows = load(args.csv)
    print(f"样本: {args.csv} | 成交 {len(rows)} 笔")
    print()

    short = [r for r in rows if r["direction"] == "做空"]
    long_ = [r for r in rows if r["direction"] == "做多"]
    ct_short = [r for r in short if _is_counter_trend(r, NEUTRAL)]
    tw_short = [r for r in short if not _is_counter_trend(r, NEUTRAL)]

    print("=== 空单侧 ===")
    header()
    line("全部空单(对照)", short)
    line("逆势空", ct_short)
    line("顺势空", tw_short)
    line("顺势空 x 通道/区间", [r for r in tw_short if r["cycle_position"] in CH])
    line("顺势空 x trending_tr", [r for r in tw_short if r["cycle_position"] == "trending_tr"])
    line("顺势空 x 非neutral诊断", [r for r in tw_short if r["diag_direction"] != "neutral"])
    line("顺势空 x 通道/区间 x 非neutral",
         [r for r in tw_short if r["cycle_position"] in CH and r["diag_direction"] != "neutral"])
    print()

    print("=== 空单按周期(不分顺势) ===")
    header()
    for cp in ("trending_tr", "broad_channel", "normal_channel", "trading_range", "spike", "tight_channel"):
        line(f"空单 x {cp}", [r for r in short if r["cycle_position"] == cp])
    print()

    print("=== 多单侧(对照, 确认效应是否只属于空单) ===")
    header()
    line("全部多单", long_)
    line("逆势多", [r for r in long_ if _is_counter_trend(r, NEUTRAL)])
    line("顺势多", [r for r in long_ if not _is_counter_trend(r, NEUTRAL)])
    line("多单 x 通道/区间", [r for r in long_ if r["cycle_position"] in CH])
    line("多单 x trending_tr", [r for r in long_ if r["cycle_position"] == "trending_tr"])
    print()

    # 组合: 现行"全禁空" vs "禁空 + 条件化放行顺势通道空"
    print("=== 组合口径(相对现行 block_short_entry=true) ===")
    header()
    keep = [r for r in rows if r["direction"] != "做空"]
    add = [r for r in tw_short if r["cycle_position"] in CH and r["diag_direction"] != "neutral"]
    line("现行: 禁全部空单", keep)
    line("条件化: 禁空 + 放行顺势通道空", keep + add)
    print(f"  放行增量: n={len(add)} 净{sum(r['net'] for r in add):+.2f}U "
          f"(逐笔 {[round(r['net'], 2) for r in add]})")
    print()

    # 对抗检验: 时间对半
    print("=== 对抗检验: 时间对半(候选 = 顺势空 x 通道/区间 x 非neutral) ===")
    header()
    ordered = sorted(add, key=lambda r: r["record_time"])
    half = len(ordered) // 2
    line("前半段", ordered[:half])
    line("后半段", ordered[half:])
    # 留一品种
    for sym in sorted({r["symbol"] for r in add}):
        line(f"去掉 {sym}", [r for r in add if r["symbol"] != sym])


if __name__ == "__main__":
    main()
