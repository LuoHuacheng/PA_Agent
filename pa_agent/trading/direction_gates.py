"""Programmatic direction-quality gates applied before an order is executed.

The model decides; these gates decide whether the model may act on that
decision *right now*. They target the repeated "entry into the wrong side"
losses using pure bar geometry - no LLM text parsing involved:

  G1 close-touch: a resting entry within 2 ticks of the latest closed bar
                  close has no pullback meaning left: it fills at the open of
                  the very next bar, turning a "retest" plan into a chase.
                  ADA 12:00 bought 1 tick above the 11:45 close (0.2229 vs
                  0.2228) and LINK 18:11 bought 1 tick above the 18:00 close
                  (12.268 vs 12.267) - both reversed hard immediately.

  G2 flip:        signal direction opposite to the previous analysis round's
                  stage-1 direction. Entering straight against the diagnosis
                  of the round just before is the least stable setup.

Mode (settings.binance_usdm_testnet.direction_gates_mode):
  off     - disabled
  dry_run - log/count but still execute (default: observe before switching on)
  on      - rejected decisions are blocked from execution and notification
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_G1_CLOSE_TOUCH_TICKS = 2


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _direction_of(decision: dict) -> int | None:
    text = str(decision.get("order_direction") or "").strip()
    if text == "做多":
        return 1
    if text == "做空":
        return -1
    return None


def _stage1_direction_of(record: Any) -> str | None:
    s1 = getattr(record, "stage1_diagnosis", None)
    if isinstance(s1, dict):
        raw = str(s1.get("direction") or "").strip().lower()
        if raw in ("bullish", "bearish", "neutral"):
            return raw
    return None


def _bar_close(bar: Any) -> float | None:
    if isinstance(bar, dict):
        try:
            return float(bar["close"])
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return float(getattr(bar, "close", None))
    except (TypeError, ValueError):
        return None


def evaluate_direction_gates(
    *,
    decision: dict,
    bars: Any,
    tick: float,
    previous_record: Any = None,
) -> list[str]:
    """Return rejected gate reasons (empty list = decision passes)."""
    reasons: list[str] = []
    if not bars or tick is None or tick <= 0:
        return reasons
    sign = _direction_of(decision)
    if sign is None:
        return reasons
    otype = str(decision.get("order_type") or "").strip()
    if otype not in ("限价单", "突破单"):
        return reasons
    entry = _as_float(decision.get("entry_price"))
    if entry is None:
        return reasons

    seq = list(bars or ())
    last_close = _bar_close(seq[0]) if seq else None
    if last_close is None:
        return reasons

    # G1: resting entry hugging the latest close - a chase, not a retest.
    dist_ticks = abs(entry - last_close) / tick
    if dist_ticks <= _G1_CLOSE_TOUCH_TICKS + 1e-9:
        reasons.append(
            f"G1 close-touch: entry {entry:.6g} within {dist_ticks:.1f}t of "
            f"close {last_close:.6g} (chase, no pullback)"
        )

    # G2: fresh opposite-direction flip straight into a new order.
    prev_dir = _stage1_direction_of(previous_record)
    if prev_dir in ("bullish", "bearish"):
        conflict = (sign > 0 and prev_dir == "bearish") or (sign < 0 and prev_dir == "bullish")
        if conflict:
            label = "做多" if sign > 0 else "做空"
            reasons.append(f"G2 flip: {label} signal vs previous round {prev_dir}")
    return reasons
