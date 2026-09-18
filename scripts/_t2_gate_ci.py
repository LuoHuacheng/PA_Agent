"""T2: 用自助法区间逐条裁定候选闸门(结论见 docs/闸门裁定表.md).

用法(仓库根):  PYTHONPATH=. .venv/bin/python scripts/_t2_gate_ci.py
              PYTHONPATH=. .venv/bin/python scripts/_t2_gate_ci.py --csv logs/shadow_rows.csv

默认读**权威样本** logs/shadow_rows_baseline45.csv。
不要默认读 logs/shadow_rows.csv —— 那个文件每次 --replay 都被覆盖(见
docs/闸门裁定表.md 第 6 节: T5 最后一轮把它换成了 --time-stop-min 100000 的样本,
配方净额从 +41.60U 变成 +38.85U)。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from tools.shadow_replay import _is_counter_trend, bootstrap_mean_ci

CH = ("broad_channel", "normal_channel", "trading_range")
DEFAULT_CSV = "logs/shadow_rows_baseline45.csv"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(path=DEFAULT_CSV):
    with open(path, encoding="utf-8-sig") as fh:
        rows = []
        for r in csv.DictReader(fh):
            if r["filled"] != "True":
                continue
            for k in ("net", "rr", "trade_conf", "trend_pct"):
                r[k] = num(r[k])
            rows.append(r)
    return rows


def _hour(r):
    return int(r["record_time"][11:13])


def _cases(rows):
    return [
        ("基线 全部", rows),
        ("禁 session(04-12时)", [r for r in rows if not 4 <= _hour(r) <= 11]),
        ("仅 session(04-12时)", [r for r in rows if 4 <= _hour(r) <= 11]),
        ("禁 neutral 诊断", [r for r in rows if r["diag_direction"] != "neutral"]),
        ("仅 neutral 诊断", [r for r in rows if r["diag_direction"] == "neutral"]),
        ("禁 trending_tr (R1a)", [r for r in rows if r["cycle_position"] != "trending_tr"]),
        ("仅 trending_tr", [r for r in rows if r["cycle_position"] == "trending_tr"]),
        ("禁逆势 1.5% (R1a)", [r for r in rows if not _is_counter_trend(r, 1.5)]),
        ("trade_conf>=55", [r for r in rows if (r["trade_conf"] or 0) >= 55]),
        ("仅做多 (R1a)", [r for r in rows if r["direction"] == "做多"]),
        ("仅通道/区间 (R1b)", [r for r in rows if r["cycle_position"] in CH]),
        ("做多+通道/区间 (R1b)",
         [r for r in rows if r["direction"] == "做多" and r["cycle_position"] in CH]),
        ("做多+RR>=1.5 (R1b)",
         [r for r in rows if r["direction"] == "做多" and (r["rr"] or 0) >= 1.5]),
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=DEFAULT_CSV, help=f"回放明细 CSV (默认 {DEFAULT_CSV})")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if not Path(args.csv).exists():
        print(f"样本文件不存在: {args.csv}", file=sys.stderr)
        print("先跑默认回放并存档, 见 docs/闸门裁定表.md 第 6 节。", file=sys.stderr)
        return 2

    rows = load_rows(args.csv)
    print(f"样本文件: {args.csv}  成交 {len(rows)} 笔")
    print(f"{'变体':<24} {'n':>4} {'净U':>9} {'均U':>7} {'90%CI':>21} R1")
    for name, sub in _cases(rows):
        nets = [r["net"] for r in sub]
        lo, hi = bootstrap_mean_ci(nets)
        need = 20 if "(R1a)" in name else (10 if "(R1b)" in name else None)
        verdict = "" if need is None else ("PASS" if (len(nets) >= need and lo > 0) else "fail")
        mean = sum(nets) / len(nets)
        print(f"{name:<24} {len(nets):>4d} {sum(nets):>+9.2f} {mean:>+7.2f} "
              f"[{lo:>+8.2f},{hi:>+8.2f}] {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
