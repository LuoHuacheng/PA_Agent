#!/usr/bin/env python3
"""被闸门拦截的订单: 如果成交, 盈亏如何 (2026-09-20).

被拦方案的落盘位置是 `records/pending` 的 `stage2_decision.execution`
(`status=gate_blocked/rejected/skipped` + `gate_reasons`), 不是 CSV 账本
(见 `signal_pipeline.py:376` 的说明)。

两个口径:
  A 按原计划挂单    —— 只有价格回踩到 entry 才成交(与线上挂单行为一致),
                       未被触及的记 0, 所以净额只反映那几笔真会成交的
  B 决策时市价入场  —— 假设立刻以决策时刻收盘价进场, 结构止损/止盈不变
                       (回答"这些机会本身值多少钱", 不被"回踩没到"稀释)

口径与判定照 docs/实盘开闸禁令: 90% 自助法区间, 跨零 = 没有结论。
样本小(成交笔数两位数), 任何单组结论都不可当上线依据。

用法(仓库根)
    PYTHONPATH=. .venv/bin/python scripts/_blocked_orders_ci.py
    PYTHONPATH=. .venv/bin/python scripts/_blocked_orders_ci.py --detail
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.shadow_replay import (  # noqa: E402
    PENDING_DIR,
    bootstrap_mean_ci,
    load_plans,
    merge_timelines,
    simulate_plan,
)

BLOCKED = ("gate_blocked", "rejected", "skipped")
FEE_RATE = 0.0006
COMMON = dict(
    risk_usdt=4.0, fee_rate=FEE_RATE, leverage=20, margin_usdt=250.0,
    partial_pct=50.0, timeout_min=120.0, time_stop_min=720.0,
    min_stop_pct=0.2, min_stop_atr=1.2,
)


def blocked_status(days=45):
    """pending 文件名 -> (status, gate_reasons)."""
    out = {}
    for path in glob.glob(str(PENDING_DIR / "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        ex = ((doc.get("stage2_decision") or {}).get("execution") or {})
        st = ex.get("status")
        if st in BLOCKED:
            out[os.path.basename(path)] = (st, tuple(ex.get("gate_reasons") or []))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true", help="打印逐笔明细")
    args = ap.parse_args()

    all_plans = load_plans(PENDING_DIR, days=45)
    timelines = merge_timelines(all_plans)
    status = blocked_status()
    plans = [p for p in all_plans if os.path.basename(p.path) in status]
    print(f"被拦且可回放的计划单: {len(plans)} 条")
    print(f"(execution.status 归因: " + str(dict(collections.Counter(
        status[os.path.basename(p.path)][0] for p in plans))) + ")")
    print()

    def run(plan):
        return simulate_plan(plan, timelines.get((plan.symbol, plan.timeframe)) or [],
                             **COMMON)

    def market_entry(plan):
        """决策时刻可见的最后一根收盘价."""
        seq = [b for b in plan.bars if b[0] <= plan.record_ms] or plan.bars
        return dataclasses.replace(plan, entry=seq[-1][4]) if seq else plan

    def show(label, items):
        if not items:
            print(f"  {label:<30} {0:>4}")
            return
        nets = [o.net for _, o in items]
        filled = [o for _, o in items if o.filled]
        lo, hi = bootstrap_mean_ci(nets)
        wins = sum(1 for o in filled if o.net > 0)
        rate = f"{wins}/{len(filled)}" if filled else "-"
        print(f"  {label:<30} {len(items):>4}  成交{len(filled):>3}  胜{rate:>7}  "
              f"净{sum(nets):>+9.2f}  均{sum(nets) / len(items):>+6.2f}  [{lo:>+6.2f},{hi:>+6.2f}]")

    def group(items):
        by_status = collections.defaultdict(list)
        by_reason = collections.defaultdict(list)
        for (st, gr), o in items:
            by_status[st].append((st, o))
            key = str(gr[0]).split(":")[0] if gr else "(execute 层拒绝, 记录未存原因)"
            by_reason[key].append((st, o))
        return by_status, by_reason

    header = f"  {'分组':<30} {'n':>4}  {'':>8} {'':>9}  {'净U':>9} {'均U':>7}  {'90%CI':>15}"

    for name, maker in (("口径 A: 按原计划挂单", lambda p: run(p)),
                        ("口径 B: 决策时市价入场", lambda p: run(market_entry(p)))):
        items = [((status[os.path.basename(p.path)]), maker(p)) for p in plans]
        passed = [(("(闸门放行)", ()), maker(p)) for p in all_plans
                  if os.path.basename(p.path) not in status]
        print(f"=== {name} ===")
        print(header)
        show("全部被拦", items)
        show("对照: 闸门放行", passed)
        show("对照: 全样本", items + passed)
        import time as _time

        def _early(p):
            return _time.strftime("%Y-%m-%d", _time.localtime(p.record_ms / 1000)) < "2026-09-13"

        ok_plans = [q for q in all_plans if os.path.basename(q.path) not in status]
        print("  时间分层(闸门 2026-09-13 起进入配置, 09-18 15:11 上线):")
        show("    早期  被拦", [x for p, x in zip(plans, items) if _early(p)])
        show("    早期  放行", [x for p, x in zip(ok_plans, passed) if _early(p)])
        show("    闸门期 被拦", [x for p, x in zip(plans, items) if not _early(p)])
        show("    闸门期 放行", [x for p, x in zip(ok_plans, passed) if not _early(p)])
        by_status, by_reason = group(items)
        for st in BLOCKED:
            if st in by_status:
                show(f"  status={st}", by_status[st])
        print("  按拦因:")
        for k, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            show(f"    {k}", v)
        if args.detail:
            print("  逐笔(仅成交):")
            for (st, gr), o in items:
                if o.filled:
                    print(f"    {st:<12} {str(gr[0]).split(':')[0] if gr else '-':<22} "
                          f"{o.reason:<18} net={o.net:+.2f} R={o.net / 4.0:+.2f}")
        print()


if __name__ == "__main__":
    main()
