"""Tests for the unified order SignalPipeline (GUI/monitor shared seam)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pa_agent.config.settings import Settings
from pa_agent.trading.signal_pipeline import (
    OrderSignal,
    SignalPipeline,
    frame_atr_pct,
    signal_notification_allowed,
)


class _Res:
    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason
        self.symbol = "BTCUSDT"


def _frame(atr: float = 1.5, close: float = 100.0) -> Any:
    return type(
        "Frame",
        (),
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "bars": (type("Bar", (), {"close": close})(),),
            "indicators": type("Ind", (), {"atr14": (atr,)})(),
        },
    )()


def _decision(order_type: str = "市价单", conf: int = 90) -> dict:
    return {
        "decision": {
            "order_type": order_type,
            "order_direction": "做多",
            "trade_confidence": conf,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 110,
        }
    }


def _signal(decision: dict, frame: Any = None) -> OrderSignal:
    return OrderSignal(
        decision=decision,
        inner=decision["decision"],
        symbol="BTCUSDT",
        timeframe="15m",
        frame=frame,
    )


def _patch_io(monkeypatch, exec_status: str = "submitted") -> tuple[list, list]:
    """Keep the pipeline away from disk/network; return (exec_calls, notified)."""
    exec_calls: list[dict] = []
    notified: list[str] = []
    monkeypatch.setattr(
        "pa_agent.records.trade_logger.save_trade_record", lambda **_kw: None
    )
    monkeypatch.setattr(
        "pa_agent.records.trade_logger.latest_chart_image",
        lambda _s, _t: None,
    )

    def fake_execute(inner, _settings, *, analysis_symbol=""):
        exec_calls.append({"inner": inner, "analysis_symbol": analysis_symbol})
        return _Res(exec_status)

    monkeypatch.setattr(
        "pa_agent.trading.binance_usdm_testnet.execute_market_signal", fake_execute
    )
    for mod in ("telegram_notifier", "feishu_notifier", "pushplus_notifier"):
        monkeypatch.setattr(
            f"pa_agent.notify.{mod}.send_order_signal",
            lambda _m=mod, **_kw: notified.append(_m) or True,
        )
    return exec_calls, notified


def test_below_stance_threshold_is_not_an_opportunity() -> None:
    settings = Settings()
    settings.general.decision_stance = "conservative"  # floor 55
    pipeline = SignalPipeline(settings)
    assert pipeline.evaluate(_decision(conf=54)["decision"]).opportunity is False
    assert pipeline.evaluate(_decision(conf=55)["decision"]).opportunity is True


def test_user_floor_raisess_threshold_above_stance() -> None:
    settings = Settings()
    settings.general.decision_stance = "extreme_aggressive"  # floor 25
    settings.general.decision_confidence_threshold = 70
    pipeline = SignalPipeline(settings)
    assert pipeline.confidence_threshold() == 70
    assert pipeline.evaluate(_decision(conf=69)["decision"]).opportunity is False
    assert pipeline.evaluate(_decision(conf=70)["decision"]).opportunity is True


def test_dispatch_injects_atr_pct_from_frame(monkeypatch) -> None:
    settings = Settings()
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(settings)
    result = pipeline.dispatch(_signal(_decision(), frame=_frame(atr=1.5, close=100.0)))
    assert result.opportunity is True
    assert exec_calls and exec_calls[0]["inner"]["atr_pct"] == 1.5
    assert exec_calls[0]["analysis_symbol"] == "BTCUSDT"


def test_gate_on_mode_blocks_execution_and_counts(monkeypatch) -> None:
    settings = Settings()
    settings.binance_usdm_testnet.direction_gates_mode = "on"
    exec_calls, notified = _patch_io(monkeypatch)
    hits: list[str] = []
    monkeypatch.setattr(
        "pa_agent.util.price_tick.infer_price_tick_from_frame", lambda _f: 0.01
    )
    monkeypatch.setattr(
        "pa_agent.trading.direction_gates.evaluate_direction_gates",
        lambda **_kw: ["G1: 逆势", "G2: 中性带"],
    )
    pipeline = SignalPipeline(settings, on_gate_hit=hits.append)
    result = pipeline.dispatch(_signal(_decision(), frame=_frame()))
    assert result.gate_blocked is True
    assert exec_calls == [] and notified == []
    assert hits and "G1" in hits[0] and "G2" in hits[0]


def test_gate_dry_run_logs_but_passes(monkeypatch) -> None:
    settings = Settings()
    settings.binance_usdm_testnet.direction_gates_mode = "dry_run"
    exec_calls, notified = _patch_io(monkeypatch)
    monkeypatch.setattr(
        "pa_agent.util.price_tick.infer_price_tick_from_frame", lambda _f: 0.01
    )
    monkeypatch.setattr(
        "pa_agent.trading.direction_gates.evaluate_direction_gates",
        lambda **_kw: ["G1: 逆势"],
    )
    pipeline = SignalPipeline(settings, on_gate_hit=lambda _k: None)
    result = pipeline.dispatch(_signal(_decision()))
    assert result.gate_blocked is False
    assert exec_calls and notified


def test_rejected_execution_suppresses_all_channels(monkeypatch) -> None:
    settings = Settings()
    _exec_calls, notified = _patch_io(monkeypatch, exec_status="rejected")
    pipeline = SignalPipeline(settings)
    result = pipeline.dispatch(_signal(_decision()))
    assert result.exec_result is not None and result.exec_result.status == "rejected"
    assert notified == []


def test_failed_execution_pushes_channels_and_keeps_result(monkeypatch) -> None:
    settings = Settings()
    _exec_calls, notified = _patch_io(monkeypatch, exec_status="failed")
    failures: list[str] = []
    monkeypatch.setattr(
        "pa_agent.notify.telegram_notifier.send_execution_failure",
        lambda **_kw: failures.append("telegram") or True,
    )
    pipeline = SignalPipeline(settings)
    result = pipeline.dispatch(_signal(_decision()))
    assert result.exec_result is not None and result.exec_result.status == "failed"
    assert sorted(notified) == ["feishu_notifier", "pushplus_notifier", "telegram_notifier"]
    assert failures == ["telegram"]


def test_signal_notification_allowed_only_suppresses_rejected() -> None:
    assert signal_notification_allowed(None) is True
    for ok in ("submitted", "pending", "dry_run", "skipped", "failed"):
        assert signal_notification_allowed(_Res(ok)) is True, ok
    assert signal_notification_allowed(_Res("rejected")) is False


def test_frame_atr_pct_handles_warmup_and_bad_input() -> None:
    assert frame_atr_pct(_frame(atr=1.5, close=100.0)) == 1.5
    nan_frame = type(
        "Frame",
        (),
        {
            "bars": (type("Bar", (), {"close": 100})(),),
            "indicators": type("Ind", (), {"atr14": (float("nan"),)})(),
        },
    )()
    assert frame_atr_pct(nan_frame) is None
    assert frame_atr_pct(type("Frame", (), {})()) is None


def test_gate_off_skips_gate_evaluation_entirely(monkeypatch, tmp_path: Path) -> None:
    settings = Settings()
    called = {"gates": 0}

    def _fail(**_kw):
        called["gates"] += 1
        raise AssertionError("mode=off 时不得评估方向闸门")

    monkeypatch.setattr(
        "pa_agent.trading.direction_gates.evaluate_direction_gates", _fail
    )
    pipeline = SignalPipeline(settings)
    assert pipeline.evaluate(_decision(), frame=_frame()).gate_mode == "off"
    assert called["gates"] == 0


# ── 入口环境门控 (block_*_entry) ────────────────────────────────────────────

def _env_signal(direction: str, cycle: str, diag: str, conf: int = 90) -> OrderSignal:
    decision = {
        "decision": {
            "order_type": "市价单",
            "order_direction": direction,
            "trade_confidence": conf,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 110,
        },
        "diagnosis_summary": {"cycle_position": cycle, "direction": diag},
    }
    return OrderSignal(decision=decision, inner=decision["decision"], symbol="BTCUSDT")


def _env_settings(**flags: bool) -> Settings:
    settings = Settings()
    settings.general.decision_stance = "balanced"  # floor 40, conf=90 必过
    cfg = settings.binance_usdm_testnet
    for key, value in flags.items():
        setattr(cfg, key, value)
    return settings


def test_all_entry_gates_off_by_default(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings())
    result = pipeline.dispatch(_env_signal("做空", "trending_tr", "neutral"))
    assert result.gate_blocked is False
    assert len(exec_calls) == 1


def test_block_short_entry(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_short_entry=True))
    result = pipeline.dispatch(_env_signal("做空", "normal_channel", "bullish"))
    assert result.gate_blocked and "short_block" in result.gate_reasons[0]
    assert exec_calls == []
    # 做多不受影响
    result2 = pipeline.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    assert not result2.gate_blocked and len(exec_calls) == 1


def test_block_trending_tr_entry(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_trending_tr_entry=True))
    result = pipeline.dispatch(_env_signal("做多", "trending_tr", "bullish"))
    assert result.gate_blocked and "trending_tr_block" in result.gate_reasons[0]
    assert exec_calls == []


def test_block_neutral_diag_entry(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_neutral_diag_entry=True))
    result = pipeline.dispatch(_env_signal("做多", "trading_range", "neutral"))
    assert result.gate_blocked and "neutral_diag_block" in result.gate_reasons[0]
    assert exec_calls == []
