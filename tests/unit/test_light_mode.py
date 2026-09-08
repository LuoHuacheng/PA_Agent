"""Unit tests for monitor light mode (Phase C, Task C3)."""
from __future__ import annotations

from types import SimpleNamespace

from pa_agent.ai.decision_tree import build_stage2_light_skip_response
from pa_agent.config.settings import Settings
from pa_agent.monitoring.light_gate import (
    features_of,
    record_has_active_plan,
    stage1_of_record,
    structure_event_detected,
)

STAGE1 = {
    "gate_result": "proceed",
    "cycle_position": "trading_range",
    "direction": "neutral",
    "diagnosis_confidence": 62,
    "key_signals": ["边界清晰"],
    "program_features": {
        "breakout_quality": "failed",
        "zone": "middle_third",
        "swing_structure": "mixed",
        "barbwire_candidate": False,
        "spike_aftermath_hint": "none",
        "scale_conflict": False,
    },
}


def test_light_skip_response_shape():
    out = build_stage2_light_skip_response(STAGE1, reason="连续安静 3 根无结构事件")
    assert out["light_skip_stage2"] is True
    assert out["decision"]["order_type"] == "不下单"
    assert "轻量模式" in out["decision"]["reasoning"]
    assert out["decision_trace"] == []
    assert out["terminal"]["outcome"] == "wait"
    assert out["diagnosis_summary"]["cycle_position"] == "trading_range"


def test_structure_event_detected_on_feature_change():
    quiet = dict(STAGE1)
    assert structure_event_detected(quiet, quiet) is False
    changed = dict(STAGE1)
    changed["program_features"] = dict(STAGE1["program_features"],
                                       breakout_quality="surviving")
    assert structure_event_detected(quiet, changed) is True
    # route-level changes also count as events
    routed = dict(STAGE1, direction="bullish")
    assert structure_event_detected(quiet, routed) is True
    # missing context never skips
    assert structure_event_detected(None, quiet) is True
    assert structure_event_detected(quiet, None) is True


def test_stage1_of_record_and_active_plan():
    rec = SimpleNamespace(stage1_diagnosis=STAGE1, stage2_decision={
        "decision": {"order_type": "限价单", "entry_price": 1.0},
    })
    assert stage1_of_record(rec) is STAGE1
    assert record_has_active_plan(rec) is True
    rec2 = SimpleNamespace(stage1_diagnosis=None, stage2_decision={
        "decision": {"order_type": "不下单"},
    })
    assert record_has_active_plan(rec2) is False
    assert stage1_of_record({"stage1_diagnosis": STAGE1}) is STAGE1


def test_light_mode_settings_defaults_and_example():
    cfg = Settings().monitoring
    assert cfg.light_mode_enabled is False
    assert cfg.light_mode_max_quiet_bars == 3
    import json
    from pathlib import Path

    raw = json.loads(Path("config/settings.example.json").read_text(encoding="utf-8"))
    assert raw["monitoring"]["light_mode_enabled"] is False
    assert raw["monitoring"]["light_mode_max_quiet_bars"] == 3
    assert features_of({"program_features": {"a": 1}}) == {"a": 1}
    assert features_of(None) == {}
