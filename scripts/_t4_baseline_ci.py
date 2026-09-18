"""T4 补充: --baseline 入口不落 shadow_rows.csv, 用同引擎复算对照流逐笔净额与 90% 区间.

用法(仓库根):  PYTHONPATH=. .venv/bin/python scripts/_t4_baseline_ci.py
"""
from __future__ import annotations

from tools.shadow_replay import (
    DEFAULT_LEVERAGE,
    DEFAULT_MARGIN_USDT,
    DEFAULT_PARTIAL_PCT,
    DEFAULT_RISK_USDT,
    DEFAULT_TIME_STOP_MIN,
    DEFAULT_TIMEOUT_MIN,
    OUTCOMES_CSV,
    PENDING_DIR,
    bootstrap_mean_ci,
    fee_rate_from_outcomes,
    generate_baseline_plans,
    load_plans,
    maker_fee_rate,
    merge_timelines,
    simulate_plan,
    taker_fee_rate,
)

DAYS = 45
MIN_STOP_PCT = 0.2
MIN_STOP_ATR = 0.7


def nets_for(be_trigger):
    plans = load_plans(PENDING_DIR, days=DAYS)
    timelines = merge_timelines(plans)
    fee_rate = fee_rate_from_outcomes(OUTCOMES_CSV)
    maker, taker = maker_fee_rate(fee_rate), taker_fee_rate(fee_rate)
    nets = []
    for (sym, tf), tl in sorted(timelines.items()):
        for bp in generate_baseline_plans(tl, sym, tf, days=DAYS, target_rr=2.0):
            out = simulate_plan(
                bp, tl, risk_usdt=DEFAULT_RISK_USDT, fee_rate=fee_rate,
                leverage=DEFAULT_LEVERAGE, margin_usdt=DEFAULT_MARGIN_USDT,
                partial_pct=DEFAULT_PARTIAL_PCT, timeout_min=DEFAULT_TIMEOUT_MIN,
                time_stop_min=DEFAULT_TIME_STOP_MIN,
                min_stop_pct=MIN_STOP_PCT, min_stop_atr=MIN_STOP_ATR,
                be_trigger=be_trigger, entry_gtx=True,
                maker_fee=maker, taker_fee=taker,
            )
            if out.filled:
                nets.append(out.net)
    return nets


def main():
    for label, be in (("off", None), ("0.5r", 0.5)):
        nets = nets_for(be)
        lo, hi = bootstrap_mean_ci(nets)
        mean = sum(nets) / len(nets)
        verdict = "PASS" if (len(nets) >= 20 and lo > 0) else "fail"
        print(f"baseline-be={label:5s} n={len(nets):3d} 净={sum(nets):+8.2f} 均={mean:+6.2f} "
              f"90%CI=[{lo:+6.2f},{hi:+6.2f}] {verdict}")


if __name__ == "__main__":
    main()
