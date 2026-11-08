"""Display-only local cycle probabilities; no trading or API dependencies."""
import copy
from dataclasses import replace

import pytest

from pa_agent.ai.cycle_enums import CYCLE_ORDER
from pa_agent.ai.local_cycle_forecast import (
    TRANSITIONS,
    fill_cycle_probabilities,
    percentages,
)
from tests.integration.conftest import make_frame


@pytest.mark.parametrize("cycle", list(TRANSITIONS))
def test_each_reference_state_has_full_integer_distribution(cycle):
    pred = {"direction": "bullish", "reasoning": "模型原文", "unpredictable": False}
    before = copy.deepcopy(pred)
    out = fill_cycle_probabilities(pred, stage1_json={"cycle_position": cycle})
    probs = out["probabilities"]
    assert set(probs) == set(CYCLE_ORDER)
    assert sum(probs.values()) == 100
    assert all(isinstance(p, int) and 0 <= p <= 100 for p in probs.values())
    assert out["cycle"] == max(probs, key=probs.get)
    assert out["probability_source"] == "program_rules"
    assert out["direction"] == "bullish" and out["reasoning"] == "模型原文"
    assert pred == before


def test_normalization_rounding_and_invalid_total():
    assert sum(percentages({k: 1 for k in CYCLE_ORDER}).values()) == 100
    assert sum(percentages({}).values()) == 100


def test_spike_prefers_following_channel_over_new_spike():
    out = fill_cycle_probabilities({}, stage1_json={"cycle_position": "spike"})
    p = out["probabilities"]
    assert p["tight_channel"] + p["normal_channel"] > p["spike"]
    assert p["trading_range"] > 0


def test_current_snapshot_only_ignores_forming_bar():
    frame = make_frame()
    mutated = replace(frame, bars=(replace(frame.bars[0], open=1, high=999999, low=0, close=999999), *frame.bars[1:]))
    p = {"direction": "bullish", "reasoning": "原文"}
    a = fill_cycle_probabilities(p, frame=frame)
    b = fill_cycle_probabilities(p, frame=mutated)
    assert a == b
    assert a["probability_evidence"]["closed_bars"] == 19


def test_scale_invariance_no_absolute_price_threshold():
    frame = make_frame()
    scaled = replace(frame, bars=tuple(replace(b, open=b.open*100, high=b.high*100, low=b.low*100, close=b.close*100) for b in frame.bars))
    p = {"direction": "bearish", "reasoning": "保留模型方向"}
    a, b = fill_cycle_probabilities(p, frame=frame), fill_cycle_probabilities(p, frame=scaled)
    assert a["probabilities"] == b["probabilities"]
    assert a["probability_current_cycle"] == b["probability_current_cycle"]
    assert a["direction"] == "bearish"


def test_insufficient_bars_uses_documented_prior():
    frame = replace(make_frame(), bars=())
    out = fill_cycle_probabilities({"direction": None, "reasoning": "无法判断", "unpredictable": True}, frame=frame)
    assert out["probability_current_cycle"] == "unknown"
    assert sum(out["probabilities"].values()) == 100
    assert out["unpredictable"] is True


