# ruff: noqa: RUF002 - Chinese docstrings
"""Monitor pauses analysis/pushes while a Binance rate-limit ban is live."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pa_agent.config.settings import MonitorTarget, Settings
from pa_agent.data.base import DataSource, KlineBar
from pa_agent.monitoring.service import MultiSymbolMonitor
from pa_agent.trading.rate_limit import RateLimitBreaker


class FakeSource(DataSource):
    def __init__(self, bars: list[KlineBar]) -> None:
        self.bars = bars

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def list_symbols(self) -> list[str]:
        return []

    def supported_timeframes(self) -> list[str]:
        return ["15m"]

    def subscribe(self, symbol: str, timeframe: str) -> None:
        pass

    def unsubscribe(self) -> None:
        pass

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        return self.bars


def _bars(newest: int) -> list[KlineBar]:
    return [
        KlineBar(
            seq=index + 1,
            ts_open=(newest - index * 900) * 1000,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            closed=True,
        )
        for index in range(60)
    ]


def _settings(target: MonitorTarget | None = None) -> Settings:
    settings = Settings()
    settings.general.analysis_bar_count = 2
    settings.monitoring.enabled = True
    settings.monitoring.targets = [target or MonitorTarget(symbol="XAUUSD", timeframe="15m")]
    settings.monitoring.poll_lead_seconds = 5
    return settings


def _monitor(tmp_path: Path, breaker: RateLimitBreaker, analyzed: list, clock) -> MultiSymbolMonitor:
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=_settings(),
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=clock,
        analyze=lambda frame, **_kw: analyzed.append(frame) or None,
        rate_limiter=breaker,
    )
    monitor._executor = ThreadPoolExecutor(max_workers=1)
    for state in monitor._states.values():
        state.next_poll_at = 0
    return monitor


def test_pause_skips_every_poll_while_banned(tmp_path: Path) -> None:
    """限流中 run_due_once 不调度任何品种，analyze 零调用。"""
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=2_000_000_000)
    analyzed: list = []
    monitor = _monitor(tmp_path, breaker, analyzed, clock=lambda: 1_000_000.0)
    try:
        assert monitor.run_due_once(now=1_000_000.0) == 0
        assert analyzed == []
    finally:
        monitor._executor.shutdown(wait=False)


def test_resume_schedules_after_ban_expires(tmp_path: Path) -> None:
    """封禁到期后 run_due_once 恢复正常调度。"""
    breaker = RateLimitBreaker()
    analyzed: list = []
    monitor = _monitor(tmp_path, breaker, analyzed, clock=lambda: 1_000_000.0)
    try:
        breaker.record_ban(until_ms=2_000_000_000)
        assert monitor.run_due_once(now=1_000_000.0) == 0
        breaker.clear()  # 时间走过封禁窗口
        assert monitor.run_due_once(now=1_000_000.0) == 1
        monitor._executor.shutdown(wait=True)
        assert len(analyzed) == 1
    finally:
        monitor._executor.shutdown(wait=False)


def test_pause_disabled_by_setting_ignores_ban(tmp_path: Path) -> None:
    """pause_monitoring_on_rate_limit=False 时限流不再暂停分析。"""
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=2_000_000_000)
    analyzed: list = []
    monitor = _monitor(tmp_path, breaker, analyzed, clock=lambda: 1_000_000.0)
    monitor._settings.binance_usdm_testnet.pause_monitoring_on_rate_limit = False
    try:
        assert monitor.run_due_once(now=1_000_000.0) == 1
        monitor._executor.shutdown(wait=True)
        assert len(analyzed) == 1
    finally:
        monitor._executor.shutdown(wait=False)


def test_pause_and_resume_announce_transition_once(tmp_path: Path, monkeypatch) -> None:
    """暂停/恢复各只通知一次（on_status + Telegram），不重复刷屏。"""
    breaker = RateLimitBreaker()
    statuses: list[str] = []
    telegrams: list[str] = []

    def fake_telegram(*, text: str, settings=None) -> bool:
        telegrams.append(text)
        return True

    monkeypatch.setattr(
        "pa_agent.notify.telegram_notifier.send_telegram_message", fake_telegram
    )
    monitor = _monitor(tmp_path, breaker, [], clock=lambda: 1_000_000.0)
    monitor._on_status = statuses.append
    try:
        # 暂停开始
        breaker.record_ban(until_ms=1_000_300_000)
        assert monitor.run_due_once(now=1_000_000.0) == 0
        assert monitor.run_due_once(now=1_000_000.0) == 0  # 第二次不重复通知
        assert len(telegrams) == 1
        assert "暂停" in telegrams[0]
        assert any("暂停" in s for s in statuses)
        # 恢复
        breaker.clear()
        monitor.run_due_once(now=1_000_400.0)
        monitor._executor.shutdown(wait=True)
        assert len(telegrams) == 2
        assert "恢复" in telegrams[1]
        assert any("恢复" in s for s in statuses)
    finally:
        monitor._executor.shutdown(wait=False)


def test_gap_over_two_bars_drops_incremental_context(tmp_path: Path) -> None:
    """恢复后若跨 >=2 根 bar，previous_record 清空走全量。"""
    calls: list[dict] = []
    settings = _settings()
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_805)),
        clock=lambda: 1_805_000.0,
        analyze=lambda frame, **kw: calls.append(kw) or None,
        rate_limiter=RateLimitBreaker(),
    )
    state = next(iter(monitor._states.values()))
    state.previous_record = {"stage1_diagnosis": {"direction": "bullish"}}
    state.last_processed_closed_ts = 1_800_000_000
    monitor._poll_and_analyze(state)
    assert calls and "previous_record" not in calls[0]
    assert calls and "incremental_new_bar_count" not in calls[0]
    assert state.previous_record is None


def test_single_bar_gap_keeps_incremental_context(tmp_path: Path) -> None:
    """仅跨 1 根 bar（正常连续监控）时继续增量分析。"""
    calls: list[dict] = []
    settings = _settings()
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800_900)),
        clock=lambda: 1_801_800.0,
        analyze=lambda frame, **kw: calls.append(kw) or None,
        rate_limiter=RateLimitBreaker(),
    )
    state = next(iter(monitor._states.values()))
    state.previous_record = {"stage1_diagnosis": {"direction": "bullish"}}
    state.last_processed_closed_ts = 1_800_000_000
    monitor._poll_and_analyze(state)
    assert calls and calls[0]["previous_record"] is state.previous_record
    assert calls and calls[0]["incremental_new_bar_count"] == 1

