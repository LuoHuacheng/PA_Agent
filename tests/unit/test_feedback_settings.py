"""Settings defaults for the Phase-A feedback loop (Task A3)."""
from __future__ import annotations

import json
from pathlib import Path

from pa_agent.config.settings import Settings

EXPECTED = {
    "enabled": False,
    "days": 30,
    "min_samples": 10,
    "max_prompt_lines": 6,
    "group_bys": ["strategy_file", "cycle_direction"],
    "join_hours": 48,
}


def test_feedback_settings_defaults():
    fb = Settings().feedback
    for key, value in EXPECTED.items():
        assert getattr(fb, key) == value


def test_settings_example_contains_feedback_section():
    path = Path("config/settings.example.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["feedback"] == EXPECTED
    # the example must stay loadable by the Settings model
    loaded = Settings.model_validate(raw)
    assert loaded.feedback.enabled is False
