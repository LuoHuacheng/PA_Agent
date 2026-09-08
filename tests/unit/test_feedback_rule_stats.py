"""Unit tests for rule-grouped outcome statistics (Task A3).

A row that routed multiple strategy files counts once per file group
(multi-file rows appear in every file group they used); open rows only feed
open_n; win_rate/avg metrics require min_samples closed rows.
"""
from __future__ import annotations

import pytest

from pa_agent.feedback.rule_stats import build_group_stats


def _row(
    *,
    files=("震荡区间交易策略.txt",),
    cycle: str = "trading_range",
    direction: str = "neutral",
    conf: int | None = 60,
    outcome: str = "win",
    win_r: float | None = 1.0,
    ts_open: int = 1000,
) -> dict:
    return {
        "symbol": "BTCUSDT",
        "strategy_files": files,
        "cycle_position": cycle,
        "diag_direction": direction,
        "conf": conf,
        "outcome": outcome,
        "win_r": win_r,
        "ts_open": ts_open,
    }


def test_strategy_file_group_counts_multi_file_rows_per_file():
    rows = [
        _row(files=("a.txt", "b.txt")),          # win for both files
        _row(files=("a.txt",), outcome="loss", win_r=-2.0),
        _row(files=("b.txt",), outcome="open"),  # open: not in the closed set
    ]
    groups = build_group_stats(rows, group_by=("strategy_file",), min_samples=1)
    ga, gb = groups["a.txt"], groups["b.txt"]
    assert ga["n"] == 2 and ga["open_n"] == 0
    assert ga["win_rate"] == pytest.approx(0.5)
    assert gb["n"] == 1 and gb["open_n"] == 1
    assert gb["win_rate"] == pytest.approx(1.0)
    # open row was excluded from b's closed set
    assert gb["avg_confidence"] == pytest.approx(60.0)


def test_cycle_direction_grouping_and_expectancy():
    rows = [
        _row(cycle="trading_range", direction="bullish", win_r=1.0),
        _row(cycle="trading_range", direction="bullish", outcome="loss", win_r=-1.0,
             conf=None),
        _row(cycle="normal_channel", direction="bearish", outcome="loss", win_r=-0.5),
    ]
    groups = build_group_stats(rows, group_by=("cycle_position", "diag_direction"),
                               min_samples=1)
    tr = groups["trading_range|bullish"]
    assert tr["n"] == 2
    assert tr["win_rate"] == pytest.approx(0.5)
    assert tr["expectancy_r"] == pytest.approx(0.0)  # +1R and -1R equally likely
    assert tr["avg_confidence"] == pytest.approx(60.0)  # mean over available confs
    assert tr["open_n"] == 0
    nc = groups["normal_channel|bearish"]
    assert nc["win_rate"] == pytest.approx(0.0)


def test_min_samples_gates_all_metrics():
    rows = [_row() for _ in range(9)]  # 9 closed rows < min_samples=10
    groups = build_group_stats(rows, group_by=("strategy_file",), min_samples=10)
    g = groups["震荡区间交易策略.txt"]
    assert g["n"] == 9
    assert g["win_rate"] is None
    assert g["avg_r"] is None
    assert g["expectancy_r"] is None


def test_avg_r_over_closed_rows_with_risk():
    rows = [
        _row(win_r=2.0),
        _row(outcome="loss", win_r=-1.0),
        _row(outcome="open", win_r=None),
    ]
    groups = build_group_stats(rows, group_by=("strategy_file",), min_samples=1)
    g = groups["震荡区间交易策略.txt"]
    assert g["avg_r"] == pytest.approx(0.5)
    assert g["n"] == 2 and g["open_n"] == 1


def test_empty_input_returns_no_groups():
    assert build_group_stats([], group_by=("strategy_file",), min_samples=10) == {}
