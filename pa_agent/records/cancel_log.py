"""Append-only audit trail for canceled orders.

Why this exists
---------------
The rotating application log keeps 5 MB x 10 files, which at the current log
volume covers roughly ten hours - shorter than the 12-hour window used to
review cancel reasons. Cancel events were also only visible as free text, so
answering "why was this order canceled?" meant hand-grepping several rotated
files.

Every successful or failed cancel now also lands here as one JSON object per
line, with a stable reason enum and the order identity. Daily files keep the
trail cheap to read and cheap to prune.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pa_agent.config.paths import CANCEL_LOG_DIR

logger = logging.getLogger(__name__)

#: Stable reason enum. Never rename an existing value - only add new ones -
#: because historical lines are read back by analysis tooling.
REASON_LIMIT_ENTRY_TIMEOUT = "limit_entry_timeout"
REASON_PLAN_REPLACED = "plan_replaced"
REASON_STALE_ENTRY_REMOVED = "stale_entry_removed"
REASON_CANCEL_FAILED = "cancel_failed"

CANCEL_REASONS = frozenset(
    {
        REASON_LIMIT_ENTRY_TIMEOUT,
        REASON_PLAN_REPLACED,
        REASON_STALE_ENTRY_REMOVED,
        REASON_CANCEL_FAILED,
    }
)

_JSON_SEPARATORS = (",", ":")


def cancel_log_path(*, now: float | None = None, base_dir: Path | None = None) -> Path:
    """Return the daily cancel log path covering the local date of "now"."""
    directory = Path(base_dir) if base_dir is not None else CANCEL_LOG_DIR
    stamp = time.time() if now is None else now
    return directory / f"cancels-{datetime.fromtimestamp(stamp):%Y-%m-%d}.jsonl"


def record_cancel(
    *,
    symbol: str,
    client_id: str,
    reason: str,
    detail: str = "",
    environment: str = "",
    entry_price: Any = None,
    signal_id: str = "",
    base_dir: Path | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> Path | None:
    """Append one cancel event and return the file written.

    Returns None when the write fails: the audit trail must never abort an
    order flow, so I/O problems degrade to a warning. An unknown reason still
    raises ValueError - that is a programming error, not a runtime condition.
    """
    if reason not in CANCEL_REASONS:
        raise ValueError(f"Unknown cancel reason: {reason!r}")

    stamp = time.time() if now is None else now
    path = cancel_log_path(now=stamp, base_dir=base_dir)
    record: dict[str, Any] = {
        "ts_ms": int(stamp * 1000),
        "ts_iso": datetime.fromtimestamp(stamp).isoformat(timespec="seconds"),
        "symbol": symbol,
        "client_id": client_id,
        "reason": reason,
        "detail": detail,
        "environment": environment,
        "entry_price": None if entry_price is None else str(entry_price),
        "signal_id": signal_id,
    }
    if extra:
        record["extra"] = extra
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=_JSON_SEPARATORS) + "\n")
    except OSError as exc:
        logger.warning("Cancel audit log write failed (%s): %s", path, exc)
        return None
    return path
