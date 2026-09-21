"""Tests for consecutive AI-failure alerting in the headless monitor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.monitoring import service as service_mod
from pa_agent.monitoring.ai_failure_alert import (
    AnalysisFailureAlerter,
    classify_failure,
    format_failure_alert,
    format_recovery_alert,
)

AUTH_MSG = "Error code: 401 - unauthorized: 代理密钥缺失或错误 (auth_error)"


def _record(exc: object = None) -> SimpleNamespace:
    return SimpleNamespace(exception=exc)


def _network_record(stage: str = "stage1", message: str = AUTH_MSG) -> SimpleNamespace:
    return _record({"type": "network_error", "stage": stage, "message": message})


def test_classify_failure_separates_auth_from_transport() -> None:
    assert classify_failure(AUTH_MSG) == "auth"
    assert classify_failure("Error code: 429 rate limit") == "network"
    assert classify_failure("Connection reset by peer") == "network"
    assert classify_failure("") == "network"


def test_alert_copy_names_stage_count_and_next_look() -> None:
    auth = format_failure_alert(kind="auth", count=5, stage="stage1", message=AUTH_MSG)
    assert "连续失败 5 次" in auth
    assert "阶段一" in auth and "密钥/鉴权" in auth
    assert "provider.api_key" in auth
    net = format_failure_alert(kind="network", count=5, stage="stage2", message="boom")
    assert "阶段二" in net and "网络" in net
    assert "已恢复" in format_recovery_alert(count=5, kind="auth")


def test_silent_until_threshold() -> None:
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=5, repeat_every=20)
    for _ in range(4):
        alerter.note_failure(stage="stage1", message=AUTH_MSG)
    assert sent == []
    assert alerter.streak == 4


def test_full_poll_wipeout_alerts_once_then_repeats_every_20() -> None:
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=5, repeat_every=20)
    for _ in range(5):  # 5 品种一轮全灭
        alerter.note_failure(stage="stage1", message=AUTH_MSG)
    assert len(sent) == 1
    assert "连续失败 5 次" in sent[0]
    for _ in range(19):  # 第 6..24 次: 节流静默
        alerter.note_failure(stage="stage1", message=AUTH_MSG)
    assert len(sent) == 1
    alerter.note_failure(stage="stage1", message=AUTH_MSG)  # 第 25 次 = 5 + 20
    assert len(sent) == 2
    assert "连续失败 25 次" in sent[1]


def test_recovery_notice_only_after_a_real_alert() -> None:
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=3)
    alerter.note_failure(message="boom")
    alerter.note_success()  # 没打扰过用户 -> 不补恢复通知
    assert sent == []
    assert alerter.streak == 0

    for _ in range(3):
        alerter.note_failure(message=AUTH_MSG)
    assert len(sent) == 1
    alerter.note_success()
    assert len(sent) == 2
    assert "已恢复" in sent[1] and "连续失败 3 次" in sent[1]
    assert alerter.streak == 0


def test_note_record_reads_the_monitor_network_error_shape() -> None:
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=2)
    alerter.note_record(_network_record())
    alerter.note_record(_network_record(stage="stage2"))
    assert len(sent) == 1
    assert "阶段二" in sent[0]


def test_note_record_counts_a_planless_analysis_as_health() -> None:
    """跑完但没方案是正常业务结果, 不该累计失败, 也不该误报恢复。"""
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=2)
    alerter.note_record(_network_record())
    alerter.note_record(_record(None))
    assert sent == []
    assert alerter.streak == 0


def test_note_record_ignores_a_missing_record() -> None:
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=1)
    alerter.note_record(None)
    assert sent == []
    assert alerter.streak == 0


def test_notify_failure_never_breaks_the_analysis_loop() -> None:
    def boom(_text: str) -> None:
        raise RuntimeError("telegram down")

    alerter = AnalysisFailureAlerter(boom, alert_after=1)
    alerter.note_failure(message=AUTH_MSG)  # 不抛
    alerter.note_success()


class _FakeOrchestrator:
    """替身: submit 直接回一条网络失败记录, 形状与 two_stage 落盘的一致。"""

    def __init__(self, **_kwargs: object) -> None:
        pass

    def submit(self, _frame: object, *_args: object, **_kwargs: object) -> SimpleNamespace:
        exc = {"type": "network_error", "stage": "stage1", "message": AUTH_MSG}
        return SimpleNamespace(exception=exc, stage2_decision=None)


def _bare_monitor(alerter: AnalysisFailureAlerter) -> object:
    monitor = object.__new__(service_mod.MultiSymbolMonitor)
    monitor._ctx = SimpleNamespace(
        client=None,
        assembler=None,
        router=None,
        validator=None,
        pending_writer=None,
        exp_reader=None,
    )
    monitor._settings = SimpleNamespace()
    monitor._ai_failure_alerter = alerter
    monitor._key_text = lambda _key: "BTCUSDT::30m"
    monitor._light_mode_judge = lambda *_a, **_k: False
    monitor._evaluate_structure_exit = lambda *_a, **_k: None
    return monitor


def test_analyze_and_notify_feeds_the_alerter(monkeypatch: pytest.MonkeyPatch) -> None:
    """监控主路径(默认 analyze)必须把 AI 失败喂给告警器。"""
    from pa_agent.orchestrator import two_stage as two_stage_mod

    monkeypatch.setattr(two_stage_mod, "TwoStageOrchestrator", _FakeOrchestrator)
    sent: list[str] = []
    alerter = AnalysisFailureAlerter(sent.append, alert_after=1)
    monitor = _bare_monitor(alerter)
    frame = SimpleNamespace(symbol="BTCUSDT", timeframe="30m")

    result = monitor._analyze_and_notify(frame)

    assert result is None
    assert len(sent) == 1
    assert "连续失败" in sent[0]
