"""T4/T5: 算各回放存档的 n / 净额 / 90% 自助法区间.

用法(仓库根):  PYTHONPATH=. .venv/bin/python scripts/_t4_ci.py
              PYTHONPATH=. .venv/bin/python scripts/_t4_ci.py --include-dated

默认**跳过**日期命名的存档(如 shadow_rows_20260914.csv) —— 那是更早、口径不同的
样本(n=245, 净 -498U), 混进 T4/T5 报告会误导。需要时用 --include-dated 显式打开。
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys

from tools.shadow_replay import bootstrap_mean_ci

DATED = re.compile(r"^shadow_rows_\d{8}\.csv$")
DEFAULT_PATTERN = "logs/shadow_rows_*.csv"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def report(path):
    with open(path, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("filled") == "True"]
    nets = [v for v in (num(r.get("net")) for r in rows) if v is not None]
    if not nets:
        print(f"{os.path.basename(path):36s} n=0")
        return
    lo, hi = bootstrap_mean_ci(nets)
    tag = "PASS" if (len(nets) >= 20 and lo > 0) else ""
    print(f"{os.path.basename(path):36s} n={len(nets):3d} 净={sum(nets):+8.2f} "
          f"均={sum(nets) / len(nets):+6.2f} 90%CI=[{lo:+6.2f},{hi:+6.2f}] {tag}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pattern", default=DEFAULT_PATTERN)
    ap.add_argument("--include-dated", action="store_true",
                    help="把日期命名的旧存档也算进来(默认跳过)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    paths = sorted(glob.glob(args.pattern))
    skipped = []
    for path in paths:
        name = os.path.basename(path)
        if not args.include_dated and DATED.match(name):
            skipped.append(name)
            continue
        report(path)
    if skipped:
        print(f"(已跳过日期命名的旧存档 {len(skipped)} 个: {', '.join(skipped)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
