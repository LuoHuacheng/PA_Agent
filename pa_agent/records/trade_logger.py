"""Trade record logger — saves order opportunities to CSV and K-line chart images.

When stage-2 produces an order (限价单 / 突破单 / 市价单), this module:
  1. Appends a rich row to  trade_records/<symbol>_<timeframe>.csv
  2. Renders a K-line + EMA20 chart for the last ≤50 bars and saves it as a
     PNG next to the CSV.

File naming convention
----------------------
CSV   : trade_records/<symbol>_<timeframe>.csv
Image : trade_records/<symbol>_<timeframe>_<timestamp>.png

The image filename uses the same timestamp as the ``record_time`` field so
entries are easy to correlate.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRADE_RECORDS_DIR = Path("trade_records")


def load_last_trade_row(symbol: str, timeframe: str) -> dict[str, str] | None:
    """Newest CSV row for a symbol/timeframe pair, or None.

    Single owner of the record-format read path (writers and readers live in
    this module; ai/feedback/tools consume through this accessor).
    """
    safe_symbol = symbol.replace("/", "-").replace("\\", "-")
    safe_tf = timeframe.replace("/", "-")
    csv_path = TRADE_RECORDS_DIR / f"{safe_symbol}_{safe_tf}.csv"
    if not csv_path.is_file():
        return None
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else None
    except OSError:
        return None


def latest_chart_image(symbol: str, timeframe: str) -> Path | None:
    """Newest chart PNG for a symbol/timeframe pair, or None.

    Public query interface for the image-naming convention documented in the
    module docstring; callers (notify/pipeline) must not glob the records
    directory themselves.
    """
    safe_sym = str(symbol or "").replace("/", "-").replace("\\", "-")
    safe_tf = str(timeframe or "").replace("/", "-")
    try:
        candidates = sorted(
            TRADE_RECORDS_DIR.glob(f"{safe_sym}_{safe_tf}_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return candidates[0] if candidates else None

# ── CSV column definitions ─────────────────────────────────────────────────────

_CSV_FIELDNAMES = [
    # ── Meta ──────────────────────────────────────────────────────────────────
    "record_time",
    "symbol",
    "timeframe",
    "decision_stance",
    "model",
    # ── Decision core ─────────────────────────────────────────────────────────
    "order_direction",
    "order_type",
    "entry_price",
    "stop_loss_price",
    "take_profit_price",
    "take_profit_price_2",
    "entry_rule",
    "entry_basis_bar",
    "entry_basis_extreme",
    # ── Confidence & win-rate ──────────────────────────────────────────────────
    "diagnosis_confidence",
    "diagnosis_confidence_reasoning",
    "trade_confidence",
    "trade_confidence_reasoning",
    "estimated_win_rate",
    "estimated_win_rate_reasoning",
    # ── Reasoning & factors ───────────────────────────────────────────────────
    "reasoning",
    "key_factors",
    "watch_points",
    "risk_assessment",
    "invalidation_condition",
    # ── Diagnosis summary ─────────────────────────────────────────────────────
    "diag_cycle_position",
    "diag_direction",
    "diag_key_signals",
    # ── Bar analysis (stage-2) ────────────────────────────────────────────────
    "s2_always_in",
    "s2_bar_type",
    "s2_signal_bar_bar",
    "s2_signal_bar_quality",
    "s2_signal_bar_pattern",
    "s2_signal_bar_reason",
    "s2_entry_bar_strength",
    "s2_entry_bar_freshness",
    "s2_entry_bar_follow_through",
    "s2_is_second_entry",
    "s2_second_entry_type",
    # ── Next cycle prediction ─────────────────────────────────────────────────
    "next_cycle",
    "next_cycle_direction",
    "next_cycle_probabilities",
    "next_cycle_reasoning",
    # ── Terminal ──────────────────────────────────────────────────────────────
    "terminal_node_id",
    "terminal_outcome",
    "terminal_label",
    # ── Decision trace summary ────────────────────────────────────────────────
    "decision_trace_summary",
    # ── Continuity audit (vs previous CSV row) ────────────────────────────────
    "prev_plan_relation",
    "prev_plan_invalidated",
    "prev_plan_entry",
    "bars_since_prev_plan",
    # ── Image path ────────────────────────────────────────────────────────────
    "chart_image",
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_sr_price(raw: object) -> float | None:
    """Parse a price value that may be a number, string, or range (e.g. '5380-5400').

    Returns the midpoint for ranges, the numeric value for singles, or None.
    """
    import re as _re
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v > 0 else None
    text = str(raw).strip()
    # Range: e.g. "5380-5400" or "5380~5400"
    m = _re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)", text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo + hi) / 2.0
    # Single number
    m2 = _re.search(r"\d+(?:\.\d+)?", text)
    if m2:
        return float(m2.group(0))
    return None


def _j(value: Any) -> str:
    """Serialize a value to a compact JSON string for CSV storage."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _get(d: dict | None, *keys: str, default: Any = "") -> Any:
    """Safe nested dict get."""
    if not isinstance(d, dict):
        return default
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


