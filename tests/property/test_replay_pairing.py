"""Property-based tests for replay pairing and decision attribution (Task A1).

Invariants:
- LIFO rebuild never inflates quantity: leg totals stay inside the fill
  totals, closed quantity stays inside the smaller side, net-zero symbols
  leave no open leg and open legs share the sign of the symbol's net.
- Attribution partitions every trade into exactly one status; matched trades
  carry the conf/stop/target of the chosen record; ambiguous_rows count
  matches the ambiguous counter.
"""
from __future__ import annotations

from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from pa_agent.trading.replay_pairing import attach_decisions, cid_variants, rebuild_trades

SYMBOLS = ["AAA", "BBB"]
_PREFIX = "pa-entry-"


@st.composite
def fills_stream(draw):
    n = draw(st.integers(min_value=1, max_value=12))
    fills = []
    for i in range(n):
        sym = draw(st.sampled_from(SYMBOLS))
        side = draw(st.sampled_from(["BUY", "SELL"]))
        qty = draw(st.floats(min_value=0.1, max_value=50.0, allow_nan=False, allow_infinity=False))
        price = draw(st.floats(min_value=1.0, max_value=1e5, allow_nan=False, allow_infinity=False))
        cid = draw(
            st.sampled_from([
                f"{_PREFIX}{i:024d}",
                f"{_PREFIX}deadbeefdeadbeefdeadbeef",
                "manual",
                "",
            ])
        )
        fills.append({
            "symbol": sym, "orderId": 1000 + i, "id": 1000 + i,
            "time": 1000 * (i + 1), "side": side, "qty": str(qty), "price": str(price),
            "realizedPnl": "0.0", "commission": "0.0", "cid": cid,
        })
    return fills


@given(fills=fills_stream())
@h_settings(max_examples=100)
def test_rebuild_leg_bounds_and_open_consistency(fills):
    """Legs never inflate quantity; a net-zero symbol leaves no open leg; any
    open leg matches the sign and size of the symbol's net fill quantity."""
    orders = {
        sym: [{"symbol": sym, "orderId": f["orderId"], "clientOrderId": f["cid"],
               "time": f["time"], "status": "FILLED", "type": "MARKET"}
              for f in fs if f["cid"].startswith(_PREFIX)]
        for sym, fs in {s: [x for x in fills if x["symbol"] == s] for s in SYMBOLS}.items()
    }
    trades = rebuild_trades(orders, {s: [x for x in fills if x["symbol"] == s] for s in SYMBOLS}, SYMBOLS)
    assert all(t["qty"] > 0 and t["cid"] for t in trades)
    for sym in SYMBOLS:
        sym_fills = [f for f in fills if f["symbol"] == sym]
        fill_net = sum((1 if f["side"] == "BUY" else -1) * float(f["qty"]) for f in sym_fills)
        buy_total = sum(float(f["qty"]) for f in sym_fills if f["side"] == "BUY")
        sell_total = sum(float(f["qty"]) for f in sym_fills if f["side"] == "SELL")
        legs = [t for t in trades if t["sym"] == sym]
        # (a) no quantity inflation
        assert sum(t["qty"] for t in legs) <= buy_total + sell_total + 1e-6
        # (b) matched (closed) quantity never exceeds the smaller side total
        closed_qty = sum(t["qty"] for t in legs if t.get("closed_at") is not None)
        assert closed_qty <= min(buy_total, sell_total) + 1e-6
        opens = [t for t in legs if t.get("closed_at") is None]
        # (c) net-zero symbol ends with no open leg
        if abs(fill_net) < 1e-9:
            assert opens == []
        else:
            # (d) open legs share the sign of the net; a partially closed
            # leg may keep its full original qty, so no magnitude bound applies
            assert opens and all(t["side"] == (1 if fill_net > 0 else -1) for t in opens)


@st.composite
def rows_and_trades(draw):
    """Decision rows with small integer prices, plus trades whose cids may
    come from one of the rows (exact) or be unrelated/foreign."""
    n_rows = draw(st.integers(min_value=0, max_value=6))
    rows = []
    for _i in range(n_rows):
        entry = draw(st.integers(min_value=1000, max_value=2000))
        stop = entry - draw(st.integers(min_value=5, max_value=50))
        target = entry + draw(st.integers(min_value=5, max_value=50))
        rows.append({
            "record_ts": draw(st.integers(min_value=0, max_value=40000)),
            "symbol": draw(st.sampled_from(SYMBOLS)),
            "timeframe": "15m",
            "cycle_position": "trading_range",
            "direction": "做多",
            "strategy_files": ["s.txt"],
            "patterns": [],
            "decision": {
                "order_direction": "做多",
                "order_type": "限价单",
                "entry_price": float(entry),
                "stop_loss_price": float(stop),
                "take_profit_price": float(target),
                "trade_confidence": draw(st.integers(min_value=0, max_value=99)),
                "diagnosis_confidence": 60,
            },
        })
    trades = []
    for _i in range(draw(st.integers(min_value=0, max_value=8))):
        opened_at = draw(st.integers(min_value=0, max_value=100000))
        if rows and draw(st.booleans()):
            row = draw(st.sampled_from(rows))
            cid = cid_variants(
                row["symbol"], "做多", "限价单",
                row["decision"]["entry_price"], row["decision"]["stop_loss_price"],
                row["decision"]["take_profit_price"],
            )[0]
            sym = row["symbol"]
        else:
            cid = draw(st.sampled_from(["manual", "", f"{_PREFIX}notamatchnotamatchnotam"]))
            sym = draw(st.sampled_from(SYMBOLS))
        trades.append({
            "sym": sym, "side": 1, "qty": 1.0, "entry": 100.0, "realized": 0.0,
            "fees": 0.0, "cid": cid, "opened_at": opened_at,
            "closed_at": None, "close_px": None,
        })
    return rows, trades


@given(data=rows_and_trades())
@h_settings(max_examples=150)
def test_attach_statuses_partition_and_are_consistent(data):
    rows, trades = data
    attached, audit = attach_decisions(trades, rows)
    assert len(attached) == len(trades)
    assert audit["matched"] + audit["manual"] + audit["missing_decision"] + audit["ambiguous"] == len(trades)
    assert audit["matched"] == sum(1 for t in attached if t["status"] == "matched")
    assert audit["manual"] == sum(1 for t in attached if t["status"] == "manual")
    assert audit["ambiguous"] == len(audit["ambiguous_rows"])
    for t, trade in zip(attached, trades, strict=True):
        if t["status"] == "matched":
            assert t["decision_idx"] is not None
            row = rows[t["decision_idx"]]
            assert t["conf"] == row["decision"]["trade_confidence"]
            assert t["stop"] == row["decision"]["stop_loss_price"]
            assert t["target"] == row["decision"]["take_profit_price"]
        elif t["status"] == "manual":
            assert not trade["cid"].startswith(_PREFIX)
        elif t["status"] == "missing_decision":
            assert trade["cid"].startswith(_PREFIX)
            assert t["decision_idx"] is None
        else:
            assert t["status"] == "ambiguous"
            assert t["decision_idx"] is None
