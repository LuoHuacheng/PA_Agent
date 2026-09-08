"""Monitor light-mode event gate (Phase C, Task C3).

Pure helpers deciding whether the current bar carries a program-visible
structural event worth a full two-stage analysis. Stage-2 skipping only
happens after max_quiet_bars consecutive quiet bars; any event, an active
plan or an open position resets the counter.
"""
from __future__ import annotations

from typing import Any

#: program_features keys that indicate structure changes between bars
STRUCTURE_KEYS = (
    "breakout_quality",
    "zone",
    "swing_structure",
    "barbwire_candidate",
    "spike_aftermath_hint",
    "scale_conflict",
)
TOP_KEYS = ("cycle_position", "direction", "gate_result")
_ACTIVE_ORDER_TYPES = ("市价单", "限价单")


def features_of(stage1: dict | None) -> dict:
    if not isinstance(stage1, dict):
        return {}
    pf = stage1.get("program_features")
    return pf if isinstance(pf, dict) else {}


def structure_event_detected(prev_stage1: dict | None, new_stage1: dict | None) -> bool:
    """True when structure features or the top-level route changed between bars."""
    if not isinstance(new_stage1, dict):
        return True  # no diagnosis yet -> never skip
    prev = features_of(prev_stage1)
    new = features_of(new_stage1)
    if not new:
        return True
    if not prev:
        return True  # first comparison without context
    return any(str(prev.get(k)) != str(new.get(k)) for k in STRUCTURE_KEYS) or any(
        str((prev_stage1 or {}).get(k)) != str(new_stage1.get(k)) for k in TOP_KEYS
    )


def stage1_of_record(record: Any) -> dict | None:
    stage1 = getattr(record, "stage1_diagnosis", None)
    if isinstance(stage1, dict):
        return stage1
    if isinstance(record, dict):
        return record.get("stage1_diagnosis")
    return None


def record_has_active_plan(record: Any) -> bool:
    """True when the previous decision still works an entry order."""
    s2 = getattr(record, "stage2_decision", None)
    if not isinstance(s2, dict):
        s2 = record.get("stage2_decision") if isinstance(record, dict) else None
    if not isinstance(s2, dict):
        return False
    inner = s2.get("decision")
    if not isinstance(inner, dict):
        return False
    return str(inner.get("order_type") or "") in _ACTIVE_ORDER_TYPES
