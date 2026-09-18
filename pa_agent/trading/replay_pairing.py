"""Replay pairing and decision attribution core (pure, no IO).

Owned by Task A1 of the Phase-A feedback plan. Moves the LIFO trade-rebuild
semantics and the material-hash clientOrderId variants out of
tools/_pa_sim_common.py into a dependency-free module so that the feedback
pipeline (pa_agent/feedback/outcome_store.py) can attach decision context to
account fills without importing CLI tooling.

Semantics preserved from tools/_pa_sim_common.py:

- rebuild_trades: per-symbol LIFO pairing over userTrades; consecutive fills
  of the same entry clientOrderId merge into one trade leg; a trade is
  "closed" when its remaining quantity reaches zero.
- cid_variants: every numeric/string spelling of (entry, stop, target)
  produces the same set of clientOrderId candidates used by the executor
  (sha256 over the sorted material JSON, prefixed "pa-entry-").

Attribution contract (A2 review, docs/superpowers/plans/2026-09-08-A2-outcome-store-review.md §5.2):

1. Exact cid hit via numeric-form variants.
2. Multiple hits: choose the latest record with record_ts <= opened_at and
   opened_at - record_ts <= join_hours; otherwise the trade is ambiguous and
   is counted (never silently attached).
3. No hit: fills without the entry prefix are manual; entry-prefixed fills
   without any decision record are missing_decision. Both are counted and
   never fabricate a strategy outcome.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

ENTRY_CID_PREFIX = "pa-entry-"

#: Quantity residual tolerance used when deciding whether a leg is closed.
_EPS = 1e-12

#: Audit counters reported by attach_decisions (stable key order).
_AUDIT_KEYS = ("matched", "manual", "missing_decision", "ambiguous")


def _num_forms(v: Any) -> list[Any]:
    """Candidate numeric forms for the signal-id hash (legacy semantics)."""
    if v is None or str(v).strip() == "":
        return [None]
    f = float(str(v))
    s = {f}
    if f == int(f):
        s.add(int(f))
    s.add(str(f))
    return list(s)


def cid_variants(
    symbol: str, direction: Any, order_type: Any, entry: Any, stop: Any, target: Any
) -> list[str]:
    """clientOrderId candidates for a decision's entry material.

    Mirrors the executor's `_signal_id` + `_entry_client_id` derivation
    (pa_agent/trading/binance_usdm_testnet.py): the client id is
    "pa-entry-" + sha256(sort_keys material)[:27], truncated to 36 chars.
    """
    out: list[str] = []
    for e in _num_forms(entry):
        for s in _num_forms(stop):
            for t in _num_forms(target):
                if e is None or s is None or t is None:
                    continue
                material = {
                    "symbol": symbol,
                    "direction": direction,
                    "type": order_type,
                    "entry": e,
                    "stop": s,
                    "target": t,
                }
                h = hashlib.sha256(
                    json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                out.append(f"{ENTRY_CID_PREFIX}{h[:27]}")
    return out


def rebuild_trades(
    orders_by_symbol: dict[str, list[dict]],
    fills_by_symbol: dict[str, list[dict]],
    symbols: list[str],
) -> list[dict]:
    """LIFO pairing of account fills into per-signal trade legs.

    Inputs mirror the Binance payload shapes cached by fetch_account_data
    (orders.json / user_trades.json). Returns trade dicts with the same keys
    as the legacy `_pa_sim_common.rebuild_trades` rows (before decision
    attachment): sym/side/qty/entry/realized/fees/cid/opened_at/closed_at/
    close_px. Open legs are included with closed_at=None.
    """
    order_by_id = {
        (s, o["orderId"]): o
        for s, lst in orders_by_symbol.items()
        for o in lst
        if s in symbols
    }
    all_trades: list[dict] = []
    for sym in symbols:
        queue: list[dict] = []
        fills = sorted(fills_by_symbol.get(sym, []), key=lambda x: (x["time"], x["id"]))
        for fl in fills:
            qty = float(fl["qty"])
            delta = qty if fl["side"] == "BUY" else -qty
            px = float(fl["price"])
            rpnl = float(fl.get("realizedPnl") or 0)
            comm = -abs(float(fl.get("commission") or 0))
            o = order_by_id.get((sym, fl["orderId"]), {})
            cid = str(o.get("clientOrderId") or "")
            is_entry = cid.startswith(ENTRY_CID_PREFIX)
            rem = qty
            while rem > _EPS and queue and queue[-1]["side"] != (1 if delta > 0 else -1):
                e = queue[-1]
                take = min(rem, e["rem"])
                e["rem"] -= take
                e["realized"] += rpnl * (take / qty)
                e["fees"] += comm * (take / qty)
                e["close_px"] = px
                e["close_order_id"] = fl["orderId"]
                rem -= take
                if e["rem"] < _EPS:
                    e["closed_at"] = fl["time"]
                    all_trades.append(e)
                    queue.pop()
            if rem > _EPS:
                side = 1 if delta > 0 else -1
                last = queue[-1] if queue else None
                if last is not None and last["cid"] == cid and last["side"] == side:
                    total = last["orig"] + rem
                    last["avg"] = (last["avg"] * last["orig"] + px * rem) / total
                    last["orig"] = total
                    last["rem"] += rem
                    last["fees"] += comm * (rem / qty)
                else:
                    queue.append(
                        {
                            "sym": sym,
                            "side": side,
                            "orig": rem,
                            "rem": rem,
                            "avg": px,
                            "realized": 0.0,
                            "fees": comm * (rem / qty),
                            "cid": cid if is_entry else "manual",
                            "opened_at": fl["time"],
                            "closed_at": None,
                            "close_px": None,
                            "close_order_id": None,
                        }
                    )
        all_trades += queue
    out: list[dict] = []
    for t in all_trades:
        out.append(
            {
                "sym": t["sym"],
                "side": t["side"],
                "qty": t["orig"],
                "entry": t["avg"],
                "realized": t["realized"],
                "fees": t["fees"],
                "cid": t["cid"],
                "opened_at": t["opened_at"],
                "closed_at": t["closed_at"],
                "close_px": t["close_px"],
                "close_order_id": t.get("close_order_id"),
            }
        )
    return out


def rebuild_positions(
    orders_by_symbol: dict[str, list[dict]],
    fills_by_symbol: dict[str, list[dict]],
    symbols: list[str],
) -> list[dict]:
    """One record per position, with every exit attached.

    The LIFO leg pairing (`rebuild_trades`) splits a position into per-fill legs,
    which inflates trade counts and — worse — leaves fills it cannot pair out of
    the result entirely, so the rebuilt PnL does not reconcile with the account
    income ledger (measured: |diff| 152.7 USDT over 5 symbols).

    Attribution here is by *position*: same-direction fills extend the open
    position (quantity-weighted entry), opposite fills close it, and a flip closes
    the current position before opening a new one. Every fill's realised PnL and
    commission lands in exactly one record, so totals reconcile by construction.
    `exits` holds each reduction (time/px/qty/realized/fees) for partial-TP work.
    """
    order_by_id = {
        (s, o["orderId"]): o
        for s, lst in orders_by_symbol.items()
        for o in lst
        if s in symbols
    }
    out: list[dict] = []
    for sym in symbols:
        fills = sorted(fills_by_symbol.get(sym, []), key=lambda x: (x["time"], x["id"]))
        pos: dict | None = None
        for fl in fills:
            qty = float(fl["qty"])
            px = float(fl["price"])
            signed = qty if fl["side"] == "BUY" else -qty
            rpnl = float(fl.get("realizedPnl") or 0.0)
            comm = -abs(float(fl.get("commission") or 0.0))
            o = order_by_id.get((sym, fl["orderId"]), {})
            cid = str(o.get("clientOrderId") or "")
            if qty <= 0:
                continue
            if pos is None:
                pos = _open_position(sym, signed, qty, px, cid, int(fl["time"]), rpnl, comm)
                continue
            if (signed > 0) == (pos["side"] > 0):
                pos["qty"] += qty
                pos["size"] += qty
                pos["entry_notional"] += qty * px
                pos["realized"] += rpnl
                pos["fees"] += comm
                pos["fills"] += 1
                continue
            close_qty = min(qty, pos["qty"])
            share = close_qty / qty
            exit_row = {
                "time": int(fl["time"]),
                "px": px,
                "qty": close_qty,
                "realized": rpnl * share,
                "fees": comm * share,
                # The closing order's clientOrderId says *what* closed the
                # position: pa-sl/pa-tp are the protective orders, pa-rollback
                # is an active exit, and anything else was placed outside this
                # process. Empty when the order predates the exchange's
                # order-history window (fills still carry the orderId).
                "cid": cid,
            }
            pos["exits"].append(exit_row)
            pos["realized"] += exit_row["realized"]
            pos["fees"] += exit_row["fees"]
            pos["close_px"] = px
            pos["closed_at"] = int(fl["time"])
            pos["qty"] -= close_qty
            pos["fills"] += 1
            remainder = qty - close_qty
            if pos["qty"] <= _EPS:
                out.append(_finalize_position(pos, closed=True))
                pos = None
                if remainder > _EPS:
                    pos = _open_position(
                        sym,
                        remainder if signed > 0 else -remainder,
                        remainder,
                        px,
                        cid,
                        int(fl["time"]),
                        rpnl * (remainder / qty),
                        comm * (remainder / qty),
                    )
        if pos is not None:
            out.append(_finalize_position(pos, closed=False))
    out.sort(key=lambda t: t["opened_at"])
    return out


def _open_position(
    sym: str,
    signed_qty: float,
    qty: float,
    px: float,
    cid: str,
    ts: int,
    realized: float,
    fees: float,
) -> dict:
    return {
        "sym": sym,
        "side": 1 if signed_qty > 0 else -1,
        "qty": qty,
        "size": qty,
        "entry_notional": qty * px,
        "realized": realized,
        "fees": fees,
        "cid": cid if cid.startswith(ENTRY_CID_PREFIX) else "manual",
        "opened_at": ts,
        "closed_at": None,
        "close_px": None,
        "exits": [],
        "fills": 1,
    }


def _finalize_position(pos: dict, *, closed: bool) -> dict:
    # Report the size the position was opened with: a fully closed position has no
    # remaining quantity, but its entry/report size must survive.
    size = pos["size"]
    return {
        "sym": pos["sym"],
        "side": pos["side"],
        "qty": size,
        "entry": (pos["entry_notional"] / size) if size > 0 else 0.0,
        "realized": pos["realized"],
        "fees": pos["fees"],
        "cid": pos["cid"],
        "opened_at": pos["opened_at"],
        "closed_at": pos["closed_at"] if closed else None,
        "close_px": pos["close_px"],
        "exits": list(pos["exits"]),
        "fills": pos["fills"],
        "legs": len(pos["exits"]),
    }


def position_partial_comparison(positions: list[dict]) -> list[dict]:
    """Partial-vs-single-exit comparison for `rebuild_positions` records.

    The position record already carries every exit, so no time-clustering guess
    is involved. The counterfactual exits the whole quantity once at the last
    observed exit price while paying the same commission, so the delta reduces to
    gross(last exit) - realised: positive means banking the partial cost money,
    negative means it protected the position.
    """
    rows: list[dict] = []
    for pos in positions:
        exits = sorted(pos.get("exits") or [], key=lambda e: e.get("time") or 0)
        qty = float(pos.get("qty") or 0.0)
        entry = float(pos.get("entry") or 0.0)
        fees = float(pos.get("fees") or 0.0)
        realized = float(pos.get("realized") or 0.0)
        sign = 1.0 if pos.get("side") == 1 else -1.0
        row = {
            "sym": pos.get("sym"),
            "side": pos.get("side"),
            "legs": len(exits),
            "qty": qty,
            "entry": entry,
            "actual": realized + fees,
            "actual_fees": fees,
            "realized": realized,
            "partial": len(exits) > 1,
        }
        if len(exits) > 1:
            first_px = float(exits[0].get("px") or 0.0)
            last_px = float(exits[-1].get("px") or 0.0)
            gross = (last_px - entry) * qty * sign
            row.update(
                {
                    "partial_qty": float(exits[0].get("qty") or 0.0),
                    "partial_px": first_px,
                    "final_px": last_px,
                    "single_exit": gross + fees,
                    "delta": gross - realized,
                    "helped": (last_px - first_px) * sign < 0,
                }
            )
        rows.append(row)
    return rows


def _merge_legs(legs: list[dict]) -> dict:
    """Sum the cash flows and quantity of one position's legs."""
    qty = sum(float(t.get("qty") or 0.0) for t in legs)
    notional = sum(float(t.get("qty") or 0.0) * float(t.get("entry") or 0.0) for t in legs)
    closed = [t for t in legs if t.get("closed_at")]
    merged: dict = {
        "sym": legs[0].get("sym"),
        "side": legs[0].get("side"),
        "qty": qty,
        "entry": (notional / qty) if qty else float(legs[0].get("entry") or 0.0),
        "realized": sum(float(t.get("realized") or 0.0) for t in legs),
        "fees": sum(float(t.get("fees") or 0.0) for t in legs),
        "cid": next((t.get("cid") for t in legs if t.get("cid") and t.get("cid") != "manual"), "manual"),
        "opened_at": min(t.get("opened_at") or 0 for t in legs),
        "closed_at": max(t.get("closed_at") or 0 for t in closed) if closed else None,
        "close_px": closed[-1].get("close_px") if closed else None,
        "legs": len(legs),
    }
    for key in ("conf", "stop", "target"):
        merged[key] = next((t.get(key) for t in legs if t.get(key) is not None), None)
    if any(t.get("manual") for t in legs):
        merged["manual"] = True
    return merged


