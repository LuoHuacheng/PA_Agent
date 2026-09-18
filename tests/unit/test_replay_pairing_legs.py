"""Tests for leg aggregation in the P&L replay pairing."""
from __future__ import annotations

from pa_agent.trading.replay_pairing import aggregate_legs, partial_exit_comparison


def _leg(side: str, qty: float, entry: float, pnl: float, fees: float, opened: int, closed: int | None, px: float, cid: str = 'manual') -> dict:
    return {
        'sym': 'ADAUSDT',
        'side': side,
        'qty': qty,
        'entry': entry,
        'realized': pnl,
        'fees': fees,
        'cid': cid,
        'opened_at': opened,
        'closed_at': closed,
        'close_px': px,
        'conf': None,
        'stop': None,
        'target': None,
    }


def test_partial_closes_of_one_position_merge() -> None:
    """Same symbol/side, entries and exits within the window -> one trade."""
    legs = [
        _leg('BUY', 100.0, 0.2133, -1.00, -0.30, 1_000, 600_000, 0.2114),
        _leg('BUY', 200.0, 0.2139, -5.48, -0.60, 1_030, 600_000, 0.2114),
    ]
    merged = aggregate_legs(legs)
    assert len(merged) == 1
    row = merged[0]
    assert row['legs'] == 2
    assert row['qty'] == 300.0
    assert abs(row['entry'] - (100 * 0.2133 + 200 * 0.2139) / 300) < 1e-9
    assert abs(row['realized'] - (-6.48)) < 1e-9
    assert abs(row['fees'] - (-0.90)) < 1e-9
    assert row['opened_at'] == 1_000
    assert row['closed_at'] == 600_000


def test_opposite_sides_never_merge() -> None:
    legs = [
        _leg('BUY', 100.0, 0.2133, -1.0, -0.3, 1_000, 600_000, 0.2114),
        _leg('SELL', 100.0, 0.2133, -1.0, -0.3, 1_010, 600_000, 0.2114),
    ]
    assert len(aggregate_legs(legs)) == 2


def test_distant_trades_never_merge() -> None:
    legs = [
        _leg('BUY', 1.0, 1.0, 1.0, -0.1, 1_000, 100_000, 1.1),
        _leg('BUY', 1.0, 1.0, 1.0, -0.1, 9_000_000, 9_600_000, 1.1),
    ]
    assert len(aggregate_legs(legs)) == 2


def test_partial_exit_comparison_measures_the_partial() -> None:
    """Partial banked 104 then the rest exited 102: holding would have been worse."""
    legs = [
        _leg('BUY', 1.5, 100.0, 6.00, -0.30, 0, 60_000, 104.0),
        _leg('BUY', 1.5, 100.0, 3.00, -0.31, 0, 120_000, 102.0),
    ]
    rows = partial_exit_comparison(legs)
    assert len(rows) == 1
    row = rows[0]
    assert row['partial'] is True
    assert row['legs'] == 2
    assert row['partial_px'] == 104.0
    assert row['final_px'] == 102.0
    assert row['helped'] is True  # final exit worse than the partial exit
    assert abs(row['actual'] - (9.0 - 0.61)) < 1e-9
    assert row['single_exit'] < row['actual']
    assert abs(row['delta'] - (row['single_exit'] - row['actual'])) < 1e-9


def test_partial_exit_comparison_flags_missed_upside() -> None:
    """Partial at 102, final at 108: banking early gave up upside (helped=False)."""
    legs = [
        _leg('BUY', 1.0, 100.0, 2.00, -0.20, 0, 60_000, 102.0),
        _leg('BUY', 1.0, 100.0, 8.00, -0.21, 0, 120_000, 108.0),
    ]
    row = partial_exit_comparison(legs)[0]
    assert row['helped'] is False
    assert row['single_exit'] > row['actual']


def test_partial_exit_comparison_marks_positions_without_partials() -> None:
    legs = [_leg('BUY', 1.0, 100.0, 2.0, -0.2, 0, 60_000, 102.0)]
    row = partial_exit_comparison(legs)[0]
    assert row['partial'] is False
    assert 'single_exit' not in row


def test_single_leg_is_passed_through_unmarked() -> None:
    legs = [_leg('BUY', 1.0, 1.0, 1.0, -0.1, 1_000, 100_000, 1.1)]
    out = aggregate_legs(legs)
    assert len(out) == 1
    assert 'legs' not in out[0]