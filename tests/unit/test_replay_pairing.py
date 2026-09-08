"""Unit tests for replay pairing and decision attribution (Task A1).

Covers:
- LIFO trade rebuild from fills (same semantics as the legacy
  tools/_pa_sim_common.rebuild_trades, but pure/dict based).
- Numeric-form tolerant clientOrderId variants.
- Decision attribution: exact cid hit, nearest-<=-opened_at disambiguation
  inside the join window, ambiguity and manual/missing classification.
- classify_outcome: net==0 counts as loss (Q3 decision).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from pa_agent.trading.replay_pairing import (
    attach_decisions,
    cid_variants,
    classify_outcome,
    rebuild_trades,
)

SYM = "BTCUSDT"


def _fill(
    sym: str,
    oid: int,
    fid: int,
    t_ms: int,
    side: str,
    qty: float,
    price: float,
    rpnl: float = 0.0,
    comm: float = 0.0,
) -> dict:
    return {
        "symbol": sym,
        "orderId": oid,
        "id": fid,
        "time": t_ms,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "realizedPnl": str(rpnl),
        "commission": str(comm),
    }


def _order(sym: str, oid: int, cid: str, t_ms: int) -> dict:
    return {
        "symbol": sym,
        "orderId": oid,
        "clientOrderId": cid,
        "time": t_ms,
        "status": "FILLED",
        "type": "LIMIT",
    }


def _dec(
    ts_ms: int,
    *,
    sym: str = SYM,
    direction: str = "做多",
    otype: str = "限价单",
    entry=65000.0,
    stop=64976.0,
    target=65014.0,
    conf: int = 60,
    tf: str = "15m",
    cycle: str = "trading_range",
) -> dict:
    return {
        "record_ts": ts_ms,
        "symbol": sym,
        "timeframe": tf,
        "cycle_position": cycle,
        "direction": direction,
        "strategy_files": ["震荡区间交易策略.txt"],
        "patterns": ["ais", "failed_breakout"],
        "decision": {
            "order_direction": direction,
            "order_type": otype,
            "entry_price": entry,
            "stop_loss_price": stop,
            "take_profit_price": target,
            "trade_confidence": conf,
            "diagnosis_confidence": 65,
        },
    }


def _trade(
    opened_at: int,
    *,
    cid: str,
    side: int = 1,
    qty: float = 1.0,
    closed_at: int | None = 2000,
) -> dict:
    t = {
        "sym": SYM,
        "side": side,
        "qty": qty,
        "entry": 100.0,
        "realized": 0.0,
        "fees": 0.0,
        "cid": cid,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "close_px": None,
    }
    return t


# ---------------------------------------------------------------------------
# cid variants
# ---------------------------------------------------------------------------


def _expected_cid(entry, stop, target) -> str:
    material = {
        "symbol": SYM,
        "direction": "做多",
        "type": "限价单",
        "entry": entry,
        "stop": stop,
        "target": target,
    }
    h = hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return "pa-entry-" + h[:27]


def test_cid_variants_numeric_spellings_share_candidates():
    # Legacy _num_forms folds every whole-value spelling into the float and
    # its str() form, so int/float/"65000.0"/"65000" must yield the same set
    # of clientOrderId candidates (both sides of the join produce the same).
    base = cid_variants(SYM, "做多", "限价单", 65000.0, 64976.0, 65014.0)
    assert cid_variants(SYM, "做多", "限价单", 65000, 64976, 65014) == base
    assert cid_variants(SYM, "做多", "限价单", "65000.0", "64976.0", "65014.0") == base
    assert cid_variants(SYM, "做多", "限价单", "65000", "64976", "65014") == base
    assert len(set(base)) == len(base) == 8  # 2 forms per price field


def test_cid_variants_all_forms_are_prefixed():
    variants = cid_variants(SYM, "做空", "市价单", 100.5, 99.0, 103.0)
    assert variants and all(v.startswith("pa-entry-") for v in variants)
    assert len(set(variants)) == len(variants) == 8  # 2 forms per price field


# ---------------------------------------------------------------------------
# rebuild_trades (LIFO)
# ---------------------------------------------------------------------------


def test_rebuild_full_entry_and_close():
    fills = [
        _fill(SYM, 1, 1, 1000, "BUY", 1.0, 100.0, comm=-0.02),
        _fill(SYM, 2, 2, 2000, "SELL", 1.0, 110.0, rpnl=10.0, comm=-0.02),
    ]
    orders = [_order(SYM, 1, "pa-entry-abc", 1000), _order(SYM, 2, "", 2000)]
    trades = rebuild_trades(
        {SYM: orders}, {SYM: fills}, [SYM]
    )
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == 1
    assert t["qty"] == pytest.approx(1.0)
    assert t["entry"] == pytest.approx(100.0)
    assert t["realized"] == pytest.approx(10.0)
    assert t["fees"] == pytest.approx(-0.04)
    assert t["closed_at"] == 2000
    assert t["close_px"] == pytest.approx(110.0)
    assert t["cid"] == "pa-entry-abc"


def test_rebuild_partial_close_keeps_remainder_open_until_full_close():
    fills = [
        _fill(SYM, 1, 1, 1000, "BUY", 1.0, 100.0, comm=-0.02),
        _fill(SYM, 2, 2, 2000, "SELL", 0.4, 104.0, rpnl=4.0, comm=-0.01),
        _fill(SYM, 3, 3, 3000, "SELL", 0.6, 110.0, rpnl=6.0, comm=-0.01),
    ]
    orders = [_order(SYM, 1, "pa-entry-abc", 1000)]
    trades = rebuild_trades({SYM: orders}, {SYM: fills}, [SYM])
    # 0.4 close leaves the position open; only the second close completes it
    assert len(trades) == 1
    t = trades[0]
    assert t["closed_at"] == 3000
    assert t["qty"] == pytest.approx(1.0)
    assert t["realized"] == pytest.approx(10.0)
    assert t["fees"] == pytest.approx(-0.04)


def test_rebuild_merges_consecutive_same_cid_entry_fills():
    fills = [
        _fill(SYM, 1, 1, 1000, "BUY", 0.6, 100.0, comm=-0.02),
        _fill(SYM, 1, 2, 1200, "BUY", 0.4, 100.5, comm=-0.01),
        _fill(SYM, 2, 3, 2000, "SELL", 1.0, 110.0, rpnl=10.0, comm=-0.02),
    ]
    orders = [_order(SYM, 1, "pa-entry-abc", 1000)]
    trades = rebuild_trades({SYM: orders}, {SYM: fills}, [SYM])
    assert len(trades) == 1
    t = trades[0]
    assert t["qty"] == pytest.approx(1.0)
    assert t["entry"] == pytest.approx(100.2)  # weighted: (0.6*100 + 0.4*100.5)/1
    assert t["realized"] == pytest.approx(10.0)


def test_rebuild_records_closing_order_id():
    fills = [
        _fill(SYM, 1, 1, 1000, "BUY", 1.0, 100.0, comm=-0.02),
        _fill(SYM, 7, 2, 2000, "SELL", 1.0, 105.0, rpnl=5.0, comm=-0.02),
    ]
    orders = [_order(SYM, 1, "pa-entry-abc", 1000), _order(SYM, 7, "pa-sl-x", 2000)]
    trades = rebuild_trades({SYM: orders}, {SYM: fills}, [SYM])
    assert len(trades) == 1
    assert trades[0]["close_order_id"] == 7
    # open legs carry no closing order
    fills2 = [_fill(SYM, 1, 1, 1000, "BUY", 1.0, 100.0)]
    trades2 = rebuild_trades({SYM: orders[:1]}, {SYM: fills2}, [SYM])
    assert trades2[0]["close_order_id"] is None


def test_rebuild_no_fills_returns_empty():
    trades = rebuild_trades({SYM: []}, {SYM: []}, [SYM])
    assert trades == []


# ---------------------------------------------------------------------------
# attach_decisions
# ---------------------------------------------------------------------------


def test_attach_exact_cid_single_match():
    row = _dec(1000)
    cid = cid_variants(SYM, row["decision"]["order_direction"], row["decision"]["order_type"],
                       row["decision"]["entry_price"], row["decision"]["stop_loss_price"],
                       row["decision"]["take_profit_price"])[0]
    trade = _trade(2500, cid=cid)
    attached, audit = attach_decisions([trade], [row])
    assert audit == {"matched": 1, "manual": 0, "missing_decision": 0, "ambiguous": 0,
                     "ambiguous_rows": []}
    assert attached[0]["status"] == "matched"
    assert attached[0]["decision_idx"] == 0
    assert attached[0]["conf"] == 60
    assert attached[0]["stop"] == pytest.approx(64976.0)
    assert attached[0]["target"] == pytest.approx(65014.0)


def test_attach_chooses_nearest_record_at_or_before_opened_at():
    rows = [_dec(1000), _dec(2000)]  # same material
    cid = cid_variants(SYM, "做多", "限价单", 65000.0, 64976.0, 65014.0)[0]
    trade = _trade(2500, cid=cid)
    attached, audit = attach_decisions([trade], rows)
    assert audit["matched"] == 1 and audit["ambiguous"] == 0
    assert attached[0]["decision_idx"] == 1  # later record within window wins


def test_attach_ambiguous_when_no_record_at_or_before_opened_at():
    rows = [_dec(1000), _dec(2000)]
    cid = cid_variants(SYM, "做多", "限价单", 65000.0, 64976.0, 65014.0)[0]
    trade = _trade(500, cid=cid)  # opened before both records
    attached, audit = attach_decisions([trade], rows)
    assert audit["matched"] == 0 and audit["ambiguous"] == 1
    assert attached[0]["status"] == "ambiguous"
    assert attached[0]["decision_idx"] is None
    assert audit["ambiguous_rows"][0]["candidates"] == 2


def test_attach_respects_join_hours_window():
    row = _dec(1000)
    cid = cid_variants(SYM, "做多", "限价单", 65000.0, 64976.0, 65014.0)[0]
    # opened_at more than join_hours after the record -> no eligible candidate
    trade = _trade(1000 + 2 * 3600 * 1000, cid=cid)
    attached, audit = attach_decisions([trade], [row], join_hours=1)
    assert attached[0]["status"] == "ambiguous"
    assert audit["ambiguous"] == 1
    # inside the window it matches
    trade2 = _trade(1000 + 30 * 60 * 1000, cid=cid)
    attached2, _audit2 = attach_decisions([trade2], [row], join_hours=1)
    assert attached2[0]["status"] == "matched"


def test_attach_manual_and_missing_decision():
    manual = _trade(1000, cid="manual")
    missing = _trade(1000, cid="pa-entry-000000000000000000000000000")
    attached, audit = attach_decisions([manual, missing], [_dec(1)])
    assert audit["manual"] == 1 and audit["missing_decision"] == 1
    by_cid = {t["cid"]: t for t in attached}
    assert by_cid["manual"]["status"] == "manual"
    assert by_cid[missing["cid"]]["status"] == "missing_decision"


def test_attach_empty_rows_no_crash():
    trade = _trade(1000, cid="manual")
    attached, audit = attach_decisions([trade], [])
    assert attached[0]["status"] == "manual"
    assert audit["matched"] == 0 and audit["manual"] == 1


def test_attach_csv_string_prices_match_float_execution_material():
    row = _dec(1000, entry="65000.0", stop="64976.0", target="65014.0")
    cid = cid_variants(SYM, "做多", "限价单", 65000.0, 64976.0, 65014.0)[0]
    trade = _trade(2500, cid=cid)
    attached, _audit = attach_decisions([trade], [row])
    assert attached[0]["status"] == "matched"


def test_classify_outcome_zero_counts_as_loss():
    assert classify_outcome(1.5) == "win"
    assert classify_outcome(-0.3) == "loss"
    assert classify_outcome(0.0) == "loss"
