"""按硬门槛判定实盘期望是否显著为正."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.shadow_replay import bootstrap_mean_ci

MIN_N = {"R2": 30, "R3": 100}
MAX_DD = {"R2": 0.25, "R3": 0.30}


def max_drawdown(nets):
    """按时间顺序累计净额的峰值回撤(相对峰值)."""
    peak = equity = worst = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _load_rows(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def main(path="trade_records/outcomes.csv"):
    rows = _load_rows(path)

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    pairs = sorted((str(r.get("ts_close") or ""), num(r.get("net_usdt"))) for r in rows)
    nets = [n for _, n in pairs if n is not None]
    if not nets:
        print("FAIL: 无成交记录")
        return 1
    mean = sum(nets) / len(nets)
    lo, hi = bootstrap_mean_ci(nets)
    wr = sum(1 for n in nets if n > 0) / len(nets)
    dd = max_drawdown(nets)
    stage = "R3" if len(nets) >= MIN_N["R3"] else ("R2" if len(nets) >= MIN_N["R2"] else "样本不足")
    if stage == "样本不足":
        print(f"n={len(nets)} 均{mean:+.2f}U 胜率{wr:.0%} 90%CI[{lo:+.2f}, {hi:+.2f}] "
              f"最大回撤{dd:.1%} -> FAIL(样本不足, 需 n>={MIN_N['R2']})")
        return 1
    ok = mean > 0 and lo > 0 and dd < MAX_DD[stage]
    print(f"n={len(nets)} 均{mean:+.2f}U 胜率{wr:.0%} 90%CI[{lo:+.2f}, {hi:+.2f}] "
          f"最大回撤{dd:.1%} 阶段={stage} -> {'PASS' if ok else 'FAIL'}")
    print(f"门槛({stage}): n>={MIN_N[stage]}, 均>0, CI下界>0, 回撤<{MAX_DD[stage]:.0%}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
