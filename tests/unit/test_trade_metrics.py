"""Unit tests for trade_metrics helpers."""
from __future__ import annotations

from pa_agent.util.trade_metrics import (
    adjust_decision_stop_for_tp1_rr_cap,
    compute_risk_reward,
    format_estimated_win_rate,
    format_estimated_win_rate_reasoning,
    is_long_direction,
    max_risk_reward_ratio,
    min_risk_reward_ratio,
    widen_stop_for_tp1_rr_cap,
)


def test_is_long_direction():
    assert is_long_direction("做多") is True
    assert is_long_direction("做空") is False


def test_compute_risk_reward_short():
    rr = compute_risk_reward(4541, 4510, 4553, "做空")
    assert rr is not None
    assert rr["risk"] == 12
    assert rr["reward"] == 31


def test_rr_bounds_all_stances_share_one_floor() -> None:
    for stance in ("conservative", "balanced", "aggressive", "extreme_aggressive", None):
        assert min_risk_reward_ratio(stance) == 1.0
    # Upper cap 2.0: sane structural stops (RR<=2) are never widened by the program.
    assert max_risk_reward_ratio() == 2.0


def test_widen_stop_for_tp1_rr_cap_long():
    # entry=100, tp=110, stop=99 -> risk=1, reward=10, RR=10 > 2 -> widen to RR=2
    widened = widen_stop_for_tp1_rr_cap(100.0, 110.0, 99.0, "做多", tick=0.01)
    assert widened is not None
    assert widened == 95.0
    assert widened < 99.0
    rr = compute_risk_reward(100.0, 110.0, widened, "做多")
    assert rr is not None
    assert rr["ratio"] <= 2.0 + 1e-9
    assert rr["ratio"] >= 1.0 - 1e-9


def test_widen_stop_for_tp1_rr_cap_short():
    widened = widen_stop_for_tp1_rr_cap(100.0, 90.0, 101.0, "做空", tick=0.01)
    assert widened is not None
    assert widened == 105.0
    assert widened > 101.0
    rr = compute_risk_reward(100.0, 90.0, widened, "做空")
    assert rr is not None
    assert rr["ratio"] <= 2.0 + 1e-9


def test_adjust_decision_stop_for_tp1_rr_cap_mutates_decision():
    decision = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.0,
        "take_profit_price": 110.0,
        "stop_loss_price": 99.0,
    }
    assert adjust_decision_stop_for_tp1_rr_cap(decision, tick=0.01)
    assert decision["stop_loss_price"] == 95.0
    rr = compute_risk_reward(
        decision["entry_price"],
        decision["take_profit_price"],
        decision["stop_loss_price"],
        decision["order_direction"],
    )
    assert rr is not None
    assert rr["ratio"] <= 2.0 + 1e-9


def test_structural_stop_rr2_kept_short_no_widen():
    # ADAUSDT 16:10 复现: stop 0.2206 -> RR exactly 2.0 -> cap is NOT exceeded,
    # the structural stop must survive untouched (regression: a 1.0 cap used to
    # silently widen it to 0.2214 and double the risk).
    widened = widen_stop_for_tp1_rr_cap(0.2198, 0.2182, 0.2206, "做空", tick=0.0001)
    assert widened == 0.2206
    assert repr(widened) == "0.2206"


def test_structural_stop_rr_below_cap_kept_long():
    # ADAUSDT 12:00-style structure: RR 1.95 is kept under cap 2.0, so the
    # K1-low based stop 0.2210 is not silently widened to 0.2192 anymore.
    widened = widen_stop_for_tp1_rr_cap(0.2229, 0.2266, 0.2210, "做多", tick=0.0001)
    assert widened == 0.2210
    assert f"{widened:.4f}" == "0.2210"
    assert "99999" not in repr(widened)


def test_widen_only_for_extreme_rr_short():
    # risk 0.0004 vs reward 0.0016 -> RR 4 > 2 -> widen to RR 2.0
    widened = widen_stop_for_tp1_rr_cap(0.2198, 0.2182, 0.2202, "做空", tick=0.0001)
    assert widened == 0.2206
    assert repr(widened) == "0.2206"


def test_widen_only_for_extreme_rr_long():
    widened = widen_stop_for_tp1_rr_cap(0.2198, 0.2214, 0.2196, "做多", tick=0.0001)
    assert widened == 0.2190
    assert repr(widened) == "0.219"


def test_widen_no_tick_still_repr_clean():
    widened = widen_stop_for_tp1_rr_cap(100.0, 110.0, 99.0, "做多")
    assert widened == 95.0
    assert repr(widened) == "95.0"


def test_adjust_keeps_structural_stop_within_cap():
    decision = {
        "order_type": "限价单",
        "order_direction": "做空",
        "entry_price": 0.2198,
        "take_profit_price": 0.2182,
        "stop_loss_price": 0.2206,
    }
    assert not adjust_decision_stop_for_tp1_rr_cap(decision, tick=0.0001)
    assert decision["stop_loss_price"] == 0.2206


def test_format_estimated_win_rate_from_model_field():
    decision = {
        "estimated_win_rate": 47,
        "estimated_win_rate_reasoning": "宽通道顺势，方程用 47%",
    }
    assert format_estimated_win_rate(decision) == "47%"
    assert "47" in format_estimated_win_rate_reasoning(decision)
