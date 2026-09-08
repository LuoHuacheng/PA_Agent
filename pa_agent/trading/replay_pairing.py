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
