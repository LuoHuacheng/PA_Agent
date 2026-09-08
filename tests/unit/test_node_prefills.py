"""Unit tests for §6.3 boundary / §9.0 signal-quality prefills (Task B2)."""
from __future__ import annotations

from pa_agent.ai.kline_features import KlineGeometryFeature
from pa_agent.ai.market_features import SimpleMarketFeatures
from pa_agent.ai.node_prefills import (
    prefill_boundary,
    prefill_signal_quality,
    rank_quality,
    render_node_prefills_block,
)


def _mf(position=None, dl=None, dh=None) -> SimpleMarketFeatures:
    return SimpleMarketFeatures(
        lookback_bars=40, range_high=110.0, range_low=100.0,
        range_width_atr=4.0, price_position=position, zone="middle_third",
        dist_to_high_atr=dh, dist_to_low_atr=dl, overlap_mean_10=0.4,
        doji_inside_ratio_10=0.1, barbwire_score=0.1, barbwire_candidate=False,
        swing_structure="mixed", swings=(), pullback_depth_atr=None,
        pullback_bars=None, breakout_events=(), hl_count=None,
        supports=(), resistances=(), invalidation_long=None,
        invalidation_short=None, measured_moves=(), breakout_quality="none",
        breakout_attempt_type=None, mm_as_tp_ok=False,
        spike_aftermath_hint="none", scale_conflict=False,
    )


def _geo(**over) -> KlineGeometryFeature:
    base = dict(seq=1, bar_type="trend_bull", body_ratio=0.7,
                upper_wick_ratio=0.1, lower_wick_ratio=0.1,
                close_position=0.8, range_atr_ratio=1.2, ema_relation="above",
                overlap_prev_ratio=0.1, inside_sequence="none", ioi_pattern=False,
                micro_double="none", gap_bar="none", ema_gap_count=0,
                breakout_prev="none", follow_through_1_2="none")
    base.update(over)
    return KlineGeometryFeature(**base)


def test_boundary_near_lower_edge():
    r = prefill_boundary(_mf(position=0.17, dl=0.9, dh=4.0))
    assert r is not None and r[0] == "是" and r[1] == "lower"


def test_boundary_near_upper_edge():
    r = prefill_boundary(_mf(position=0.82, dl=5.0, dh=0.4))
    assert r is not None and r[0] == "是" and r[1] == "upper"


def test_boundary_middle_far_from_edges():
    r = prefill_boundary(_mf(position=0.5, dl=3.0, dh=3.0))
    assert r is not None and r[0] == "否" and r[1] == "middle"


def test_boundary_none_without_position():
    assert prefill_boundary(_mf(position=None)) is None


def test_quality_strong_medium_weak_and_invalid():
    assert prefill_signal_quality(_geo())[0] == "strong"
    assert prefill_signal_quality(
        _geo(range_atr_ratio=0.7, close_position=0.65, body_ratio=0.55)
    )[0] == "medium"
    assert prefill_signal_quality(_geo(bar_type="doji", body_ratio=0.1,
                                       range_atr_ratio=0.4))[0] == "invalid"
    assert prefill_signal_quality(_geo(bar_type="doji", body_ratio=0.2,
                                       range_atr_ratio=0.7))[0] == "weak"
    assert prefill_signal_quality(
        _geo(bar_type="flat", range_atr_ratio=0.1, body_ratio=0.05)
    )[0] == "invalid"
    assert prefill_signal_quality(
        _geo(bar_type="trend_bull", range_atr_ratio=0.2, body_ratio=0.1)
    )[0] == "invalid"


def test_rank_order_and_render():
    assert rank_quality("invalid") < rank_quality("weak") < rank_quality("medium") < rank_quality("strong")
    assert rank_quality("bogus") == 0
    r = prefill_boundary(_mf(position=0.17, dl=0.9, dh=4.0))
    text = render_node_prefills_block(boundary=(r[0], r[1], r[2]), quality=("weak", "依据说明"))
    assert "§6.3" in text and "§9.0" in text and "weak" in text
    assert render_node_prefills_block(boundary=None, quality=None) == ""
