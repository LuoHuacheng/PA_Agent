"""方向闸门 G1/G2 与结构否定离场: 只用价格几何, 不解析 LLM 文本."""

from __future__ import annotations

import pytest

from tools.shadow_replay import (
    EntryGates,
    Round,
    RoundIndex,
    apply_entry_gates,
    direction_gate_reasons,
    infer_tick_from_bars,
    simulate_plan,
)
from tools.shadow_replay import Plan as ReplayPlan


def make_plan(
    *,
    symbol="BTCUSDT",
    timeframe="30m",
    direction="做多",
    entry=100.0,
    stop=99.0,
    target=103.0,
    record_ms=0,
    bars=None,
):
    return ReplayPlan(
        path="p.json",
        symbol=symbol,
        timeframe=timeframe,
        record_ms=record_ms,
        order_type="限价单",
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        target2=None,
        cycle_position="broad_channel",
        diag_direction="bullish",
        trade_conf=60.0,
        diag_conf=60.0,
        est_win_rate=50.0,
        bars=bars if bars is not None else [(0, 100.0, 100.5, 99.5, 100.0)],
    )


def test_round_index_previous_takes_last_round_before():
    index = RoundIndex(
        {
            ("BTCUSDT", "30m"): [
                Round(100, "bullish", 1.0),
                Round(200, "bearish", 2.0),
                Round(300, "bullish", 3.0),
            ]
        }
    )
    key = ("BTCUSDT", "30m")
    assert index.previous(key, 250) == "bearish"
    assert index.previous(key, 100) is None
    assert index.previous(key, 301) == "bullish"
    assert index.previous(("ETHUSDT", "30m"), 250) is None


def test_round_index_in_bar_picks_last_round_inside():
    # 故意乱序传入: RoundIndex 自己排序, 不依赖调用方
    index = RoundIndex(
        {
            ("BTCUSDT", "30m"): [
                Round(0, "bullish", 1.0),
                Round(2_000_000, "bullish", 3.0),
                Round(1_000_000, "bearish", 2.0),
            ]
        }
    )
    key = ("BTCUSDT", "30m")
    # 落在 (bar 起点, bar 终点] 内的最后一轮
    assert index.in_bar(key, 500_000, 1_800_000) == "bullish"
    assert index.in_bar(key, 0, 900_000) is None  # 只有 0 那一轮, 不算之内
    assert index.in_bar(key, 2_200_000, 1_800_000) is None  # 之后没有轮次


def test_infer_tick_from_bars_uses_finest_precision():
    bars = [(0, 100.0, 100.5, 99.5, 100.12), (1, 100.12, 100.6, 99.9, 100.34)]
    assert infer_tick_from_bars(bars) == pytest.approx(0.01)


def test_infer_tick_defaults_to_one_for_integers():
    assert infer_tick_from_bars([(0, 100.0, 101.0, 99.0, 100.0)]) == pytest.approx(1.0)


def test_g1_blocks_entry_hugging_last_close():
    plan = make_plan(entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 100.0)])
    assert direction_gate_reasons(plan, tick=0.01, previous_direction=None) == ("G1_close_touch",)


def test_g1_allows_entry_far_from_close():
    plan = make_plan(entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 99.0)])
    assert direction_gate_reasons(plan, tick=0.01, previous_direction=None) == ()


def test_g2_blocks_entry_against_previous_round():
    plan = make_plan(direction="做多", entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 99.0)])
    assert direction_gate_reasons(plan, tick=0.01, previous_direction="bearish") == ("G2_flip",)
    short = make_plan(direction="做空", entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 99.0)])
    assert direction_gate_reasons(short, tick=0.01, previous_direction="bullish") == ("G2_flip",)


def test_g2_allows_entry_aligned_with_previous_round():
    plan = make_plan(direction="做多", entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 99.0)])
    assert direction_gate_reasons(plan, tick=0.01, previous_direction="bullish") == ()
    assert direction_gate_reasons(plan, tick=0.01, previous_direction="neutral") == ()


