"""Stage-2 校验：诊断方向 neutral 时不得提交方向单。

对应入口门控 `neutral_diag_block`（pa_agent/trading/signal_pipeline.py）的上游：
门控只负责"不提交"，被拦信号对模型不可见、无法自纠；这里让同一类矛盾在
校验期报错，模型重试时必须二选一 —— 补 node 2.3 重判方向，或放弃方案。

2026-09-14 现场：SOLUSDT 30m 阶段一 direction=neutral（trading_range），
阶段二沿用 neutral 仍提交 100.79 做多限价单，被门控静默丢弃。
"""
from __future__ import annotations

import json

from pa_agent.ai.json_validator import Ok, ValidationError
from tests.fixtures.validators import schema_test_validator

validator = schema_test_validator()


def _decision(**overrides) -> dict:
    d = {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 100.79,
        "take_profit_price": 101.20,
        "take_profit_price_2": 101.60,
        "stop_loss_price": 100.61,
        "reasoning": "测试样本",
        "diagnosis_confidence": 55,
        "diagnosis_confidence_reasoning": "测试样本",
        "trade_confidence": 56,
        "trade_confidence_reasoning": "测试样本",
        "estimated_win_rate": 52,
        "estimated_win_rate_reasoning": "测试样本",
        "key_factors": [],
        "watch_points": [],
        "risk_assessment": "测试样本",
        "invalidation_condition": "测试样本",
    }
    d.update(overrides)
    return d


def _stage2(decision: dict, *, diag_direction: str = "neutral") -> dict:
    is_no_order = decision.get("order_type") == "不下单"
    trace = [
        {
            "node_id": "10.3",
            "question": "交易者方程是否通过？",
            "answer": "否" if is_no_order else "是",
            "reason": "test",
            "bar_range": "K2-K1",
        }
    ]
    if not is_no_order:
        trace.append(
            {
                "node_id": "11.1",
                "question": "是趋势 / 尖峰状态吗？",
                "answer": "是",
                "reason": "test",
                "bar_range": "K2-K1",
            }
        )
    obj = {
        "decision": decision,
        "diagnosis_summary": {
            "cycle_position": "trading_range",
            "direction": diag_direction,
            "key_signals": [],
        },
        "decision_trace": trace,
        "terminal": {
            "node_id": "10.3" if is_no_order else "11.1",
            "outcome": "reject" if is_no_order else "trade",
            "label": "test",
        },
    }
    if not is_no_order:
        obj["bar_analysis"] = {
            "always_in": "long",
            "last_closed_bar": "K1",
            "bar_type": "trend_bull",
            "signal_bar": {
                "bar": "K2",
                "quality": "strong",
                "pattern": "H1",
                "reason": "test",
            },
            "entry_bar": {
                "bar": "K1",
                "strength": "strong",
                "follow_through": True,
                "still_valid": True,
                "freshness": "fresh",
            },
            "second_entry": {"is_second_entry": False, "type": "none"},
        }
    return obj


def _validate(obj: dict):
    return validator.validate("stage2", json.dumps(obj))


def test_neutral_diag_with_limit_order_rejected() -> None:
    """诊断 neutral + 限价多单 = 校验失败（门控 upstream）。"""
    result = _validate(_stage2(_decision()))
    assert isinstance(result, ValidationError), f"Expected rejection, got {result}"
    assert result.category == "c"
    assert any("diagnosis_summary.direction" in f for f in result.invalid_fields), (
        result.invalid_fields
    )


def test_neutral_diag_with_no_order_accepted() -> None:
    """诊断 neutral + 不下单 = 正常（观望）。"""
    decision = _decision(
        order_type="不下单",
        order_direction=None,
        entry_price=None,
        take_profit_price=None,
        take_profit_price_2=None,
        stop_loss_price=None,
        estimated_win_rate=None,
    )
    result = _validate(_stage2(decision))
    assert isinstance(result, Ok), f"Expected Ok, got {result}"


def test_directional_diag_with_matching_order_accepted() -> None:
    """诊断 bullish + 做多 = 通过（这正是重试该走的路）。"""
    result = _validate(_stage2(_decision(), diag_direction="bullish"))
    assert isinstance(result, Ok), f"Expected Ok, got {result}"


def test_bearish_diag_with_short_order_accepted() -> None:
    """诊断 bearish + 做空 = 通过。"""
    decision = _decision(order_direction="做空", stop_loss_price=101.20, take_profit_price=100.20)
    result = _validate(_stage2(decision, diag_direction="bearish"))
    assert isinstance(result, Ok), f"Expected Ok, got {result}"


def test_auto_stub_neutral_diag_with_order_not_punished() -> None:
    """程序注入的 diagnosis stub（_auto_stub）不罚模型。"""
    obj = _stage2(_decision())
    obj["_auto_stub"] = True
    result = _validate(obj)
    assert isinstance(result, Ok), f"Expected Ok for auto stub, got {result}"


def test_gate_shortcircuited_not_punished() -> None:
    """门控短路输出（程序产物）不罚模型。"""
    obj = _stage2(_decision())
    obj["gate_shortcircuited"] = True
    result = _validate(obj)
    assert isinstance(result, Ok), f"Expected Ok for gate shortcircuit, got {result}"


def test_solusdt_20260914_blocked_record_shape_rejected() -> None:
    """复刻 09-14 12:09 SOLUSDT 那条被门控拦截的记录形态。"""
    obj = _stage2(
        _decision(
            order_type="限价单",
            order_direction="做多",
            entry_price=100.79,
            stop_loss_price=100.61,
            take_profit_price=101.20,
            trade_confidence=56,
            diagnosis_confidence=55,
        ),
        diag_direction="neutral",
    )
    result = _validate(obj)
    assert isinstance(result, ValidationError), f"Expected rejection, got {result}"
    assert any("diagnosis_summary.direction" in f for f in result.invalid_fields)


# ── 重试反馈：豁免段 ──────────────────────────────────────────────────────────

def test_retry_feedback_includes_neutral_conflict_remedy() -> None:
    """命中本检查时，重试反馈必须给出二选一的修正路径（否则模型被禁改清单锁死）。"""
    from pa_agent.ai.retry_feedback import build_retry_feedback

    result = _validate(_stage2(_decision()))
    assert isinstance(result, ValidationError)
    text = build_retry_feedback(result, stage="stage2", attempt=1, max_attempts=3)
    assert "neutral 诊断冲突" in text
    assert "node 2.3" in text
    assert "不下单" in text


def test_retry_feedback_omits_remedy_for_unrelated_errors() -> None:
    """其他 stage2 报错不带豁免段，禁改清单保持原样。"""
    from pa_agent.ai.retry_feedback import build_retry_feedback

    err = ValidationError(
        category="c",
        stage="stage2",
        raw_text="{}",
        invalid_fields=["metrics:RR 不足"],
    )
    text = build_retry_feedback(err, stage="stage2", attempt=1, max_attempts=3)
    assert "neutral 诊断冲突" not in text

