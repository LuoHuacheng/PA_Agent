"""T2: 用自助法区间逐条裁定候选闸门(结论见 docs/闸门裁定表.md).

用法(仓库根):  PYTHONPATH=. .venv/bin/python scripts/_t2_gate_ci.py
"""
from __future__ import annotations

import csv

from tools.shadow_replay import _is_counter_trend, bootstrap_mean_ci

CH = ("broad_channel", "normal_channel", "trading_range")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(path="logs/shadow_rows.csv"):
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


def main():
    rows = load_rows()
    print(f"{'变体':<24} {'n':>4} {'净U':>9} {'均U':>7} {'90%CI':>21} R1")
    for name, sub in _cases(rows):
        nets = [r["net"] for r in sub]
        lo, hi = bootstrap_mean_ci(nets)
        need = 20 if "(R1a)" in name else (10 if "(R1b)" in name else None)
        verdict = "" if need is None else ("PASS" if (len(nets) >= need and lo > 0) else "fail")
        mean = sum(nets) / len(nets)
        print(f"{name:<24} {len(nets):>4d} {sum(nets):>+9.2f} {mean:>+7.2f} "
              f"[{lo:>+8.2f},{hi:>+8.2f}] {verdict}")


if __name__ == "__main__":
    main()
