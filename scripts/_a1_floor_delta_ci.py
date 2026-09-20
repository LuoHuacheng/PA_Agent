#!/usr/bin/env python3
"""A1: 止损下限档位增量的自助法区间(2026-09-20).

问题: 把 min_stop_atr_multiple 从 1.5 降到 1.2 / 1.0, 多出来的那批
"窄结构止损"单子, 期望是正是负? 只在增量本身不劣时, 降档才算提量而非摊薄。

口径: 区间 = bootstrap_mean_ci(20000, alpha=0.10, seed=7); 判定照禁令 1。

用法(仓库根)
    PYTHONPATH=. .venv/bin/python scripts/_a1_floor_delta_ci.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.shadow_replay import bootstrap_mean_ci  # noqa: E402

PAIRS = [
    ("1.5 -> 1.2", "logs/shadow_rows_20260920_atr15.csv", "logs/shadow_rows_20260920_atr12.csv"),
    ("1.2 -> 1.0", "logs/shadow_rows_20260920_atr12.csv", "logs/shadow_rows_20260920_atr10.csv"),
    ("1.5 -> 1.0", "logs/shadow_rows_20260920_atr15.csv", "logs/shadow_rows_20260920_atr10.csv"),
]


def load(path):
    with open(path, encoding="utf-8-sig") as fh:
        out = {}
        for r in csv.DictReader(fh):
            if r["filled"] != "True":
                continue
            r["net"] = float(r["net"])
            r["rr"] = float(r["rr"] or 0)
            out[r["record_file"]] = r
        return out


def line(label, rows):
    n = len(rows)
    if n == 0:
        print(f"{label:<28} {0:>4} {'-':>9} {'-':>8} {'-':>6} {'-':>9} {'-':>9}")
        return
    nets = [r["net"] for r in rows]
    lo, hi = bootstrap_mean_ci(nets)
    wins = sum(1 for x in nets if x > 0)
    print(f"{label:<28} {n:>4} {sum(nets):>+9.2f} {sum(nets) / n:>+8.2f} {wins / n * 100:>5.0f}% "
          f"{lo:>+9.2f} {hi:>+9.2f}")
    return lo


def main():
    print(f"{'增量口径':<28} {'n':>4} {'净U':>9} {'均U':>8} {'胜率':>6} {'90%下':>9} {'90%上':>9}")
    print("-" * 82)
    for label, lo_path, hi_path in PAIRS:
        lo_rows, hi_rows = load(lo_path), load(hi_path)
        delta = [hi_rows[k] for k in hi_rows if k not in lo_rows]
        line(label + " 增量", delta)
        if delta:
            ordered = sorted(delta, key=lambda r: r["record_time"])
            half = len(ordered) // 2
            line("  前半段", ordered[:half])
            line("  后半段", ordered[half:])
            worst = max(ordered, key=lambda r: r["net"])
            best = min(ordered, key=lambda r: r["net"])
            line(f"  去掉最大盈单({worst['net']:+.1f})", [r for r in ordered if r is not worst])
            line(f"  去掉最大亏单({best['net']:+.1f})", [r for r in ordered if r is not best])
            by_sym = {}
            for r in ordered:
                by_sym.setdefault(r["symbol"], []).append(r)
            for sym, rs in sorted(by_sym.items(), key=lambda kv: -len(kv[1])):
                line(f"  仅 {sym}", rs)
            rr_lo = [r for r in ordered if r["rr"] < 1.5]
            line("  其中 rr<1.5", rr_lo)
            line("  其中 rr>=1.5", [r for r in ordered if r["rr"] >= 1.5])
            cyc = {}
            for r in ordered:
                cyc.setdefault(r["cycle_position"], []).append(r)
            for cp, rs in sorted(cyc.items(), key=lambda kv: -len(kv[1])):
                line(f"  {cp}", rs)
        print()


if __name__ == "__main__":
    main()