# ── Public API ────────────────────────────────────────────────────────────────

def save_trade_record(
    *,
    decision_inner: dict,
    stage2_full: dict,
    stage1_diagnosis: dict | None,
    frame: Any,            # KlineFrame or None
    meta_symbol: str,
    meta_timeframe: str,
    decision_stance: str,
    model_name: str,
    structure_flip_cooldown_bars: int = 3,
) -> None:
    """Append one row to the trade CSV and generate the chart image.

    All arguments are best-effort; missing data is recorded as empty string.
    """
    try:
        _save_trade_record_impl(
            decision_inner=decision_inner,
            stage2_full=stage2_full,
            stage1_diagnosis=stage1_diagnosis,
            frame=frame,
            meta_symbol=meta_symbol,
            meta_timeframe=meta_timeframe,
            decision_stance=decision_stance,
            model_name=model_name,
            structure_flip_cooldown_bars=structure_flip_cooldown_bars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("save_trade_record failed: %s", exc, exc_info=True)


def _save_trade_record_impl(
    *,
    decision_inner: dict,
    stage2_full: dict,
    stage1_diagnosis: dict | None,
    frame: Any,
    meta_symbol: str,
    meta_timeframe: str,
    decision_stance: str,
    model_name: str,
    structure_flip_cooldown_bars: int = 3,
) -> None:
    dec = decision_inner or {}
    diag = stage2_full.get("diagnosis_summary") or {}
    bar_analysis = stage2_full.get("bar_analysis") or {}
    terminal = stage2_full.get("terminal") or {}
    next_cycle = stage2_full.get("next_cycle_prediction") or {}
    signal_bar = bar_analysis.get("signal_bar") or {}
    entry_bar = bar_analysis.get("entry_bar") or {}
    second_entry = bar_analysis.get("second_entry") or {}
    now = datetime.now()
    ts_str = now.strftime("%Y%m%d_%H%M%S")
    record_time = now.strftime("%Y-%m-%d %H:%M:%S")

    # ── File paths ────────────────────────────────────────────────────────────
    safe_symbol = meta_symbol.replace("/", "-").replace("\\", "-")
    safe_tf = meta_timeframe.replace("/", "-")
    TRADE_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = TRADE_RECORDS_DIR / f"{safe_symbol}_{safe_tf}.csv"
    image_filename = f"{safe_symbol}_{safe_tf}_{ts_str}.png"
    image_path = TRADE_RECORDS_DIR / image_filename

    # ── Render chart (before CSV so we can record image path) ─────────────────
    chart_written = False
    if frame is not None:
        try:
            from pa_agent.records.trade_chart import render_trade_chart
            bars = list(getattr(frame, "bars", []))
            indicators = getattr(frame, "indicators", None)
            ema20_vals = list(getattr(indicators, "ema20", []) or [])
            chart_written = render_trade_chart(
                bars_newest_first=bars,
                ema20_newest_first=ema20_vals,
                symbol=meta_symbol,
                timeframe=meta_timeframe,
                image_path=image_path,
                entry_price=_parse_sr_price(dec.get("entry_price")),
                stop_loss_price=_parse_sr_price(dec.get("stop_loss_price")),
                take_profit_price=_parse_sr_price(dec.get("take_profit_price")),
                take_profit_price_2=_parse_sr_price(dec.get("take_profit_price_2")),
                order_direction=str(dec.get("order_direction") or ""),
                order_type=str(dec.get("order_type") or ""),
                diagnosis_confidence=str(dec.get("diagnosis_confidence") or ""),
                trade_confidence=str(dec.get("trade_confidence") or ""),
                estimated_win_rate=str(dec.get("estimated_win_rate") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chart render failed: %s", exc)

    # ── Decision trace summary (node_id + answer, compact) ───────────────────
    trace = stage2_full.get("decision_trace") or []
    trace_summary = " | ".join(
        f"{t.get('node_id','')}:{t.get('answer','')}" for t in trace if isinstance(t, dict)
    )

    from pa_agent.ai.decision_continuity import audit_relation_fields

    prev_csv_row = load_last_trade_row(meta_symbol, meta_timeframe)
    audit = audit_relation_fields(
        prev_csv_row,
        dec,
        frame=frame,
        cooldown_bars=structure_flip_cooldown_bars,
    )

    # ── Build CSV row ─────────────────────────────────────────────────────────
    row = {
        "record_time": record_time,
        "symbol": meta_symbol,
        "timeframe": meta_timeframe,
        "decision_stance": decision_stance,
        "model": model_name,

        "order_direction": _get(dec, "order_direction"),
        "order_type": _get(dec, "order_type"),
        "entry_price": _get(dec, "entry_price"),
        "stop_loss_price": _get(dec, "stop_loss_price"),
        "take_profit_price": _get(dec, "take_profit_price"),
        "take_profit_price_2": _get(dec, "take_profit_price_2"),
        "entry_rule": _get(dec, "entry_rule"),
        "entry_basis_bar": _get(dec, "entry_basis_bar"),
        "entry_basis_extreme": _get(dec, "entry_basis_extreme"),

        "diagnosis_confidence": _get(dec, "diagnosis_confidence"),
        "diagnosis_confidence_reasoning": _get(dec, "diagnosis_confidence_reasoning"),
        "trade_confidence": _get(dec, "trade_confidence"),
        "trade_confidence_reasoning": _get(dec, "trade_confidence_reasoning"),
        "estimated_win_rate": _get(dec, "estimated_win_rate"),
        "estimated_win_rate_reasoning": _get(dec, "estimated_win_rate_reasoning"),

        "reasoning": _get(dec, "reasoning"),
        "key_factors": _j(_get(dec, "key_factors")),
        "watch_points": _j(_get(dec, "watch_points")),
        "risk_assessment": _get(dec, "risk_assessment"),
        "invalidation_condition": _get(dec, "invalidation_condition"),

        "diag_cycle_position": _get(diag, "cycle_position"),
        "diag_direction": _get(diag, "direction"),
        "diag_key_signals": _j(_get(diag, "key_signals")),

        "s2_always_in": _get(bar_analysis, "always_in"),
        "s2_bar_type": _get(bar_analysis, "bar_type"),
        "s2_signal_bar_bar": _get(signal_bar, "bar"),
        "s2_signal_bar_quality": _get(signal_bar, "quality"),
        "s2_signal_bar_pattern": _get(signal_bar, "pattern"),
        "s2_signal_bar_reason": _get(signal_bar, "reason"),
        "s2_entry_bar_strength": _get(entry_bar, "strength"),
        "s2_entry_bar_freshness": _get(entry_bar, "freshness"),
        "s2_entry_bar_follow_through": _get(entry_bar, "follow_through"),
        "s2_is_second_entry": _get(second_entry, "is_second_entry"),
        "s2_second_entry_type": _get(second_entry, "type"),

        "next_cycle": _get(next_cycle, "cycle"),
        "next_cycle_direction": _get(next_cycle, "direction"),
        "next_cycle_probabilities": _j(_get(next_cycle, "probabilities")),
        "next_cycle_reasoning": _get(next_cycle, "reasoning"),

        "terminal_node_id": _get(terminal, "node_id"),
        "terminal_outcome": _get(terminal, "outcome"),
        "terminal_label": _get(terminal, "label"),

        "decision_trace_summary": trace_summary,

        "prev_plan_relation": audit.get("prev_plan_relation", ""),
        "prev_plan_invalidated": audit.get("prev_plan_invalidated", ""),
        "prev_plan_entry": audit.get("prev_plan_entry", ""),
        "bars_since_prev_plan": audit.get("bars_since_prev_plan", ""),

        "chart_image": image_filename if chart_written else "",
    }

    # ── Write CSV (rewrite with unified header for schema migrations) ─────────
    existing_rows: list[dict[str, str]] = []
    if csv_path.exists():
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                existing_rows = list(csv.DictReader(f))
        except OSError:
            existing_rows = []
    merged_row = {k: str(row.get(k, "")) for k in _CSV_FIELDNAMES}
    for k, v in row.items():
        if k in _CSV_FIELDNAMES:
            merged_row[k] = "" if v is None else str(v)
    existing_rows.append(merged_row)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for r in existing_rows:
            writer.writerow({k: r.get(k, "") for k in _CSV_FIELDNAMES})

    logger.info("Trade record appended: %s", csv_path)
