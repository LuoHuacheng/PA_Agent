"""Phase 2 分析: 决策差量 与 置信度分桶 的纯函数单测."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from k2_ab_analyse import confidence_bucket, decision_delta  # noqa: E402


def _inner(**kw):
    base = {"order_type": "限价单", "order_direction": "做多", "entry_price": 100,
            "stop_loss_price": 95, "take_profit_price": 110, "trade_confidence": 57}
    base.update(kw)
    return base


def test_decision_delta_flags_each_tradable_field():
    a = _inner()
    assert decision_delta(a, _inner())["changed"] is False
    for field, value in (("order_type", "不下单"), ("order_direction", "做空"),
                         ("entry_price", 101), ("stop_loss_price", 94),
                         ("take_profit_price", 111)):
        delta = decision_delta(a, _inner(**{field: value}))
        assert delta["changed"] is True, field
        assert delta[field] is True, field
    assert decision_delta(a, _inner(trade_confidence=58))["changed"] is False
    assert decision_delta(a, _inner(trade_confidence=63))["changed"] is True


def test_confidence_bucket_is_ten_wide_and_none_safe():
    assert confidence_bucket(57) == 5
    assert confidence_bucket(63) == 6
    assert confidence_bucket(60) == 6
    assert confidence_bucket(None) is None
    assert confidence_bucket("bad") is None
