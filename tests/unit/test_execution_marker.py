"""执行标记: status + gate_reasons + reason 三件套 (2026-09-20 补 reason)."""
from __future__ import annotations

from pa_agent.records.execution_marker import (
    execution_status,
    gate_reasons,
    mark_execution,
    plan_is_working,
)


def test_mark_execution_records_reason():
    decision = {}
    mark_execution(decision, status="rejected", reason="Notional exceeds cap")
    assert decision["execution"] == {
        "status": "rejected",
        "reason": "Notional exceeds cap",
    }


def test_mark_execution_omits_blank_reason():
    decision = {}
    mark_execution(decision, status="pending", reason="   ")
    assert "reason" not in decision["execution"]


def test_mark_execution_keeps_gate_reasons_and_reason():
    decision = {}
    mark_execution(
        decision,
        status="gate_blocked",
        gate_reasons=("short_block: ...",),
        reason="执行层文案",
    )
    marker = decision["execution"]
    assert marker["gate_reasons"] == ["short_block: ..."]
    assert marker["reason"] == "执行层文案"


def test_readers_still_work():
    decision = {}
    mark_execution(decision, status="rejected", reason="Duplicate position")
    record = {"stage2_decision": decision}
    assert execution_status(record) == "rejected"
    assert gate_reasons(record) == []
    assert plan_is_working(record) is False


def test_unmarked_record_is_treated_as_working():
    assert plan_is_working({"stage2_decision": {}}) is True
