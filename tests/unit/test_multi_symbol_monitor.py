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
    _frame_atr_pct,
    _signal_notification_allowed,
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
    # 预置一个手动白名单条目: 白名单必须与监控品种一致(替换式同步)
    settings.binance_usdm_testnet.symbol_whitelist = ["XAUUSD"]
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        discover=lambda: ["BTCUSDT", "ETHUSDT"],
    )

    monitor._apply_discovered()

    assert settings.binance_usdm_testnet.symbol_whitelist == ["BTCUSDT", "ETHUSDT"]


def test_static_mode_whitelist_matches_targets(tmp_path: Path) -> None:
    """Static-targets mode: whitelist becomes the monitor set on start."""
    settings = _settings(
        MonitorTarget(symbol="XAUUSD", timeframe="15m"),
        MonitorTarget(symbol="BTCUSDT", timeframe="30m"),
    )
    settings.binance_usdm_testnet.symbol_whitelist = ["ETHUSDT"]
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        analyze=lambda _frame, **_kw: None,
    )

    monitor.start()
    try:
        assert settings.binance_usdm_testnet.symbol_whitelist == ["XAUUSD", "BTCUSDT"]
    finally:
        monitor.stop()


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
        source_factory=lambda _kind: FakeSource(_bars(1_800_900)),
        clock=lambda: 1_801_800.0,
        analyze=lambda frame, **kw: calls.append((frame, kw)) or None,
    )
    state = next(iter(monitor._states.values()))
    state.previous_record = {"stage1_diagnosis": {"direction": "bullish"}}
    state.last_processed_closed_ts = 1_800_000_000

    monitor._poll_and_analyze(state)

    assert len(calls) == 1
    kw = calls[0][1]
    # 15m bar: prev close 1_800_900_000, now 1_801_800_000 → exactly 1 new bar.
    assert kw["previous_record"] == state.previous_record
    assert kw["incremental_new_bar_count"] == 1
    assert "record_sink" in kw


def test_monitor_drops_stale_record_after_multibar_gap(tmp_path: Path) -> None:
    """After a multi-bar gap the stale record is dropped for a full pipeline.

    Scenarios such as a rate-limit pause or a long outage span >= 2 closed
    bars; incremental analysis on an outdated previous_record would produce
    low-quality signals, so fall back to the full pipeline instead.
    """
    settings = _settings(MonitorTarget(symbol="XAUUSD", timeframe="15m"))
    calls: list[tuple[object, dict]] = []
    monitor = MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_805)),
        clock=lambda: 1_805_000.0,  # prev close + 5 根 15m bar
        analyze=lambda frame, **kw: calls.append((frame, kw)) or None,
    )
    state = next(iter(monitor._states.values()))
    state.previous_record = {"stage1_diagnosis": {"direction": "bullish"}}
    state.last_processed_closed_ts = 1_800_000_000

    monitor._poll_and_analyze(state)

    assert len(calls) == 1
    kw = calls[0][1]
    assert "previous_record" not in kw
    assert "incremental_new_bar_count" not in kw
    assert state.previous_record is None, "stale record must be dropped"
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

    ret = monitor._save_order_opportunity(
        _order_frame(), _order_decision(), _order_decision()["decision"], _record_double()
    )

    assert ret is not None and ret.status == "dry_run"
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


def test_save_order_opportunity_lifts_stop_to_atr_floor_before_record(
    tmp_path: Path, monkeypatch
) -> None:
    """Plan B: 限价单止损低于 ATR 动态下限时, 落盘/执行前自动抬升止损,
    消除"记录可出、执行被 Stop loss too close to entry 拒"断层。"""
    settings = _settings(MonitorTarget(symbol="XRPUSDT", timeframe="30m"))
    cfg = settings.binance_usdm_testnet
    cfg.enabled = True
    cfg.dry_run = False
    cfg.emergency_stop = False
    cfg.symbol = "XRPUSDT"
    cfg.symbol_whitelist = ["XRPUSDT"]
    cfg.limit_order_enabled = True
    cfg.min_stop_mode = "atr"
    cfg.min_stop_distance_pct = 0.2
    cfg.min_stop_atr_multiple = 0.7

    class _Bar:
        def __init__(self, close: float) -> None:
            self.open = close
            self.high = close
            self.low = close
            self.close = close

    frame = type("Frame", (), {
        "symbol": "XRPUSDT",
        "timeframe": "30m",
        "bars": (_Bar(1.4229), _Bar(1.4213), _Bar(1.4188)),
        "indicators": type("Ind", (), {"atr14": (0.00866, 0.009)})(),
    })()
    decision = {
        "decision": {
            "order_type": "限价单",
            "order_direction": "做空",
            "trade_confidence": 56,
            "entry_price": 1.4213,
            "stop_loss_price": 1.4267,
            "take_profit_price": 1.4105,
            "take_profit_price_2": 1.3903,
            "estimated_win_rate": 53,
        }
    }
    calls: list[dict] = []
    recorded: list[dict] = []

    def fake_execute(inner, cfg, *, analysis_symbol=""):
        calls.append(inner)
        return type("Result", (), {"status": "submitted", "symbol": "XRPUSDT", "reason": "ok"})()

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
        analyze=lambda _frame, **_kw: decision,
    )

    ret = monitor._save_order_opportunity(
        frame, decision, decision["decision"], _record_double()
    )

    assert ret is not None and ret.status == "submitted"
    assert recorded and recorded[0]["decision_inner"]["stop_loss_price"] == 1.4274
    assert calls and calls[0]["stop_loss_price"] == 1.4274


