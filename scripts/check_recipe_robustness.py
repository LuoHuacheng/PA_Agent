"""T2 唯一 PASS 配方「做多 + 通道/区间」的稳健性检查.

问题: n=14, CI [+1.01, +4.90] 只是**一次**自助重采样的结果。它会不会是
  (a) 蒙特卡洛噪声?      -> 换种子 / 提高 iters
  (b) 少数几笔大赢单撑的? -> 逐笔刀切(jackknife) + 收益集中度
只有在三种检查下都不翻车的 PASS, 才配拿去实盘(T6)。

用法(仓库根):  PYTHONPATH=. .venv/bin/python scripts/check_recipe_robustness.py
"""
from __future__ import annotations

import argparse
import csv
import sys

from tools.shadow_replay import bootstrap_mean_ci

CHANNELS = ("broad_channel", "normal_channel", "trading_range")
#: 权威样本(默认命令跑完存档的那份)。logs/shadow_rows.csv 会被每次 --replay 覆盖, 别直接信它。
CSV = "logs/shadow_rows_baseline45.csv"


def load_nets(path=CSV):
    with open(path, encoding="utf-8-sig") as fh:
        filled = [r for r in csv.DictReader(fh) if r.get("filled") == "True"]
    nets = [
        float(r["net"])
        for r in filled
        if r["direction"] == "做多" and r["cycle_position"] in CHANNELS
    ]
    return nets


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=CSV, help=f"回放明细 CSV (默认 {CSV})")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    nets = load_nets(args.csv)
    print(f"样本文件: {args.csv}")
    print(f"配方样本 n={len(nets)} 净={sum(nets):+.2f}U")
    lo, hi = bootstrap_mean_ci(nets)
    print(f"[基线 seed=7 iters=20000] 90%CI=[{lo:+.2f},{hi:+.2f}] -> {'PASS' if lo > 0 else 'fail'}")

    los = [bootstrap_mean_ci(nets, seed=s)[0] for s in range(1, 21)]
    worse = sum(1 for x in los if x <= 0)
    print(f"[种子稳健] seed 1..20 下界: min={min(los):+.2f} max={max(los):+.2f} "
          f"同向(PASS)={20 - worse}/20")

    lo2, hi2 = bootstrap_mean_ci(nets, iters=100000)
    print(f"[加大重采样 iters=100000] 90%CI=[{lo2:+.2f},{hi2:+.2f}] -> {'PASS' if lo2 > 0 else 'fail'}")

    flips = []
    for i in range(len(nets)):
        sub = nets[:i] + nets[i + 1:]
        sub_lo = bootstrap_mean_ci(sub)[0]
        if sub_lo <= 0:
            flips.append((nets[i], sub_lo))
    print(f"[逐笔刀切] 去掉任一笔后仍 PASS: {len(nets) - len(flips)}/{len(nets)}")
    for net, sub_lo in sorted(flips, reverse=True):
        print(f"    去掉一笔 {net:+.2f}U 后下界={sub_lo:+.2f} -> 跨零")

    top = sorted(nets, reverse=True)[:3]
    total = sum(nets)
    top_txt = ", ".join(f"{x:+.2f}" for x in top)
    print(f"[集中度] 前三笔 = [{top_txt}] 合计 {sum(top):+.2f}U "
          f"占净额 {sum(top) / total * 100:.0f}%")
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    print(f"[分布] 盈 {len(wins)} 笔 合计 {sum(wins):+.2f}U | 亏 {len(losses)} 笔 合计 {sum(losses):+.2f}U")


if __name__ == "__main__":
    main()
