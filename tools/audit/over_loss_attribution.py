"""A1: 超计划亏损逐笔归因 — 止损腿滑点 vs 手续费 vs 残余(延迟/仓位漂移)。

用法:
    uv run python tools/audit/over_loss_attribution.py [--all-losses] [--refetch]

前提: /tmp/pa-a1-cache/{orders,user_trades}.json (tools/_pa_sim_common.fetch_account_data)
口径: outcomes.csv risk_usdt = 计划 1R, net_usdt = 已实现+费用。
     超损 = |net| - risk (仅亏损单); 拆:
       止损腿滑点 = 劣于 stop 价的平仓成交 Σ (stop-price)×qty×dir
       手续费     = 该笔全部 commission
       残余       = 超损 - 滑点 - 手续费 (仓位漂移/延迟/记录偏差)
已知的 ZEC 止损语义 bug 纪元单(ts_open>=2026-09-06 18:00)单列, 不入通用合计。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

CACHE = Path("/tmp/pa-a1-cache")
OUTCOMES = Path("trade_records/outcomes.csv")
ZEC_BUG_EPOCH_MS = 1788688800000  # 2026-09-06 18:00, 见 research/conf门槛回归分析.md


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-losses", action="store_true")
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()
    if args.refetch or not (CACHE / "user_trades.json").exists():
        print("cache missing; run tools/_pa_sim_common.fetch_account_data first")
        sys.exit(1)

    ut = json.load(open(CACHE / "user_trades.json"))
    rows = list(csv.DictReader(open(OUTCOMES, encoding="utf-8-sig")))
    picked = [
        r for r in rows
        if _f(r["net_usdt"]) < 0
        and (_f(r["risk_usdt"]) <= 0 or _f(r["net_usdt"]) / _f(r["risk_usdt"]) <= -1.0 or args.all_losses)
    ]

    head = (f"{'时间':11s} {'品种':9s} {'方向':4s} {'|net|':>7s} {'risk':>6s} {'超损':>6s} "
            f"{'滑点':>6s} {'费用':>5s} {'残余':>6s} {'退出腿':>22s}")
    print(head)
    generic = {"slip": 0.0, "fee": 0.0, "resid": 0.0, "excess": 0.0, "n": 0}
    bug = {"net": 0.0, "n": 0}
    for t in sorted(picked, key=lambda r: _f(r["net_usdt"])):
        sym, net, risk = t["symbol"], _f(t["net_usdt"]), _f(t["risk_usdt"])
        if sym == "ZECUSDT" and _f(t["ts_open"]) >= ZEC_BUG_EPOCH_MS:
            bug["net"] += net; bug["n"] += 1
            continue
        t0, t1 = _f(t["ts_open"]) - 120_000, _f(t["ts_close"]) + 120_000
        fills = [x for x in ut.get(sym, []) if t0 <= x["time"] <= t1]
        if not fills:
            fills = [x for x in ut.get(sym, []) if _f(t["ts_open"]) - 7_200_000 <= x["time"] <= _f(t["ts_open"]) + 28_800_000]
        if not fills:
            print(f"{sym} {t['direction'][:3]} net={net:.2f} 无成交回报(窗口内外均无)")
            continue
        side = 1 if "空" not in (t["direction"] or "做多") else -1
        entry_side = 1 if side == 1 else 2
        exits = [x for x in fills if x["side"] != entry_side]
        stop = _f(t["stop"])
        # 止损腿: 劣于 stop 的平仓成交(多单价 < stop / 空单价 > stop)
        stop_legs = [
            x for x in exits
            if stop > 0 and ((side == 1 and _f(x["price"]) < stop) or (side == -1 and _f(x["price"]) > stop))
        ]
        slip = sum((stop - _f(x["price"])) * _f(x["qty"]) * side for x in stop_legs)
        fee = sum(_f(x["commission"]) for x in fills)
        excess = abs(net) - risk if risk > 0 else 0.0
        resid = excess - slip - fee
        qty = sum(_f(x["qty"]) for x in exits)
        e_avg = sum(_f(x["price"]) * _f(x["qty"]) for x in exits) / qty if qty else 0.0
        when = datetime.fromtimestamp(_f(t["ts_open"]) / 1000).strftime("%m-%d %H:%M")
        print(f"{when:11s} {sym:9s} {t['direction'][:3]:4s} {abs(net):7.2f} {risk:6.2f} {excess:6.2f} "
              f"{slip:6.2f} {fee:5.2f} {resid:6.2f} {e_avg:.5f}×{qty:.3f} vs stop {stop:.5f}")
        generic["excess"] += max(excess, 0); generic["slip"] += max(slip, 0)
        generic["fee"] += fee; generic["resid"] += resid; generic["n"] += 1

    print(f"\n通用合计 {generic['n']} 笔 | 超损 {generic['excess']:.2f}U ≈ 滑点 {generic['slip']:.2f} "
          f"+ 手续费 {generic['fee']:.2f} + 残余 {generic['resid']:+.2f}")
    print(f"ZEC bug 纪元单 {bug['n']} 笔, 合计 {bug['net']:.2f}U (已修复的止损语义 bug, 单列)")


if __name__ == "__main__":
    main()
