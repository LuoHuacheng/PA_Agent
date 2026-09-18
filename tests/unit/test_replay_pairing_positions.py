"""Tests for position-based fill pairing and partial-exit comparison."""
from __future__ import annotations

from pa_agent.trading.replay_pairing import (
    position_partial_comparison,
    rebuild_positions,
)

SYM = 'ADAUSDT'


def _fill(fid, oid, side, qty, px, ts, rpnl=0.0, comm=0.0):
    return {
        'id': fid,
        'orderId': oid,
        'side': side,
        'qty': str(qty),
        'price': str(px),
        'time': ts,
        'realizedPnl': str(rpnl),
        'commission': str(comm),
    }


def _ok(oid, cid):
    return {'orderId': oid, 'clientOrderId': cid}


def test_partial_closes_become_one_position_with_exits() -> None:
    orders = {SYM: [_ok(1, 'pa-entry-aaa')]}
    fills = {
        SYM: [
            _fill(1, 1, 'BUY', 2, 100, 1_000, 0.0, 0.10),
            _fill(2, 2, 'SELL', 1, 104, 2_000, 4.0, 0.05),
            _fill(3, 2, 'SELL', 1, 102, 3_000, 2.0, 0.05),
        ]
    }
    out = rebuild_positions(orders, fills, [SYM])
    assert len(out) == 1
    row = out[0]
    assert row['qty'] == 2
    assert row['entry'] == 100
    assert row['side'] == 1
    assert row['cid'] == 'pa-entry-aaa'
    assert len(row['exits']) == 2
    assert row['close_px'] == 102
    assert row['closed_at'] == 3_000
    assert abs(row['realized'] - 6.0) < 1e-9
    assert abs(row['fees'] + 0.20) < 1e-9


def test_exit_rows_carry_the_closing_order_id() -> None:
    """The closing clientOrderId is what tells stops from active closes apart."""
    orders = {SYM: [_ok(1, 'pa-entry-aaa'), _ok(2, 'pa-sl-bbb'), _ok(3, 'x-cvGenerated')]}
    fills = {
        SYM: [
            _fill(1, 1, 'BUY', 2, 100, 1_000, 0.0, 0.0),
            _fill(2, 2, 'SELL', 1, 98, 2_000, -2.0, 0.0),
            _fill(3, 3, 'SELL', 1, 97, 3_000, -3.0, 0.0),
        ]
    }
    exits = rebuild_positions(orders, fills, [SYM])[0]['exits']
    assert [e['cid'] for e in exits] == ['pa-sl-bbb', 'x-cvGenerated']


def test_exit_rows_leave_the_cid_empty_when_the_order_is_unavailable() -> None:
    # a stop order the exchange no longer lists in order history
    orders = {SYM: [_ok(1, 'pa-entry-aaa')]}
    fills = {SYM: [_fill(1, 1, 'BUY', 1, 100, 1_000), _fill(2, 99, 'SELL', 1, 98, 2_000)]}
    exits = rebuild_positions(orders, fills, [SYM])[0]['exits']
    assert exits[0]['cid'] == ''


def test_same_side_add_on_sizes_into_the_entry() -> None:
    orders = {SYM: [_ok(1, 'pa-entry-bbb')]}
    fills = {
        SYM: [
            _fill(1, 1, 'BUY', 1, 100, 1_000, 0.0, 0.05),
            _fill(2, 1, 'BUY', 1, 102, 1_100, 0.0, 0.05),
            _fill(3, 2, 'SELL', 2, 105, 2_000, 8.0, 0.10),
        ]
    }
    out = rebuild_positions(orders, fills, [SYM])
    assert len(out) == 1
    assert out[0]['qty'] == 2
    assert abs(out[0]['entry'] - 101.0) < 1e-9
    assert len(out[0]['exits']) == 1


def test_flip_closes_then_opens_the_opposite_side() -> None:
    orders = {SYM: [_ok(1, 'pa-entry-ccc')]}
    fills = {
        SYM: [
            _fill(1, 1, 'BUY', 1, 100, 1_000, 0.0, 0.05),
            _fill(2, 2, 'SELL', 2, 110, 2_000, 10.0, 0.10),
        ]
    }
    out = rebuild_positions(orders, fills, [SYM])
    assert len(out) == 2
    closed, opened = out[0], out[1]
    assert closed['side'] == 1 and closed['close_px'] == 110 and closed['closed_at'] == 2_000
    assert opened['side'] == -1 and opened['qty'] == 1 and opened['entry'] == 110
    assert opened['closed_at'] is None


def test_every_fill_is_attributed_exactly_once() -> None:
    orders = {SYM: [_ok(1, 'pa-entry-ddd'), _ok(2, 'pa-tp-1')]}
    fills = {
        SYM: [
            _fill(1, 1, 'BUY', 3, 50, 1_000, 0.0, 0.15),
            _fill(2, 2, 'SELL', 1, 52, 2_000, 2.0, 0.05),
            _fill(3, 2, 'SELL', 1, 48, 3_000, -2.0, 0.05),
            _fill(4, 2, 'SELL', 1, 55, 4_000, 5.0, 0.05),
        ]
    }
    out = rebuild_positions(orders, fills, [SYM])
    assert len(out) == 1
    assert abs(sum(r['realized'] for r in out) - 5.0) < 1e-9
    assert abs(sum(r['fees'] for r in out) + 0.30) < 1e-9


def test_position_partial_comparison_uses_realised_pnl() -> None:
    pos = {
        'sym': SYM,
        'side': 1,
        'qty': 2.0,
        'entry': 100.0,
        'realized': 6.0,
        'fees': -0.20,
        'exits': [
            {'time': 2_000, 'px': 104.0, 'qty': 1.0, 'realized': 4.0, 'fees': -0.05},
            {'time': 3_000, 'px': 102.0, 'qty': 1.0, 'realized': 2.0, 'fees': -0.05},
        ],
    }
    row = position_partial_comparison([pos])[0]
    assert row['partial'] is True
    assert row['legs'] == 2
    assert row['partial_px'] == 104.0 and row['final_px'] == 102.0
    assert row['helped'] is True          # final exit worse than the partial exit
    assert abs(row['single_exit'] - 3.8) < 1e-9   # (102-100)*2 - 0.20
    assert abs(row['delta'] - (-2.0)) < 1e-9      # gross 4 - realised 6


def test_position_partial_comparison_marks_single_exit() -> None:
    pos = {
        'sym': SYM,
        'side': -1,
        'qty': 1.0,
        'entry': 100.0,
        'realized': 5.0,
        'fees': -0.1,
        'exits': [{'time': 2_000, 'px': 95.0, 'qty': 1.0, 'realized': 5.0, 'fees': -0.1}],
    }
    row = position_partial_comparison([pos])[0]
    assert row['partial'] is False
    assert 'single_exit' not in row