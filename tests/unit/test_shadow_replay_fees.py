"""回放费用模型: 单边费率必须能重现实盘 4 / 6 / 8 bps 三点。

实测(2026-09-18, trade_records/outcomes.csv 73 笔, fees/入场名义):
  最小 4.00 bps  = 两腿都走 maker
  中位 6.01 bps  = 一腿 maker 一腿 taker
  最大 8.04 bps  = 两腿都走 taker

修复前两处派生都多收: 默认支路 maker*0.4 + taker*0.8 = 1.20x 往返费率;
兜底支路 fee_rate x (入场 + 出场名义) = 2.00x 往返费率(实测 12.03 bps)。
"""
from __future__ import annotations

import random

import pytest

from tools.shadow_replay import (
    MAKER_SHARE,
    maker_fee_rate,
    simulate_plan,
    taker_fee_rate,
)
from tools.vergex_gens import gen_momo_breakout

#: 实盘往返费率中位(手续费 / 入场名义)
ROUND_TRIP = 0.000601
MAKER_BPS = 0.000400  # 两腿都走 maker
TAKER_BPS = 0.000804  # 两腿都走 taker


def _tl(n: int = 300, drift: float = 0.4) -> list[tuple]:
    out, price, base = [], 100.0, 1_700_000_000_000
    rng = random.Random(7)
    for i in range(n):
        o = price
        price = max(1.0, price + drift + rng.uniform(-0.6, 0.6))
        out.append((base + i * 900_000, o, price + 0.3, price - 0.3, price))
    return out


def _sim(plan, tl, **kwargs):
    return simulate_plan(
        plan, tl, risk_usdt=4.0, fee_rate=ROUND_TRIP, leverage=10, margin_usdt=250.0,
        partial_pct=50.0, timeout_min=60, time_stop_min=720, min_stop_pct=0.2,
        min_stop_atr=0.0, be_trigger=None, entry_gtx=True, **kwargs
    )


def test_side_rates_sum_to_the_round_trip_rate() -> None:
    maker, taker = maker_fee_rate(ROUND_TRIP), taker_fee_rate(ROUND_TRIP)
    assert maker + taker == pytest.approx(ROUND_TRIP, rel=1e-9)
    assert maker < taker
    share = MAKER_SHARE
    assert share == pytest.approx(1 / 3)


def test_split_reproduces_the_measured_maker_and_taker_round_trips() -> None:
    """两腿都走 maker = 2 x maker, 两腿都走 taker = 2 x taker."""
    maker = maker_fee_rate(ROUND_TRIP)
    taker = taker_fee_rate(ROUND_TRIP)
    assert maker == pytest.approx(MAKER_BPS / 2, rel=0.01)
    assert taker == pytest.approx(TAKER_BPS / 2, rel=0.01)


def test_market_entry_market_exit_matches_the_measured_taker_ceiling() -> None:
    """突破单(挂单即吃单)进出场都是 taker -> 8.0 bps, 与实测上界一致."""
    tl = _tl()
    plan = gen_momo_breakout(tl, "ZECUSDT", "15m")[0]
    out = _sim(plan, tl, maker_fee=maker_fee_rate(ROUND_TRIP),
               taker_fee=taker_fee_rate(ROUND_TRIP))
    assert out.filled
    assert out.fees / out.notional == pytest.approx(TAKER_BPS, rel=0.02)


def test_fallback_branch_no_longer_double_charges() -> None:
    """maker_fee/taker_fee 都为 None 时按往返费率收一次, 不是两条腿各收一次."""
    tl = _tl()
    plan = gen_momo_breakout(tl, "ZECUSDT", "15m")[0]
    out = _sim(plan, tl)
    assert out.filled
    effective = out.fees / out.notional
    assert effective == pytest.approx(ROUND_TRIP, rel=0.05)
    # 修复前这里是 2 * ROUND_TRIP
    assert effective < ROUND_TRIP * 1.5
