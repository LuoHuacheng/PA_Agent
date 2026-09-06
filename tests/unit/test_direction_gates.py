"""Unit tests for direction-quality gates (G1 close-touch, G2 flip)."""
from __future__ import annotations

from types import SimpleNamespace

from pa_agent.trading.direction_gates import evaluate_direction_gates


def _bar(close: float, high: float = 0.0, low: float = 0.0, open_: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(open=open_ or close, high=high or close, low=low or close, close=close)


def _long_limit(entry: float) -> dict:
    return {"order_type": "限价单", "order_direction": "做多", "entry_price": entry}


def _record(direction: str) -> SimpleNamespace:
    return SimpleNamespace(stage1_diagnosis={"direction": direction})


def test_ada_12h_case_g1_rejects_close_hug_entry() -> None:
    # ADA 12:00: entry 0.2229 sits 1 tick above the 11:45 close 0.2228.
    bars = [
        _bar(close=0.2228, high=0.2236, low=0.2228),
        _bar(close=0.2234, high=0.2234, low=0.2207),
    ]
    reasons = evaluate_direction_gates(
        decision=_long_limit(0.2229), bars=bars, tick=0.0001
    )
    assert any(r.startswith("G1") for r in reasons)


def test_link_18h_case_g1_rejects_close_hug_entry() -> None:
    # LINK 18:11: entry 12.268 sits 1 tick above the 18:00 close 12.267.
    bars = [_bar(close=12.267, high=12.302, low=12.267)]
    reasons = evaluate_direction_gates(
        decision=_long_limit(12.268), bars=bars, tick=0.001
    )
    assert any(r.startswith("G1") for r in reasons)


def test_deep_pullback_entry_passes_g1() -> None:
    # A true retest entry far below the close is exactly what G1 must keep.
    bars = [_bar(close=12.31, high=12.32, low=12.29)]
    reasons = evaluate_direction_gates(
        decision=_long_limit(12.255), bars=bars, tick=0.001
    )
    assert reasons == []


def test_g2_flip_rejects_opposite_previous_round() -> None:
    bars = [_bar(close=0.2228)]
    reasons = evaluate_direction_gates(
        decision=_long_limit(0.2215),
        bars=bars,
        tick=0.0001,
        previous_record=_record("bearish"),
    )
    assert any(r.startswith("G2") for r in reasons)


def test_g2_neutral_previous_round_not_blocked() -> None:
    bars = [_bar(close=0.2228)]
    reasons = evaluate_direction_gates(
        decision=_long_limit(0.2215),
        bars=bars,
        tick=0.0001,
        previous_record=_record("neutral"),
    )
    assert reasons == []


def test_market_order_not_gated() -> None:
    bars = [_bar(close=0.2228)]
    decision = {"order_type": "市价单", "order_direction": "做多", "entry_price": 0.2229}
    reasons = evaluate_direction_gates(decision=decision, bars=bars, tick=0.0001)
    assert reasons == []
