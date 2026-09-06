# ruff: noqa: RUF001 - Chinese product copy
"""Structure-failure auto exit: let a holding position react to the diagnosis.

The old design only protected open positions with static exchange-side orders
(entry stop / TP / breakeven guard). A bar-close analysis that keeps calling
the setup failed (direction flipped against the position, price below entry)
had no way to close the trade early - the position just rode to the static
stop. This module closes that gap.

Pipeline (evaluated once per symbol after each closed-bar analysis):

  1. configuration mode: off | dry_run | on
  2. open position exists and matches a recorded pa-entry signal row
  3. stage-1 diagnosis direction has been against the position for
     structure_exit_confirm_bars consecutive closed bars
  4. latest close already crossed entry (unfavourable) but has NOT hit the
     static stop (that case belongs to the protective order)
  5. action: market reduce-only close (on) or a notice only (dry_run)

Safety notes
------------
- The static stop/TP watchers keep running unchanged; this path only acts
  *before* the stop is hit.
- close_market_position is reduceOnly and sized to the live position, so a
  racing pa-sl close degrades this to a harmless no-op.
- dry_run mode never trades; it notifies and logs what would have happened.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pa_agent.ai.decision_continuity import load_last_trade_csv_row
from pa_agent.util.price_tick import infer_price_tick_from_frame

logger = logging.getLogger(__name__)

_ROW_SIGN = {"做多": 1, "做空": -1}
_CLOSE_SIDE = {1: "SELL", -1: "BUY"}
_POSITION_LABEL = {1: "多", -1: "空"}


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _anti_direction_of_position(sign: int) -> str | None:
    """Stage-1 direction that negates an open position (long->bearish...)."""
    if sign > 0:
        return "bearish"
    if sign < 0:
        return "bullish"
    return None


def evaluate_structure_failure_exit(
    *,
    symbol: str,
    timeframe: str,
    record: Any,
    frame: Any,
    settings: Any,
    counts: dict,
    client: Any,
    decision_row: dict | None = None,
    notify: Callable[[str], bool] | None = None,
) -> dict:
    """Evaluate one closed-bar analysis against the open position.

    Returns a verdict dict with keys: action (none|dry_exit|exit|failed),
    reason, and informational fields. counts is a mutable per-symbol
    streak map kept by the caller (persisted between polls).
    """
    out: dict = {"action": "none", "reason": "", "symbol": symbol}
    cfg = getattr(settings, "binance_usdm_testnet", None)
    mode = str(getattr(cfg, "structure_exit_mode", "off") or "off").strip()
    if mode not in ("off", "dry_run", "on"):
        logger.warning("structure_exit_mode=%r ignored (off|dry_run|on)", mode)
        mode = "off"
    if mode == "off":
        return out
    confirm_bars = max(1, int(getattr(cfg, "structure_exit_confirm_bars", 2) or 2))

    try:
        pos = client.position_info(symbol)
    except Exception as exc:
        logger.warning("structure-exit position check failed for %s: %s", symbol, exc)
        return out
    amount = _as_float(pos.get("amount"))
    if not amount or abs(amount) < 1e-12:
        counts.pop(symbol, None)
        return out
    sign = 1 if amount > 0 else -1
    entry = _as_float(pos.get("entry"))
    if entry is None:
        counts.pop(symbol, None)
        return out

    s1 = getattr(record, "stage1_diagnosis", None) or {}
    if not isinstance(s1, dict):
        s1 = {}
    direction = str(s1.get("direction") or "").strip().lower()
    anti_dir = _anti_direction_of_position(sign)

    streak = counts.setdefault(symbol, {})
    if direction == anti_dir:
        streak["dir"] = anti_dir
        streak["n"] = int(streak.get("n") or 0) + 1
    else:
        streak.clear()
        streak["dir"] = anti_dir if anti_dir else ""
        streak["n"] = 0
    neg_bars = int(streak.get("n") or 0)
    if direction != anti_dir or neg_bars < confirm_bars:
        return out

    # Match the recorded signal that opened this position for entry/stop.
    row = decision_row if decision_row is not None else load_last_trade_csv_row(symbol, timeframe)
    if not isinstance(row, dict):
        return out
    row_sign = _ROW_SIGN.get(str(row.get("order_direction") or "").strip())
    row_entry = _as_float(row.get("entry_price"))
    row_stop = _as_float(row.get("stop_loss_price"))
    if row_sign != sign or row_entry is None or row_stop is None:
        return out
    tick = float(infer_price_tick_from_frame(frame) or 0.01)
    if abs(row_entry - entry) > max(tick * 3, tick):
        return out  # position not opened by this signal (manual/other)

    bars = list(getattr(frame, "bars", ()) or ())
    if not bars:
        return out
    try:
        close = float(getattr(bars[0], "close", 0))
    except (TypeError, ValueError):
        return out

    if sign > 0:
        if close <= row_stop or close >= entry:
            return out
        gap = entry - close
    else:
        if close >= row_stop or close <= entry:
            return out
        gap = close - entry

    kind = _POSITION_LABEL[sign]
    reason = (
        f"结构否定x{neg_bars}：持仓{kind}单 @{entry:.4f}，诊断连续看反侧，"
        f"最新收盘 {close:.4f} 已破入场但未触止损 {row_stop:.4f}（浮亏 {gap:.4f}）"
    )
    out["reason"] = reason
    out["confirm_bars"] = neg_bars
    out["close_px"] = close
    out["entry"] = entry
    out["stop"] = row_stop

    if mode == "dry_run":
        out["action"] = "dry_exit"
        logger.info("[结构否定] %s dry-run: %s", symbol, reason)
        if notify is not None:
            notify("[dry-run] " + reason)
        return out

    try:
        client.close_market_position(
            symbol=symbol,
            side=_CLOSE_SIDE[sign],
            quantity=Decimal(str(abs(amount))),
        )
    except Exception as exc:
        logger.error("structure-exit close failed for %s: %s", symbol, exc)
        out["action"] = "failed"
        out["reason"] = reason + "；平仓失败: " + str(exc)
        return out
    counts.pop(symbol, None)
    out["action"] = "exit"
    logger.info("[结构否定] %s 自动平仓: %s", symbol, reason)
    if notify is not None:
        notify("[结构否定自动平仓] " + reason)
    return out
