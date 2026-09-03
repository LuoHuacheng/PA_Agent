"""Tests for settings-driven close-of-bar multi-symbol monitoring."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pa_agent.config.settings import MonitorTarget, Settings
from pa_agent.data.base import DataSource, KlineBar
from pa_agent.monitoring.cli import format_decision_result
from pa_agent.monitoring.service import (
    MultiSymbolMonitor,
    _default_validate_symbols,
    next_poll_at,
    timeframe_seconds,
)


class FakeSource(DataSource):
    def __init__(self, bars: list[KlineBar]) -> None:
        self.bars = bars
        self.calls: list[tuple] = []

    def connect(self) -> None:
        self.calls.append(("connect",))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    def list_symbols(self) -> list[str]:
        return []

    def supported_timeframes(self) -> list[str]:
        return ["15m", "30m"]

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.calls.append(("subscribe", symbol, timeframe))

    def unsubscribe(self) -> None:
        self.calls.append(("unsubscribe",))

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        self.calls.append(("latest_snapshot", n))
        return self.bars


def _bars(newest_ts: int) -> list[KlineBar]:
    return [
        KlineBar(
            seq=index + 1,
            ts_open=(newest_ts - index * 900) * 1000,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            closed=True,
        )
        for index in range(60)
    ]


def _settings(*targets: MonitorTarget) -> Settings:
    settings = Settings()
    settings.general.analysis_bar_count = 2
    settings.monitoring.enabled = True
    settings.monitoring.targets = list(targets)
    settings.monitoring.poll_lead_seconds = 5
    return settings


def test_timeframe_scheduler_uses_natural_boundaries() -> None:
    assert timeframe_seconds("30m") == 1800
    assert next_poll_at("15m", now=901, lead_seconds=5) == 1805
    assert next_poll_at("30m", now=1801, lead_seconds=5) == 3605


def test_auto_discover_replaces_static_targets_on_apply(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    settings.monitoring.auto_discover.enabled = True
    settings.monitoring.auto_discover.timeframe = "30m"
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: ["BTCUSDT", "ETHUSDT"],
    )

    # 启动时静态 target 已存在
    assert set(monitor._states) == {("XAUUSD", "15m")}

    monitor._apply_discovered()

    # 静态 target 被发现的品种替换，使用 auto_discover.timeframe
    assert set(monitor._states) == {("BTCUSDT", "30m"), ("ETHUSDT", "30m")}
    assert all(s.target.timeframe == "30m" for s in monitor._states.values())


def test_auto_discover_refresh_keeps_common_and_drops_vanished(tmp_path: Path) -> None:
    settings = _settings()
    settings.monitoring.auto_discover.enabled = True
    settings.monitoring.auto_discover.timeframe = "15m"
    results = iter([["BTCUSDT", "ETHUSDT"], ["BTCUSDT", "SOLUSDT"]])
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: next(results),
    )

    monitor._apply_discovered()
    monitor._apply_discovered()

    assert set(monitor._states) == {("BTCUSDT", "15m"), ("SOLUSDT", "15m")}


def test_auto_discover_failure_keeps_existing_targets(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    settings.monitoring.auto_discover.enabled = True

    def discover() -> list[str]:
        raise RuntimeError("binance down")

    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=discover,
    )

    monitor._apply_discovered()

    assert set(monitor._states) == {("XAUUSD", "15m")}


def test_auto_discover_empty_keeps_static_targets(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    settings.monitoring.auto_discover.enabled = True
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: [],
    )

    monitor._apply_discovered()

    assert set(monitor._states) == {("XAUUSD", "15m")}


def test_start_with_auto_discover_and_empty_static_targets(tmp_path: Path) -> None:
    settings = _settings()
    settings.monitoring.auto_discover.enabled = True
    settings.monitoring.auto_discover.timeframe = "15m"
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: ["BTCUSDT"],
        analyze=lambda frame, **_kw: None,
    )

    monitor.start()
    try:
        assert set(monitor._states) == {("BTCUSDT", "15m")}
    finally:
        monitor.stop()


def test_auto_discover_syncs_symbol_whitelist(tmp_path: Path) -> None:
    settings = _settings()
    settings.monitoring.auto_discover.enabled = True
    settings.monitoring.auto_discover.timeframe = "15m"
    # 预置一个手动白名单条目，验证被保留而不是覆盖
    settings.binance_usdm_testnet.symbol_whitelist = ["XAUUSD"]
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: ["BTCUSDT", "ETHUSDT"],
    )

    monitor._apply_discovered()

    assert settings.binance_usdm_testnet.symbol_whitelist == ["XAUUSD", "BTCUSDT", "ETHUSDT"]


def test_auto_discover_drops_symbols_without_kline(tmp_path: Path) -> None:
    settings = _settings()
    settings.monitoring.auto_discover.enabled = True
    settings.monitoring.auto_discover.timeframe = "15m"
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: ["BTCUSDT", "SKRUSDT"],
        # SKRUSDT 无 K 线，被验证器剔除
        validate_symbols=lambda symbols: [s for s in symbols if s != "SKRUSDT"],
    )

    monitor._apply_discovered()

    assert set(monitor._states) == {("BTCUSDT", "15m")}
    # 被剔除的品种也不进入下单白名单
    assert "SKRUSDT" not in settings.binance_usdm_testnet.symbol_whitelist


class _ProbeRecordingSource(FakeSource):
    """TradingView-shaped fake that records exchange/timeout setup calls."""

    def __init__(self, fetchable: set[str]) -> None:
        super().__init__(_bars(1_800))
        self._fetchable = fetchable
        self.setup_calls: list[tuple] = []

    def set_exchange(self, exchange: str) -> None:
        self.setup_calls.append(("set_exchange", exchange))

    def limit_fetch_wait(self, seconds: float) -> None:
        self.setup_calls.append(("limit_fetch_wait", seconds))

    def subscribe(self, symbol: str, timeframe: str) -> None:
        self.setup_calls.append(("subscribe", symbol, timeframe))
        self._symbol = symbol

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if self._symbol not in self._fetchable:
            raise RuntimeError("no data")
        return self.bars


def test_default_validate_probes_binance_only_with_short_timeout(monkeypatch) -> None:
    """Validation must not crawl 7 exchanges: force BINANCE + short wait so
    contracts TradingView does not serve fail fast instead of stalling."""
    settings = Settings()
    settings.general.last_data_source = "tradingview"
    source = _ProbeRecordingSource(fetchable={"BTCUSDT"})
    monkeypatch.setattr("pa_agent.data.factory.create_data_source", lambda _kind, **_kw: source)

    valid = _default_validate_symbols(["BTCUSDT", "SKRUSDT"], settings)

    assert valid == ["BTCUSDT"]
    assert ("set_exchange", "BINANCE") in source.setup_calls
    assert ("limit_fetch_wait", 4.0) in source.setup_calls
    assert ("subscribe", "SKRUSDT", "15m") in source.setup_calls


def test_default_validate_non_tradingview_skips_venue_setup(monkeypatch) -> None:
    settings = Settings()
    settings.general.last_data_source = "akshare"
    source = _ProbeRecordingSource(fetchable={"000001"})
    monkeypatch.setattr("pa_agent.data.factory.create_data_source", lambda _kind, **_kw: source)

    _default_validate_symbols(["000001"], settings)

    assert source.setup_calls == [("subscribe", "000001", "15m")]


def test_monitor_subscribes_each_target_with_a_separate_source(tmp_path: Path) -> None:
    settings = _settings(
        MonitorTarget(symbol="XAUUSD", timeframe="15m"),
        MonitorTarget(symbol="BTCUSDT", timeframe="30m"),
    )
    sources: list[FakeSource] = []

    def factory(_kind: str) -> FakeSource:
        source = FakeSource(_bars(1_800))
        sources.append(source)
        return source

    monitor = MultiSymbolMonitor(
        ctx=object(), settings=settings, state_path=tmp_path / "state.json", source_factory=factory
    )
    for state in monitor._states.values():
        monitor._ensure_source(state)

    assert len(sources) == 2
    assert sources[0] is not sources[1]
    assert ("subscribe", "XAUUSD", "15m") in sources[0].calls
    assert ("subscribe", "BTCUSDT", "30m") in sources[1].calls


def test_monitor_processes_a_closed_bar_once_and_persists_state(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    source = FakeSource(_bars(1_800))
    analyzed: list[object] = []
    path = tmp_path / "state.json"
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=path,
        source_factory=lambda _kind: source,
        clock=lambda: 1_805,
        analyze=lambda frame, **_kw: analyzed.append(frame) or None,
    )
    state = next(iter(monitor._states.values()))

    monitor._poll_and_analyze(state)
    monitor._poll_and_analyze(state)

    assert len(analyzed) == 1
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["last_processed_closed_ts"]["XAUUSD::15m"] == 1_800_000_000


def test_monitor_reuses_previous_record_for_incremental_analysis(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    calls: list[tuple[object, dict]] = []
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_805)),
        clock=lambda: 1_805_000.0,
        analyze=lambda frame, **kw: calls.append((frame, kw)) or None,
    )
    state = next(iter(monitor._states.values()))
    state.previous_record = {"stage1_diagnosis": {"direction": "bullish"}}
    state.last_processed_closed_ts = 1_800_000_000

    monitor._poll_and_analyze(state)

    assert len(calls) == 1
    kw = calls[0][1]
    # 15m bar: prev close 1_800_900_000, now 1_805_000_000 → 5 new bars.
    assert kw["previous_record"] == state.previous_record
    assert kw["incremental_new_bar_count"] == 5
    assert "record_sink" in kw


def test_monitor_without_prior_record_uses_full_pipeline(tmp_path: Path) -> None:
    calls: list[tuple[object, dict]] = []
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=_settings(MonitorTarget(symbol="XAUUSD", timeframe="15m")),
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=lambda: 1_805,
        analyze=lambda frame, **kw: calls.append((frame, kw)) or None,
    )
    monitor._poll_and_analyze(next(iter(monitor._states.values())))
    assert len(calls) == 1
    assert "previous_record" not in calls[0][1]
    assert "incremental_new_bar_count" not in calls[0][1]
    assert "record_sink" in calls[0][1]


def test_monitor_reports_each_completed_decision_to_callback(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    results: list[tuple[str, dict | None]] = []
    decision = {"decision": {"order_type": "观望"}}
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=lambda: 1_805,
        analyze=lambda _frame, **_kw: decision,
        on_result=lambda frame, value: results.append((frame.symbol, value)),
    )

    monitor._poll_and_analyze(next(iter(monitor._states.values())))

    assert results == [("XAUUSD", decision)]


def test_monitor_persists_completed_analysis_when_result_callback_fails(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=lambda: 1_805,
        analyze=lambda _frame, **_kw: {"decision": {"order_type": "观望"}},
        on_result=lambda _frame, _value: (_ for _ in ()).throw(RuntimeError("output failed")),
    )
    state = next(iter(monitor._states.values()))

    monitor._poll_and_analyze(state)

    assert state.last_processed_closed_ts == 1_800_000_000
    assert state.retry_count == 0


def test_terminal_decision_output_includes_only_order_summary() -> None:
    frame = type("Frame", (), {"symbol": "BTCUSDT", "timeframe": "15m"})()
    decision = {
        "decision": {
            "order_type": "限价单",
            "order_direction": "做多",
            "trade_confidence": 90,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 110,
            "take_profit_price_2": 120,
            "estimated_win_rate": "65%",
            "reasoning": "价格回踩支撑后出现放量反弹。",
        },
        "next_cycle_prediction": {"probabilities": {"上涨": 0.7}},
        "internal_trace": "must not be logged",
    }

    result = format_decision_result(frame, decision)

    assert "[决策] BTCUSDT 15m" in result
    assert "TP1=110" in result
    assert "TP2=120" in result
    assert "胜率=65%" in result
    assert "理由=价格回踩支撑后出现放量反弹。" in result
    assert "next_cycle_prediction" not in result
    assert "internal_trace" not in result


def test_monitor_failure_retries_without_blocking_other_target(tmp_path: Path) -> None:
    settings = _settings(
        MonitorTarget(symbol="BAD", timeframe="15m"),
        MonitorTarget(symbol="GOOD", timeframe="15m"),
    )
    sources = {
        "BAD": FakeSource([]),
        "GOOD": FakeSource(_bars(1_800)),
    }
    analyzed: list[str] = []
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: (
            sources.pop("BAD") if "BAD" in sources else sources.pop("GOOD")
        ),
        clock=lambda: 1_805,
        analyze=lambda frame, **_kw: analyzed.append(frame.symbol) or None,
    )
    states = list(monitor._states.values())

    monitor._poll_and_analyze(states[0])
    monitor._poll_and_analyze(states[1])

    assert states[0].retry_count == 1
    assert analyzed == ["GOOD"]


def test_monitor_reports_fetch_failure_and_retry_status(tmp_path: Path) -> None:
    settings = _settings(MonitorTarget(symbol="BAD", timeframe="15m"))
    statuses: list[str] = []
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource([]),
        clock=lambda: 1_805,
        on_status=statuses.append,
    )

    monitor._poll_and_analyze(next(iter(monitor._states.values())))

    assert any("analysis started for BAD 15m" in status for status in statuses)
    assert any("poll failed for BAD 15m" in status for status in statuses)
    assert any("retry 1/3 for BAD 15m" in status for status in statuses)


def test_monitor_respects_configured_analysis_concurrency(tmp_path: Path) -> None:
    settings = _settings(
        MonitorTarget(symbol="XAUUSD", timeframe="15m"),
        MonitorTarget(symbol="BTCUSDT", timeframe="15m"),
    )
    settings.monitoring.max_concurrent_analyses = 1
    sources = [FakeSource(_bars(1_800)), FakeSource(_bars(1_800))]
    started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    lock = threading.Lock()

    def analyze(_frame: object, **kw: object) -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1

    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: sources.pop(),
        clock=lambda: 1_805,
        analyze=analyze,
    )
    monitor._executor = ThreadPoolExecutor(max_workers=1)
    for state in monitor._states.values():
        state.next_poll_at = 0

    assert monitor.run_due_once(now=1_805) == 2
    assert started.wait(timeout=1)
    time.sleep(0.05)
    assert max_active == 1
    release.set()
    monitor._executor.shutdown(wait=True)


def test_monitor_stop_disconnects_source_before_waiting_for_running_analysis(
    tmp_path: Path,
) -> None:
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    source = FakeSource(_bars(1_800))
    started = threading.Event()
    released = threading.Event()

    def analyze(_frame: object, **kw: object) -> None:
        started.set()
        released.wait(timeout=2)

    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: source,
        clock=lambda: 1_805,
        analyze=analyze,
    )
    monitor._executor = ThreadPoolExecutor(max_workers=1)
    state = next(iter(monitor._states.values()))
    state.next_poll_at = 0
    assert monitor.run_due_once(now=1_805) == 1
    assert started.wait(timeout=1)

    threading.Timer(0.05, released.set).start()
    monitor.stop(timeout=1)

    assert ("disconnect",) in source.calls
    assert not monitor._futures


def _order_frame() -> object:
    return type("Frame", (), {"symbol": "BTCUSDT", "timeframe": "15m"})()


def _order_decision() -> dict:
    return {
        "decision": {
            "order_type": "市价单",
            "order_direction": "做多",
            "trade_confidence": 90,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 110,
        }
    }


def _record_double() -> object:
    meta = type(
        "Meta",
        (),
        {"decision_stance": "balanced", "ai_provider": {"model": "test-model"}},
    )()
    return type("Record", (), {"meta": meta, "stage1_diagnosis": {"direction": "up"}})()


def test_monitor_auto_execution_calls_executor_and_logger(tmp_path: Path, monkeypatch) -> None:
    """When enabled, _save_order_opportunity wires through to the executor."""
    settings = _settings(MonitorTarget(symbol="BTCUSDT", timeframe="15m"))
    settings.binance_usdm_testnet.enabled = True
    settings.binance_usdm_testnet.dry_run = True

    calls: list[dict] = []
    recorded: list[dict] = []

    def fake_execute(inner, cfg, *, analysis_symbol=""):
        calls.append({"inner": inner, "analysis_symbol": analysis_symbol})
        return type("Result", (), {"status": "dry_run", "symbol": "BTCUSDT", "reason": "test"})()

    monkeypatch.setattr("pa_agent.trading.binance_usdm_testnet.execute_market_signal", fake_execute)
    monkeypatch.setattr(
        "pa_agent.records.trade_logger.save_trade_record",
        lambda **kw: recorded.append(kw),
    )
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=lambda: 1_805,
        analyze=lambda _frame, **_kw: _order_decision(),
    )

    monitor._save_order_opportunity(
        _order_frame(), _order_decision(), _order_decision()["decision"], _record_double()
    )

    assert calls and calls[0]["analysis_symbol"] == "BTCUSDT"
    assert calls[0]["inner"]["order_type"] == "市价单"
    assert recorded and recorded[0]["meta_symbol"] == "BTCUSDT"
    assert recorded[0]["decision_stance"] == "balanced"
    assert recorded[0]["model_name"] == "test-model"


def test_monitor_auto_execution_disabled_by_default_returns_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    """With binance_usdm_testnet.enabled=False (default), execution is a no-op."""
    settings = _settings(MonitorTarget(symbol="BTCUSDT", timeframe="15m"))
    from pa_agent.trading.binance_usdm_testnet import execute_market_signal

    monkeypatch.setattr(
        "pa_agent.records.trade_logger.save_trade_record",
        lambda **kw: None,
    )
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=lambda: 1_805,
        analyze=lambda _frame, **_kw: _order_decision(),
    )

    result = execute_market_signal(
        _order_decision()["decision"], settings, analysis_symbol="BTCUSDT"
    )

    assert result.status == "skipped"
    assert result.reason == "Binance Testnet automation disabled"
    # The monitor helper itself must not raise when pointing at the real executor.
    monitor._save_order_opportunity(
        _order_frame(), _order_decision(), _order_decision()["decision"], _record_double()
    )
