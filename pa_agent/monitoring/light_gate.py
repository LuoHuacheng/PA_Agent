"""Monitor light-mode event gate (Phase C, Task C3).

Pure helpers deciding whether the current bar carries a program-visible
structural event worth a full two-stage analysis. Stage-2 skipping only
happens after max_quiet_bars consecutive quiet bars; any event, an active
plan or an open position resets the counter.
"""
from __future__ import annotations

from typing import Any

from pa_agent.records.execution_marker import plan_is_working

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
    """True when the previous decision still works an entry order.

    A plan the execution layer never submitted (entry gate / rejection /
    dry-run) leaves nothing on the exchange, so it must not keep the light-mode
    gate thinking an order is working.
    """
    s2 = getattr(record, "stage2_decision", None)
    if not isinstance(s2, dict):
        s2 = record.get("stage2_decision") if isinstance(record, dict) else None
    if not isinstance(s2, dict):
        return False
    inner = s2.get("decision")
    if not isinstance(inner, dict):
        return False
    if str(inner.get("order_type") or "") not in _ACTIVE_ORDER_TYPES:
        return False
    return plan_is_working(record)

def session_blocked_skip_reason(cfg: Any, *, now_hour: int | None = None) -> str:
    """入口门控已禁时段内的阶段二跳过理由; 不适用时空串。

    只有"闸门确实开着 + 当前落在窗口内"才给理由。闸门一放开(放行实验)本函数立刻
    返回空串, 否则放行期会白丢 stage2 的下单机会 —— 这个耦合是刻意的: 省调用永远
    不能以错过真实入场为代价。窗口与判据都取自 binance 段的 block_session_*。
    """
    from pa_agent.trading.signal_pipeline import session_block_active

    if not session_block_active(cfg, now_hour=now_hour):
        return ""
    return "session_blocked_window: 入口门控已禁该时段, 跳过阶段二"