def test_frame_atr_pct_converts_latest_atr_to_percent() -> None:
    frame = type("Frame", (), {
        "indicators": type("Ind", (), {"atr14": (1.5, 2.0)})(),
        "bars": (type("Bar", (), {"close": 100})(),),
    })()
    assert _frame_atr_pct(frame) == 1.5


def test_frame_atr_pct_returns_none_when_unavailable() -> None:
    assert _frame_atr_pct(type("Frame", (), {})()) is None
    no_ind = type("Frame", (), {"bars": (object(),)})()
    assert _frame_atr_pct(no_ind) is None
    nan_frame = type("Frame", (), {
        "indicators": type("Ind", (), {"atr14": (float("nan"),)})(),
        "bars": (type("Bar", (), {"close": 100})(),),
    })()
    assert _frame_atr_pct(nan_frame) is None
    zero_close = type("Frame", (), {
        "indicators": type("Ind", (), {"atr14": (2.0,)})(),
        "bars": (type("Bar", (), {"close": 0})(),),
    })()
    assert _frame_atr_pct(zero_close) is None


class _Res:
    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason


def test_signal_notification_allowed_only_suppresses_rejected() -> None:
    assert _signal_notification_allowed(None) is True
    for ok in ("submitted", "pending", "dry_run", "skipped", "failed"):
        assert _signal_notification_allowed(_Res(ok)) is True, ok
    assert _signal_notification_allowed(_Res("rejected")) is False


def _notify_monitor(tmp_path: Path) -> MultiSymbolMonitor:
    settings = _settings(MonitorTarget(symbol="BTCUSDT", timeframe="15m"))
    return MultiSymbolMonitor(
        ctx=object(),
        settings=settings,
        state_path=tmp_path / "state.json",
        source_factory=lambda _kind: FakeSource(_bars(1_800)),
        clock=lambda: 1_805,
        analyze=lambda _frame, **_kw: _order_decision(),
    )


def test_rejected_execution_skips_order_signal_push(tmp_path: Path, monkeypatch) -> None:
    """下单检验被拒(rejected)时不再推信号通知(含 telegram)。"""
    calls: list[str] = []
    for mod in ("telegram_notifier", "feishu_notifier", "pushplus_notifier"):
        monkeypatch.setattr(
            "pa_agent.notify.%s.send_order_signal" % mod,
            lambda _m=mod, **_kw: calls.append(_m) or True,
        )
    monitor = _notify_monitor(tmp_path)
    out = monitor._notify_order_signal(
        _order_frame(), _order_decision(), _order_decision()["decision"],
        _Res("rejected", "Stop loss too close to entry (0.2% < 0.4% minimum)"),
    )
    assert out == _order_decision()
    assert calls == [], "rejected 的信号不得推送任何渠道"


def test_successful_execution_still_pushes_order_signal(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    for mod in ("telegram_notifier", "feishu_notifier", "pushplus_notifier"):
        monkeypatch.setattr(
            "pa_agent.notify.%s.send_order_signal" % mod,
            lambda _m=mod, **_kw: calls.append(_m) or True,
        )
    monitor = _notify_monitor(tmp_path)
    monitor._notify_order_signal(
        _order_frame(), _order_decision(), _order_decision()["decision"], _Res("submitted")
    )
    assert sorted(calls) == ["feishu_notifier", "pushplus_notifier", "telegram_notifier"]