def _cluster_legs(
    trades: list[dict],
    *,
    open_window_ms: float = 180_000.0,
    close_window_ms: float = 180_000.0,
) -> list[list[dict]]:
    """Group legs of one position: same symbol+side, entries and exits close in time."""
    clusters: list[list[dict]] = []
    used = [False] * len(trades)
    order = sorted(range(len(trades)), key=lambda i: trades[i].get("opened_at") or 0)
    for i in order:
        if used[i]:
            continue
        base = trades[i]
        used[i] = True
        cluster = [base]
        for j in order:
            if used[j]:
                continue
            other = trades[j]
            if other.get("sym") != base.get("sym") or other.get("side") != base.get("side"):
                continue
            same_open = (
                abs((other.get("opened_at") or 0) - (base.get("opened_at") or 0))
                <= open_window_ms
            )
            both_closed = bool(other.get("closed_at")) and bool(base.get("closed_at"))
            same_close = both_closed and (
                abs((other.get("closed_at") or 0) - (base.get("closed_at") or 0))
                <= close_window_ms
            )
            if not (same_open or same_close):
                continue
            used[j] = True
            cluster.append(other)
        clusters.append(cluster)
    clusters.sort(key=lambda c: min(t.get("opened_at") or 0 for t in c))
    return clusters