def test_g1_and_g2_can_fire_together():
    plan = make_plan(direction="做多", entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 100.0)])
    assert direction_gate_reasons(plan, tick=0.01, previous_direction="bearish") == (
        "G1_close_touch",
        "G2_flip",
    )


def test_apply_entry_gates_skips_direction_gates_without_rounds():
    gates = EntryGates(symbols=frozenset())
    plan = make_plan(entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 100.0)])
    admitted, counts = apply_entry_gates([plan], gates, direction_gates=True)
    assert len(admitted) == 1
    assert not counts


def test_apply_entry_gates_counts_direction_gate_reasons():
    gates = EntryGates(symbols=frozenset())
    plan = make_plan(entry=100.0, bars=[(0, 99.0, 101.0, 98.0, 100.0)])
    index = RoundIndex({("BTCUSDT", "30m"): [Round(-1, "bearish", 99.0)]})
    admitted, counts = apply_entry_gates([plan], gates, rounds=index, direction_gates=True)
    assert admitted == []
    assert counts["G1_close_touch+G2_flip"] == 1


TIMELINE = [
    (1_800_000, 101.0, 101.5, 99.9, 100.5),
    (3_600_000, 100.2, 100.4, 99.5, 99.9),
    (5_400_000, 99.8, 100.0, 99.2, 99.6),
    (7_200_000, 99.5, 99.9, 99.1, 99.3),
]

#: 收盘一直站在入场价之上的走势, 结构否定不该触发(条件要求已破入场).
TIMELINE_ABOVE = [
    (1_800_000, 101.0, 101.5, 99.9, 100.5),
    (3_600_000, 100.6, 100.9, 100.2, 100.7),
    (5_400_000, 100.6, 100.9, 100.3, 100.8),
    (7_200_000, 100.7, 101.0, 100.4, 100.9),
]


def _sim(plan, structure_exit=None, timeline=None):
    return simulate_plan(
        plan,
        TIMELINE if timeline is None else timeline,
        risk_usdt=4.0,
        fee_rate=0.0,
        leverage=20,
        margin_usdt=250.0,
        partial_pct=50.0,
        timeout_min=60.0,
        time_stop_min=720.0,
        structure_exit=structure_exit,
    )


def _bearish_rounds():
    # 轮次要落在出场循环真正遍历的 K 上: 成交当根之后的第 1、2 根
    return RoundIndex(
        {
            ("BTCUSDT", "30m"): [
                Round(4_000_000, "bearish", 99.9),
                Round(6_000_000, "bearish", 99.6),
            ]
        }
    )


def test_without_structure_exit_trade_runs_to_time_stop():
    out = _sim(make_plan(target=110.0))
    assert out.filled
    assert out.reason == "time_stop"


def test_structure_exit_closes_after_two_opposite_rounds():
    out = _sim(make_plan(target=110.0), {"index": _bearish_rounds(), "confirm_bars": 2})
    assert out.filled
    assert out.reason == "structure_exit"
    assert out.net == pytest.approx(-1.6)


def test_structure_exit_needs_confirmation_bars():
    out = _sim(make_plan(target=110.0), {"index": _bearish_rounds(), "confirm_bars": 3})
    assert out.reason == "time_stop"


def test_structure_exit_ignores_aligned_diagnosis():
    index = RoundIndex(
        {
            ("BTCUSDT", "30m"): [
                Round(4_000_000, "bullish", 100.4),
                Round(6_000_000, "bullish", 100.2),
            ]
        }
    )
    out = _sim(make_plan(target=110.0), {"index": index, "confirm_bars": 2})
    assert out.reason == "time_stop"


def test_structure_exit_skipped_when_close_above_entry():
    out = _sim(
        make_plan(target=110.0),
        {"index": _bearish_rounds(), "confirm_bars": 2},
        timeline=TIMELINE_ABOVE,
    )
    assert out.reason == "time_stop"
