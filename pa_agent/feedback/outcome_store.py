"""Outcome store: merge account fills with AI decisions (Task A2, v2 contract).

Sources (see docs/superpowers/plans/2026-09-08-A2-outcome-store-review.md §5.1):
- S1 account cache: orders / user_trades / income / positions payloads fetched
  read-only by the existing fetch_account_data CLI plumbing;
- S2 decision records: records/pending JSON (filename-date prefiltered);
- S3 fallback: trade_records CSV rows for decisions missing from S2 (grouping
  keys degrade to the CSV columns; strategy_files stay empty).

Attribution is delegated to pa_agent.trading.replay_pairing.attach_decisions
(exact cid hit, nearest-<=-opened_at disambiguation inside the join window,
manual / missing / ambiguous audit). Only matched trades become OutcomeRows;
everything else is counted in the audit so drift is visible, never silent.

Outcome conventions (repo-wide, Q3 decision): net = realized + fees (funding
excluded), only closed trades count for win rate, net == 0 counts as loss.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pa_agent.trading.replay_pairing import (
    attach_decisions,
    cid_variants,
    classify_outcome,
    rebuild_trades,
)

#: Local timezone used by record filenames / CSV timestamps (repo convention).
_TZ8 = timezone(timedelta(hours=8))

_ORDER_TYPES = ("市价单", "限价单")
_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_([A-Z0-9]+)_([^_]+)\.json$"
)
_CSV_TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: Runtime artifact that lives next to trade CSV logs and must never be
#: treated as a symbol source by symbols_scope.
OUTCOMES_CSV_NAME = "outcomes.csv"

OUTCOME_FIELDNAMES = [
    "uid", "symbol", "timeframe", "direction", "order_type", "entry_avg", "qty",
    "stop", "target", "net_usdt", "fees_usdt", "risk_usdt", "win_r", "outcome",
    "close_reason", "conf", "cycle_position", "diag_direction", "strategy_files",
    "patterns", "stance", "model", "ts_record", "ts_open", "ts_close", "source",
    "record_file",
]

_AUDIT_INCOME_TYPES = ("REALIZED_PNL", "COMMISSION")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _local_ms_to_epoch(local_ms: int) -> int:
    """Epoch ms for a local-clock ms value (fixtures share the +8 frame)."""
    return local_ms


def _parse_name_ts(name: str) -> int | None:
    m = _NAME_RE.match(name)
    if not m:
        return None
    dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H-%M-%S")
    return int(dt.replace(tzinfo=_TZ8).timestamp() * 1000)


def _row_window_boundary(now_ms: int, days: int, margin_hours: int) -> int:
    return int(now_ms - (days * 24 + margin_hours) * 3600 * 1000)


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_order_type(value: Any) -> str | None:
    ot = str(value or "").strip()
    return ot if ot in _ORDER_TYPES else None


def _sig_num(value: Any) -> str:
    if value in (None, ""):
        return ""
    f = float(str(value))
    return str(int(f)) if f == int(f) else str(f)


def _material_key(row: dict) -> tuple[str, ...]:
    dec = row.get("decision") or {}
    return (
        str(row.get("symbol") or ""),
        str(dec.get("order_type") or ""),
        str(dec.get("order_direction") or ""),
        _sig_num(dec.get("entry_price")),
        _sig_num(dec.get("stop_loss_price")),
        _sig_num(dec.get("take_profit_price")),
    )


def _variant_set(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        dec = row.get("decision") or {}
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
        out.update(variants)
    return out


# ---------------------------------------------------------------------------
# symbol scope
# ---------------------------------------------------------------------------


def symbols_scope(
    whitelist: list[str] | None,
    csv_dir: Path,
    default: list[str] | None = None,
) -> list[str]:
    """Whitelist plus CSV symbols; falls back to *default* without a whitelist."""
    out: list[str] = []
    base = [s for s in (whitelist or []) if s] or [s for s in (default or []) if s]
    for s in base:
        s = str(s).upper().strip()
        if s and s not in out:
            out.append(s)
    if csv_dir.is_dir():
        for fp in sorted(csv_dir.glob("*.csv")):
            if fp.name.lower() == OUTCOMES_CSV_NAME:
                continue  # outcomes.csv is not a symbol source
            sym = fp.stem.split("_", 1)[0].upper()
            if sym and sym.isalnum() and sym not in out:
                out.append(sym)
    return out


# ---------------------------------------------------------------------------
# S2 loader (pending JSON, filename-date prefilter)
# ---------------------------------------------------------------------------


def load_decision_records(
    records_root: Path,
    *,
    symbols: list[str],
    days: int = 30,
    now_ms: int | None = None,
    margin_hours: int = 48,
) -> list[dict]:
    now = now_ms if now_ms is not None else int(datetime.now(_TZ8).timestamp() * 1000)
    boundary = _row_window_boundary(now, days, margin_hours)
    wanted = set(symbols)
    rows: list[dict] = []
    if not records_root.is_dir():
        return rows
    for fp in sorted(records_root.glob("*.json")):
        name = fp.name
        name_ts = _parse_name_ts(name)
        if name_ts is None or name_ts < boundary:
            continue
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = raw.get("meta") or {}
        sym = str(meta.get("symbol") or "")
        if sym not in wanted:
            continue
        ts = meta.get("timestamp_local_ms")
        if not isinstance(ts, int):
            ts = name_ts
        inner = ((raw.get("stage2_decision") or {}).get("decision")) or {}
        if _norm_order_type(inner.get("order_type")) is None:
            continue
        s1 = raw.get("stage1_diagnosis") or {}
        rows.append(_normalize_row(
            record_ts=ts,
            symbol=sym,
            timeframe=str(meta.get("timeframe") or ""),
            source="pending",
            record_file=str(fp),
            cycle=s1.get("cycle_position"),
            direction=s1.get("direction"),
            strategy_files=tuple(raw.get("strategy_files_used") or ()),
            patterns=tuple(s1.get("detected_patterns") or ()),
            stance=str(meta.get("decision_stance") or ""),
            model=str((meta.get("ai_provider") or {}).get("model") or ""),
            inner=inner,
        ))
    return rows


# ---------------------------------------------------------------------------
# S3 loader (trade_records CSV fallback)
# ---------------------------------------------------------------------------


def load_csv_fallback(
    csv_dir: Path,
    *,
    symbols: list[str],
    days: int = 30,
    now_ms: int | None = None,
    margin_hours: int = 48,
) -> list[dict]:
    now = now_ms if now_ms is not None else int(datetime.now(_TZ8).timestamp() * 1000)
    boundary = _row_window_boundary(now, days, margin_hours)
    wanted = set(symbols)
    rows: list[dict] = []
    if not csv_dir.is_dir():
        return rows
    for fp in sorted(csv_dir.glob("*.csv")):
        file_sym = fp.stem.split("_", 1)[0].upper()
        if file_sym not in wanted:
            continue
        try:
            text = fp.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line_no, row in enumerate(csv.DictReader(text.splitlines()), start=2):
            sym = str(row.get("symbol") or file_sym).upper()
            if sym not in wanted:
                continue
            rt = str(row.get("record_time") or "").strip()
            try:
                ts = int(datetime.strptime(rt, _CSV_TIME_FMT).replace(tzinfo=_TZ8).timestamp() * 1000)
            except ValueError:
                continue
            if ts < boundary:
                continue
            if _norm_order_type(row.get("order_type")) is None:
                continue
            rows.append(_normalize_row(
                record_ts=ts,
                symbol=sym,
                timeframe=str(row.get("timeframe") or ""),
                source="csv",
                record_file=f"{fp.name}:{line_no}",
                cycle=row.get("diag_cycle_position"),
                direction=row.get("diag_direction"),
                strategy_files=(),
                patterns=(),
                stance="",
                model=str(row.get("model") or ""),
                inner=_csv_inner(row),
            ))
    return rows


def _csv_inner(row: dict) -> dict:
    return {
        "order_direction": str(row.get("order_direction") or ""),
        "order_type": str(row.get("order_type") or ""),
        "entry_price": _to_float(row.get("entry_price")),
        "stop_loss_price": _to_float(row.get("stop_loss_price")),
        "take_profit_price": _to_float(row.get("take_profit_price")),
        "trade_confidence": _to_float(row.get("trade_confidence")),
        "diagnosis_confidence": _to_float(row.get("diagnosis_confidence")),
    }


def _normalize_row(
    *,
    record_ts: int,
    symbol: str,
    timeframe: str,
    source: str,
    record_file: str,
    cycle: Any,
    direction: Any,
    strategy_files: tuple[str, ...],
    patterns: tuple[str, ...],
    stance: str,
    model: str,
    inner: dict,
) -> dict:
    return {
        "record_ts": int(record_ts),
        "symbol": symbol,
        "timeframe": timeframe,
        "cycle_position": str(cycle) if cycle else "",
        "direction": str(direction) if direction else "",
        "strategy_files": strategy_files,
        "patterns": patterns,
        "stance": stance,
        "model": model,
        "source": source,
        "record_file": record_file,
        "decision": dict(inner),
    }


# ---------------------------------------------------------------------------
# source preference
# ---------------------------------------------------------------------------


def prefer_pending_over_csv(
    pending_rows: list[dict],
    csv_rows: list[dict],
    tolerance_ms: int = 900_000,
) -> list[dict]:
    """Merge S2+S3 preferring the richer pending source for same-material rows.

    The same executed order is normally logged twice: as a full pending JSON
    (with strategy_files / detected_patterns) and as a trade_records CSV row
    written a few seconds later. Feeding both into attach_decisions makes the
    slightly-later CSV row win the nearest-time disambiguation, degrading the
    base-rate grouping keys to empty. CSV duplicates within *tolerance_ms* of
    a pending record with the same material are dropped; genuine csv-only
    rows (records cleaned up / GUI-only) are kept.
    """
    pending_keys: dict[tuple[str, ...], int] = {}
    for row in pending_rows:
        key = _material_key(row)
        ts = int(row.get("record_ts") or 0)
        if key not in pending_keys or ts > pending_keys[key]:
            pending_keys[key] = ts
    merged = list(pending_rows)
    for row in csv_rows:
        key = _material_key(row)
        ts = int(row.get("record_ts") or 0)
        p_ts = pending_keys.get(key)
        if p_ts is not None and abs(ts - p_ts) <= tolerance_ms:
            continue  # same order already covered by the richer pending record
        merged.append(row)
    return merged


# ---------------------------------------------------------------------------
# outcome rows
# ---------------------------------------------------------------------------


def _uid(cid: str, opened_at: Any, closed_at: Any) -> str:
    return hashlib.sha256(f"{cid}|{opened_at}|{closed_at}".encode()).hexdigest()


def _close_reason(
    close_order_id: Any, orders_by_symbol: dict[str, list[dict]], symbol: str
) -> str:
    if close_order_id is None:
        return "unknown"
    cid = ""
    found = False
    for o in orders_by_symbol.get(symbol, []):
        if o.get("orderId") == close_order_id:
            cid = str(o.get("clientOrderId") or "")
            found = True
            break
    if cid.startswith("pa-sl-"):
        return "stop"
    if cid.startswith("pa-tp-"):
        return "tp"
    # an existing close order without a pa- prefix is a manual/structure exit;
    # no matching order row at all means the close cause cannot be attributed
    return "structure_or_manual" if found else "unknown"


def build_outcome_rows(
    attached: list[dict],
    decision_rows: list[dict],
    orders_by_symbol: dict[str, list[dict]],
) -> list[dict]:
    """One OutcomeRow per matched trade (contract §5.3)."""
    out: list[dict] = []
    for t in attached:
        if t.get("status") != "matched":
            continue
        idx = t.get("decision_idx")
        row = decision_rows[idx] if isinstance(idx, int) and idx < len(decision_rows) else None
        if row is None:
            continue
        dec = row.get("decision") or {}
        closed = t.get("closed_at") is not None
        net = float(t.get("realized") or 0.0) + float(t.get("fees") or 0.0)
        stop = t.get("stop")
        entry = float(t.get("entry") or 0.0)
        qty = float(t.get("qty") or 0.0)
        risk = None
        if isinstance(stop, (int, float)):
            dist = abs(entry - float(stop))
            if dist > 1e-12:
                risk = qty * dist
        cid = str(t.get("cid") or "")
        sym = str(t.get("sym") or row.get("symbol") or "")
        outcome = "open" if not closed else classify_outcome(net)
        win_r = None
        if closed and risk is not None and risk > 0:
            win_r = net / risk
        close_reason = None if not closed else _close_reason(
            t.get("close_order_id"), orders_by_symbol, sym
        )
        out.append({
            "uid": _uid(cid, t.get("opened_at"), t.get("closed_at")),
            "symbol": sym,
            "timeframe": str(row.get("timeframe") or ""),
            "direction": str(dec.get("order_direction") or ""),
            "order_type": str(dec.get("order_type") or ""),
            "entry_avg": entry,
            "qty": qty,
            "stop": stop,
            "target": t.get("target"),
            "net_usdt": net,
            "fees_usdt": float(t.get("fees") or 0.0),
            "risk_usdt": risk,
            "win_r": win_r,
            "outcome": outcome,
            "close_reason": close_reason,
            "conf": t.get("conf"),
            "cycle_position": str(row.get("cycle_position") or ""),
            "diag_direction": str(row.get("direction") or ""),
            "strategy_files": tuple(row.get("strategy_files") or ()),
            "patterns": tuple(row.get("patterns") or ()),
            "stance": str(row.get("stance") or ""),
            "model": str(row.get("model") or ""),
            "ts_record": row.get("record_ts"),
            "ts_open": t.get("opened_at"),
            "ts_close": t.get("closed_at"),
            "source": str(row.get("source") or ""),
            "record_file": str(row.get("record_file") or ""),
        })
    return out


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple):
        return json.dumps(list(value), ensure_ascii=False)
    return str(value)


def write_outcome_csv(rows: list[dict], path: Path) -> None:
    """Append outcome rows, idempotent on uid; rewrites with a stable header."""
    existing: dict[str, dict] = {}
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                uid = row.get("uid", "")
                if uid:
                    existing[uid] = dict(row)
    for row in rows:
        # uid collisions get refreshed (a richer pending source may arrive
        # after an earlier csv-only pass); identical reruns stay byte-stable
        existing[str(row.get("uid") or "")] = {
            k: _cell(row.get(k)) for k in OUTCOME_FIELDNAMES
        }
    ordered = [existing[k] for k in sorted(existing)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTCOME_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow(row)


def write_audit_json(audit: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(audit, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# audit helpers
# ---------------------------------------------------------------------------


def symbol_income_diff(income_rows: list[dict], trades: list[dict]) -> dict[str, float]:
    trade_syms = {str(t.get("sym") or "") for t in trades}
    trade_syms.discard("")
    if not trade_syms:
        return {}  # nothing to reconcile against
    inc: dict[str, float] = {}
    for r in income_rows:
        if str(r.get("symbol") or "") not in trade_syms:
            continue
        if r.get("incomeType") not in _AUDIT_INCOME_TYPES:
            continue
        sym = str(r.get("symbol") or "")
        if sym:
            inc[sym] = inc.get(sym, 0.0) + float(r.get("income") or 0.0)
    trade_sum: dict[str, float] = {}
    for t in trades:
        sym = str(t.get("sym") or "")
        if sym:
            trade_sum[sym] = trade_sum.get(sym, 0.0) + float(t.get("realized") or 0.0)
            trade_sum[sym] += float(t.get("fees") or 0.0)
    syms = set(inc) | set(trade_sum)
    return {s: trade_sum.get(s, 0.0) - inc.get(s, 0.0) for s in sorted(syms)}


def build_audit_summary(
    attach_audit: dict,
    pending_rows: list[dict],
    csv_rows: list[dict],
    *,
    trades: list[dict],
    income_rows: list[dict],
    csv_only: int,
    pending_only: int,
) -> dict:
    """Merge the attribution audit with cross-source drift and income diff."""
    pending_set = _variant_set(pending_rows)
    csv_set = _variant_set(csv_rows)
    summary = {
        "matched": attach_audit.get("matched", 0),
        "manual": attach_audit.get("manual", 0),
        "missing_decision": attach_audit.get("missing_decision", 0),
        "ambiguous": attach_audit.get("ambiguous", 0),
        "ambiguous_rows": attach_audit.get("ambiguous_rows", []),
        "csv_only_rows": csv_only,
        "pending_only_materials": pending_only,
        "income_diff_by_symbol": symbol_income_diff(income_rows, trades),
        "decisions_pending": len(pending_rows),
        "decisions_csv": len(csv_rows),
        "source_drift": {
            "pending_materials": len(pending_set),
            "csv_materials": len(csv_set),
            "pending_only": len(pending_set - csv_set),
            "csv_only": len(csv_set - pending_set),
        },
    }
    return summary


def merge_all(
    account_cache: dict[str, Any],
    records_root: Path,
    csv_dir: Path,
    *,
    symbols: list[str],
    days: int = 30,
    now_ms: int | None = None,
    join_hours: int = 48,
) -> tuple[list[dict], dict]:
    """End-to-end merge: S1 cache + S2/S3 decisions -> (OutcomeRows, audit).

    *account_cache* mirrors the cached payloads from fetch_account_data:
    keys "orders" / "user_trades" / "income" (dicts keyed by symbol; income is
    a flat list in the fetch output, so both shapes are tolerated).
    """
    orders = account_cache.get("orders") or {}
    fills = account_cache.get("user_trades") or {}
    income_rows = account_cache.get("income") or []
    if isinstance(income_rows, dict):
        income_rows = [r for lst in income_rows.values() for r in lst]

    pending_rows = load_decision_records(
        records_root, symbols=symbols, days=days, now_ms=now_ms
    )
    csv_rows = load_csv_fallback(csv_dir, symbols=symbols, days=days, now_ms=now_ms)
    decision_rows = prefer_pending_over_csv(pending_rows, csv_rows)

    trades = rebuild_trades(orders, fills, symbols)
    attached, attach_audit = attach_decisions(trades, decision_rows, join_hours=join_hours)
    rows = build_outcome_rows(attached, decision_rows, orders)

    pending_set = _variant_set(pending_rows)
    csv_set = _variant_set(csv_rows)
    csv_only = sum(1 for row in csv_rows if not (set(cid_variants(
        row["symbol"], row["decision"].get("order_direction"), row["decision"].get("order_type"),
        row["decision"].get("entry_price"), row["decision"].get("stop_loss_price"),
        row["decision"].get("take_profit_price"),
    )) & pending_set))
    pending_only = len(pending_set - csv_set)

    audit = build_audit_summary(
        attach_audit, pending_rows, csv_rows,
        trades=trades,
        income_rows=income_rows,
        csv_only=csv_only,
        pending_only=pending_only,
    )
    audit["rows_outcome"] = len(rows)
    return rows, audit