def _fee_rate(legs: list[dict]) -> float:
    """Effective commission rate implied by the fills (median of |fee|/notional)."""
    rates = []
    for t in legs:
        try:
            notional = abs(float(t.get("qty") or 0.0) * float(t.get("entry") or 0.0))
            fee = abs(float(t.get("fees") or 0.0))
        except (TypeError, ValueError):
            continue
        if notional > 0 and fee > 0:
            rates.append(fee / notional)
    if not rates:
        return 0.0
    rates.sort()
    return rates[len(rates) // 2]


def partial_exit_comparison(
    legs: list[dict],
    *,
    open_window_ms: float = 180_000.0,
    close_window_ms: float = 180_000.0,
) -> list[dict]:
    """Compare each position's actual partial-TP outcome with a single-exit exit.

    `tp_partial_close_pct` splits one position into (at least) two legs, paying a
    second commission event. This answers whether that partial earned its extra
    fee: actual net (sum of leg realised + fees) versus a counterfactual that
    exits the whole quantity once, at the last observed exit price. `helped` is
    True when the final exit was worse than the partial exit, i.e. banking the
    partial protected the position.
    """
    rate = _fee_rate(legs)
    rows: list[dict] = []
    for cluster in _cluster_legs(legs, open_window_ms=open_window_ms, close_window_ms=close_window_ms):
        qty = sum(float(t.get("qty") or 0.0) for t in cluster)
        notional = sum(float(t.get("qty") or 0.0) * float(t.get("entry") or 0.0) for t in cluster)
        if qty <= 0:
            continue
        entry = notional / qty
        sign = 1.0 if cluster[0].get("side") == "BUY" else -1.0
        exits = sorted(
            [t for t in cluster if t.get("closed_at") and t.get("close_px")],
            key=lambda t: t.get("closed_at") or 0,
        )
        actual = sum(float(t.get("realized") or 0.0) for t in cluster)
        actual_fees = sum(float(t.get("fees") or 0.0) for t in cluster)
        row = {
            "sym": cluster[0].get("sym"),
            "side": cluster[0].get("side"),
            "legs": len(cluster),
            "qty": qty,
            "entry": entry,
            "actual": actual + actual_fees,
            "actual_fees": actual_fees,
            "partial": len(exits) > 1,
        }
        if len(exits) > 1:
            first_px = float(exits[0].get("close_px") or 0.0)
            last_px = float(exits[-1].get("close_px") or 0.0)
            gross = (last_px - entry) * qty * sign
            hyp_fees = -rate * qty * entry - rate * qty * last_px
            row.update(
                {
                    "partial_qty": float(exits[0].get("qty") or 0.0),
                    "partial_px": first_px,
                    "final_px": last_px,
                    "single_exit": gross + hyp_fees,
                    "delta": (gross + hyp_fees) - (actual + actual_fees),
                    "helped": (last_px - first_px) * sign < 0,
                }
            )
        rows.append(row)
    return rows


def aggregate_legs(
    trades: list[dict],
    *,
    open_window_ms: float = 180_000.0,
    close_window_ms: float = 180_000.0,
) -> list[dict]:
    """Collapse the partial fills / partial closes of one position into one trade.

    `rebuild_trades` deliberately returns per-signal *legs*, but a partial-TP
    position (tp_partial_close_pct) and a tranche-filled limit entry both appear
    as several legs. Reporting them as separate trades inflates the trade count,
    multiplies the per-trade fee drag and mixes entry prices (observed: 7 ADAUSDT
    legs with entries 0.2133-0.2139, all exiting at 0.2114 within the same
    minute). Legs of the same symbol and side whose entry *and* exit both fall
    inside the windows are merged; the `legs` key records how many were folded.
    """
    grouped = [
        _merge_legs(cluster) if len(cluster) > 1 else dict(cluster[0])
        for cluster in _cluster_legs(
            trades, open_window_ms=open_window_ms, close_window_ms=close_window_ms
        )
    ]
    return grouped


def classify_outcome(net: float) -> str:
    """win iff net > 0; zero nets count as loss (Q3 decision, repo-wide)."""
    return "win" if net > 0 else "loss"


def _parse_conf(value: Any) -> int | None:
    try:
        return int(float(str(value))) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def attach_decisions(
    trades: list[dict],
    decision_rows: list[dict],
    *,
    join_hours: int = 48,
) -> tuple[list[dict], dict]:
    """Attach one decision record to every entry trade (see module docstring).

    decision_rows entries are normalized dicts containing at least:
    record_ts (ms int), symbol, and decision (dict with order_direction /
    order_type / entry_price / stop_loss_price / take_profit_price /
    trade_confidence). Unrelated keys (cycle_position, strategy_files, ...)
    are ignored here and consumed later by the outcome store.

    Returns (enriched_trades, audit). Each enriched trade adds: status
    (matched|manual|missing_decision|ambiguous), decision_idx (None unless
    matched), conf/stop/target (parsed, None unless matched). Audit keys:
    matched, manual, missing_decision, ambiguous, ambiguous_rows.
    """
    join_ms = join_hours * 3600 * 1000
    rows_by_cid: dict[str, list[int]] = {}
    row_index = {id(r): i for i, r in enumerate(decision_rows)}
    for i, row in enumerate(decision_rows):
        dec = row.get("decision") if isinstance(row.get("decision"), dict) else None
        if not isinstance(dec, dict):
            continue
        try:
            variants = cid_variants(
                str(row.get("symbol") or ""),
                dec.get("order_direction"),
                dec.get("order_type"),
                dec.get("entry_price"),
                dec.get("stop_loss_price"),
                dec.get("take_profit_price"),
            )
        except (TypeError, ValueError):
            continue
        for c in variants:
            rows_by_cid.setdefault(c, []).append(i)

    audit = {k: 0 for k in _AUDIT_KEYS}
    audit["ambiguous_rows"] = []
    attached: list[dict] = []
    for t in trades:
        enriched = dict(t)
        enriched["status"] = "missing_decision"
        enriched["decision_idx"] = None
        enriched["conf"] = None
        enriched["stop"] = None
        enriched["target"] = None
        cid = str(t.get("cid") or "")
        opened_at = t.get("opened_at")
        if not cid.startswith(ENTRY_CID_PREFIX):
            enriched["status"] = "manual"
            audit["manual"] += 1
            attached.append(enriched)
            continue
        # Deduplicate row indices: one row yields several cid variants.
        cands: list[int] = []
        seen: set[int] = set()
        for i in rows_by_cid.get(cid, []):
            if i not in seen:
                seen.add(i)
                cands.append(i)
        if not cands:
            audit["missing_decision"] += 1
            attached.append(enriched)
            continue
        eligible = [
            i
            for i in cands
            if isinstance(opened_at, (int, float))
            and row_index[id(decision_rows[i])] == i
            and isinstance(decision_rows[i].get("record_ts"), (int, float))
            and 0 <= int(opened_at) - int(decision_rows[i]["record_ts"]) <= join_ms
        ]
        if not eligible:
            enriched["status"] = "ambiguous"
            audit["ambiguous"] += 1
            audit["ambiguous_rows"].append(
                {
                    "cid": cid,
                    "symbol": t.get("sym"),
                    "opened_at": opened_at,
                    "candidates": len(cands),
                }
            )
            attached.append(enriched)
            continue
        chosen = max(eligible, key=lambda i: decision_rows[i]["record_ts"])
        enriched["status"] = "matched"
        enriched["decision_idx"] = chosen
        dec = decision_rows[chosen]["decision"]
        enriched["conf"] = _parse_conf(dec.get("trade_confidence"))
        enriched["stop"] = _parse_float(dec.get("stop_loss_price"))
        enriched["target"] = _parse_float(dec.get("take_profit_price"))
        audit["matched"] += 1
        attached.append(enriched)
    return attached, audit
