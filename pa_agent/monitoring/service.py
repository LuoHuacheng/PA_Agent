# ruff: noqa: RUF001, RUF002, RUF003 - Chinese product copy
"""Settings-driven multi-symbol monitoring at K-line close boundaries.

The service deliberately does not share the GUI's single-subscription data
source. Each configured target owns one source instance of the configured
source *kind*, so subscriptions cannot overwrite one another.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pa_agent.config.settings import MonitorTarget, Settings
from pa_agent.data.base import DataSource
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, build_analysis_frame

logger = logging.getLogger(__name__)

# Auto-discovery pulls Binance USDⓈ-M perpetuals; validation probes only the
# BINANCE venue with a short websocket budget so contracts TradingView does
# not serve fail fast instead of stalling the full timeout per exchange.
_TV_VALIDATION_EXCHANGE = "BINANCE"
_TV_VALIDATION_WS_TIMEOUT_S = 4.0

_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "45m": 2700,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def timeframe_seconds(timeframe: str) -> int:
    """Return the natural boundary period for a supported K-line timeframe."""
    normalized = timeframe.strip().lower()
    if normalized not in _TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported monitoring timeframe: {timeframe}")
    return _TIMEFRAME_SECONDS[normalized]


def next_poll_at(timeframe: str, *, now: float, lead_seconds: int) -> float:
    """First poll time after the next natural bar close."""
    period = timeframe_seconds(timeframe)
    close_at = (int(now) // period + 1) * period
    return float(close_at + lead_seconds)


def _default_discover(cfg: Any) -> list[str]:
    """Production auto-discovery: top USDⓈ-M contracts from the live 24h feed."""
    from pa_agent.monitoring.discovery import fetch_usdm_top_n

    return fetch_usdm_top_n(
        rank_by=cfg.rank_by,
        top_n=cfg.top_n,
        stablecoin_only=cfg.stablecoin_only,
    )


def _default_validate_symbols(symbols: list[str], settings: Settings) -> list[str]:
    """Keep symbols that return at least one K-line bar from the data source.

    Uses the configured data source kind and a short snapshot; any symbol that
    raises or yields no bars is dropped (e.g. new contracts without history on
    TradingView). Returns the subset of *symbols* that is actually fetchable.
    """
    if not symbols:
        return []
    from pa_agent.data.factory import create_data_source

    source = create_data_source(settings.general.last_data_source, settings=settings)
    valid: list[str] = []
    try:
        source.connect()
        if settings.general.last_data_source == "tradingview":
            # Auto-discovery feeds Binance USDⓈ-M perpetuals, so probe only the
            # BINANCE venue. Without an explicit exchange the fetch path runs
            # the multi-venue auto-probe crawl (up to 7 exchanges serially); a
            # contract TradingView does not serve then stalls a full websocket
            # timeout on each venue, freezing the scheduler thread for minutes.
            set_exchange = getattr(source, "set_exchange", None)
            if callable(set_exchange):
                set_exchange(_TV_VALIDATION_EXCHANGE)
            # Existence check: a contract that answers within this budget yields
            # bars (healthy fetches complete in ~1s); the rest fail fast instead
            # of waiting out the default 10s timeout per venue.
            limit_wait = getattr(source, "limit_fetch_wait", None)
            if callable(limit_wait):
                limit_wait(_TV_VALIDATION_WS_TIMEOUT_S)
        for symbol in symbols:
            try:
                source.subscribe(symbol, settings.monitoring.auto_discover.timeframe)
                bars = source.latest_snapshot(5)
                if bars:
                    valid.append(symbol)
            except Exception:  # noqa: BLE001 - per-symbol validation failures
                logger.debug("Auto-discovery validation failed for %s", symbol, exc_info=True)
    finally:
        try:
            source.disconnect()
        except Exception:
            logger.debug("Auto-discovery validation source disconnect failed", exc_info=True)
    return valid


@dataclass
class _TargetState:
    target: MonitorTarget
    source: DataSource | None = None
    next_poll_at: float = 0.0
    last_processed_closed_ts: int | None = None
    retry_count: int = 0
    running: bool = False
    last_error: str = ""
    #: Last AnalysisRecord, reused as the incremental Stage-1 base on next poll.
    previous_record: Any = None


class MultiSymbolMonitor:
    """Poll configured targets only after their respective K-line closes.

    ``analyze`` is injectable to make scheduling testable. The production
    default executes the existing two-stage pipeline then sends notifications.
    """

    def __init__(
        self,
        *,
        ctx: Any,
        settings: Settings,
        state_path: Path,
        source_factory: Callable[[str], DataSource] | None = None,
        discover: Callable[[], list[str]] | None = None,
        validate_symbols: Callable[[list[str]], list[str]] | None = None,
        clock: Callable[[], float] = time.time,
        analyze: Callable[..., dict | None] | None = None,
        on_result: Callable[[Any, dict | None], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        rate_limiter: Any = None,
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._cfg = settings.monitoring
        if rate_limiter is None:
            from pa_agent.trading.rate_limit import rate_limiter as _module_limiter

            rate_limiter = _module_limiter
        self._rate_limiter = rate_limiter
        self._rate_limit_pause_was = False
        self._state_path = state_path
        self._source_factory = source_factory
        self._discover = discover
        self._validate_symbols = validate_symbols
        self._clock = clock
        self._analyze = analyze or self._analyze_and_notify
        self._on_result = on_result
        self._on_status = on_status
        self._states: dict[tuple[str, str], _TargetState] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._futures: set[Future[Any]] = set()
        self._lock = threading.Lock()
        self._next_refresh_at: float | None = None
        #: Per-symbol structure-failure negation streak (dir + consecutive bars).
        self._structure_exit_streak: dict[str, dict[str, object]] = {}
        #: Per-symbol daily-trend line, cached by local day: (yyyymmdd, text).
        self._daily_trend_cache: dict[str, tuple[str, str]] = {}
        #: C3 light-mode: quiet-bar counter per symbol::timeframe key.
        self._light_quiet: dict[str, int] = {}
        #: Direction-gate rejection counters keyed by gate name (G1/G2...).
        self._direction_gate_stats: dict[str, int] = {}
        from pa_agent.trading.signal_pipeline import SignalPipeline

        #: 统一开仓管线：机会判定/方向闸门/落盘/执行/通知只在这一处。
        self._signal_pipeline = SignalPipeline(
            settings, on_gate_hit=self._bump_direction_gate_stats
        )
        self._load_state()

        for target in self._cfg.targets:
            if target.enabled:
                self._add_target_state(target)

    def _add_target_state(self, target: MonitorTarget) -> None:
        key = (target.symbol, target.timeframe)
        self._states[key] = _TargetState(
            target=target,
            next_poll_at=next_poll_at(
                target.timeframe,
                now=self._clock(),
                lead_seconds=self._cfg.poll_lead_seconds,
            ),
            last_processed_closed_ts=self._persisted_closed_ts.get(self._key_text(key)),
        )

    @property
    def _binance_cfg(self) -> Any:
        """Active execution section: live when environment=live, else testnet."""
        from pa_agent.trading.binance_env import active_cfg

        return active_cfg(self._settings)

    @property
    def _binance_env(self) -> Any:
        """Profile of the declared execution environment (gateways/labels)."""
        from pa_agent.trading.binance_env import resolve_env

        return resolve_env(self._settings)

    def _sync_whitelist_to_active(self, symbols: list[str]) -> None:
        """Make the execution whitelist exactly match the active monitor set.

        白名单与监控名单保持一致：监控外的币种只会收到信号推送、绝不自动
        执行（用户要求两名单一致，替换式同步，不再保留手动条目）。
        """
        auto_cfg = self._binance_cfg
        synced = list(dict.fromkeys(symbols))
        if auto_cfg.symbol_whitelist != synced:
            auto_cfg.symbol_whitelist = synced
            self._report(
                f"symbol_whitelist 已同步监控品种: {len(synced)} 个 "
                f"({', '.join(synced)})"
            )

    def _discover_targets(self) -> list[MonitorTarget]:
        """Run auto-discovery, falling back to the static list on failure."""
        if not self._cfg.auto_discover.enabled:
            return []
        try:
            discover = self._discover or (lambda: _default_discover(self._cfg.auto_discover))
            symbols = discover()
            if not symbols:
                self._report(
                    "自动发现未返回品种，保留静态 targets", level=logging.WARNING
                )
                return []
            self._report(
                f"自动发现 {len(symbols)} 个品种 (rank_by="
                f"{self._cfg.auto_discover.rank_by}, top_n={len(symbols)})"
            )
            tf = self._cfg.auto_discover.timeframe
            return [
                MonitorTarget(symbol=symbol, timeframe=tf, enabled=True)
                for symbol in symbols
            ]
        except Exception as exc:  # noqa: BLE001 - fall back to static targets
            self._report(
                f"自动发现失败（{exc}），保留静态 targets", level=logging.WARNING
            )
            return []

    def _apply_discovered(self) -> None:
        """Replace monitored states with auto-discovered ones; drop vanished."""
        targets = self._discover_targets()
        if not targets:
            return
        symbols = [target.symbol for target in targets]
        # 剔除 TradingView 无 K 线的品种（如新上架合约无历史数据）。
        if self._validate_symbols is not None:
            valid = self._validate_symbols(symbols)
            dropped = [s for s in symbols if s not in valid]
            if dropped:
                self._report(
                    f"自动发现剔除 {len(dropped)} 个 TradingView 无数据品种: "
                    + ", ".join(dropped),
                    level=logging.WARNING,
                )
            symbols = valid
            targets = [
                target for target in targets if target.symbol in symbols
            ]
        if not targets:
            return
        # 执行白名单与监控品种保持一致（替换式同步）：监控外的品种不自动执行。
        self._sync_whitelist_to_active(symbols)
        with self._lock:
            new_states: dict[tuple[str, str], _TargetState] = {}
            for target in targets:
                key = (target.symbol, target.timeframe)
                existing = self._states.get(key)
                new_states[key] = existing if existing is not None else _TargetState(
                    target=target,
                    next_poll_at=next_poll_at(
                        target.timeframe,
                        now=self._clock(),
                        lead_seconds=self._cfg.poll_lead_seconds,
                    ),
                    last_processed_closed_ts=self._persisted_closed_ts.get(self._key_text(key)),
                )
            for key, state in list(self._states.items()):
                if key in new_states:
                    continue
                if state.source is not None:
                    try:
                        state.source.disconnect()
                    except Exception:
                        logger.debug("Monitor source disconnect failed", exc_info=True)
                self._states.pop(key)
            self._states.update(new_states)
        self._report(
            f"自动发现刷新完成，当前监控 {len(self._states)} 个品种: "
            + ", ".join(sorted(f"{k[0]} {k[1]}" for k in self._states))
        )

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self._cfg.enabled or self.is_running:
            return
        # Auto-discovery 启用时先拉取品种，避免静态 targets 为空导致不启动。
        if self._cfg.auto_discover.enabled:
            self._apply_discovered()
        else:
            # 静态 targets 模式同样保持白名单 = 监控品种。
            self._sync_whitelist_to_active(
                [state.target.symbol for state in self._states.values()]
            )
        if not self._states:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._cfg.max_concurrent_analyses,
            thread_name_prefix="symbol-monitor",
        )
        self._thread = threading.Thread(
            target=self._run, name="symbol-monitor-scheduler", daemon=True
        )
        self._thread.start()
        self._report(
            f"Started multi-symbol monitor for {len(self._states)} target(s), "
            f"max_concurrent_analyses={self._cfg.max_concurrent_analyses}"
        )
        self._report_targets()

    def _report_targets(self) -> None:
        targets = ", ".join(
            f"{state.target.symbol} {state.target.timeframe}"
            f" (next poll {time.strftime('%H:%M:%S', time.localtime(state.next_poll_at))})"
            for state in self._states.values()
        )
        self._report(f"当前监控品种: {targets}")

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        # Disconnect before waiting for executor threads. TradingView's blocking
        # socket read is released by disconnect(), allowing the worker to finish.
        for state in self._states.values():
            if state.source is not None:
                try:
                    state.source.disconnect()
                except Exception:
                    logger.debug("Monitor source disconnect failed", exc_info=True)
        deadline = time.monotonic() + max(0.0, timeout)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        while self._futures and time.monotonic() < deadline:
            time.sleep(0.01)
        self._save_state()
        return not self._futures

    def _update_rate_limit_pause(self, now: float) -> bool:
        """Observe the Binance rate-limit breaker; announce transitions once.

        Returns True while analysis/pushes must stay paused. Announcements
        fire only on the pause/resume edges so a long ban does not spam.
        """
        cfg = self._binance_cfg
        limiter = self._rate_limiter
        enabled = bool(getattr(cfg, "pause_monitoring_on_rate_limit", False))
        if not enabled or limiter is None:
            self._rate_limit_pause_was = False
            return False
        paused = bool(limiter.is_banned(now_ms=int(now * 1000)))
        if paused == self._rate_limit_pause_was:
            return paused
        self._rate_limit_pause_was = paused
        try:
            if paused:
                until_ms = int(limiter.banned_until_ms() or (now * 1000))
                when = time.strftime("%H:%M:%S", time.localtime(until_ms / 1000.0))
                self._report(
                    f"Binance 限流中：暂停行情分析与信号推送，预计 {when} 自动恢复",
                    level=logging.WARNING,
                )
                self._announce_rate_limit_change(
                    "已暂停",
                f"检测到 Binance {self._binance_env.label_en} 限流(HTTP 418/-1003)，预计 {when} 自动恢复"
                )
            else:
                self._report("Binance 限流解除：恢复行情监控（下一根 K 线收盘起）")
                self._announce_rate_limit_change(
            "已恢复", f"Binance {self._binance_env.label_en} 限流解除，下一根 K 线收盘起生效"
        )
        except Exception:  # announcements are best-effort
            logger.exception("Rate-limit transition announcement failed")
        return paused

    def _announce_rate_limit_change(self, verb: str, detail: str) -> None:
        """Push one Telegram notice per pause/resume edge (best-effort)."""
        from pa_agent.notify.telegram_notifier import send_telegram_message

        text = (
            f"ℹ️ PA Agent 监控{verb}：{detail}。"
            "暂停期间不产生分析、不推送信号，已错过的 K 线收盘不追溯补发。"
        )
        send_telegram_message(text=text, settings=self._settings)

    def run_due_once(self, now: float | None = None) -> int:
        """Schedule due targets once. Public primarily for deterministic tests."""
        now = self._clock() if now is None else now
        if self._update_rate_limit_pause(now):
            return 0
        scheduled = 0
        for state in self._states.values():
            if state.running or now < state.next_poll_at or self._executor is None:
                continue
            state.running = True
            future = self._executor.submit(self._poll_and_analyze, state)
            self._futures.add(future)
            future.add_done_callback(lambda done, s=state: self._done(s, done))
            scheduled += 1
        return scheduled

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self.run_due_once()
            if self._discover is not None or self._cfg.auto_discover.enabled:
                now = self._clock()
                interval = self._cfg.auto_discover.refresh_minutes * 60
                if self._next_refresh_at is None:
                    self._next_refresh_at = now + interval
                elif now >= self._next_refresh_at:
                    self._next_refresh_at = now + interval
                    self._apply_discovered()

    def _done(self, state: _TargetState, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)
            state.running = False
        try:
            future.result()
        except Exception:
            logger.exception(
                "Monitor task crashed for %s %s", state.target.symbol, state.target.timeframe
            )

    def _poll_and_analyze(self, state: _TargetState) -> None:
        now = self._clock()
        target = state.target
        try:
            self._report(
                f"Monitor analysis started for {target.symbol} {target.timeframe}; "
                "fetching closed K-line data"
            )
            source = self._ensure_source(state)
            bar_count = int(self._settings.general.analysis_bar_count)
            bars = source.latest_snapshot(bar_count + INDICATOR_WARMUP_BARS + 5)
            frame = build_analysis_frame(
                bars, bar_count, target.symbol, target.timeframe, now_ms=int(now * 1000)
            )
            if frame is None or not frame.bars:
                raise ValueError("insufficient closed bars after K-line close")
            closed_ts = int(frame.bars[0].ts_open)
            if state.last_processed_closed_ts == closed_ts:
                raise ValueError("data source has not published the new closed bar")
            if (
                state.last_processed_closed_ts is not None
                and closed_ts < state.last_processed_closed_ts
            ):
                raise ValueError("data source returned an older closed bar")

            htf_block = self._fetch_htf_context(
                state, target.symbol, target.timeframe
            )
            # 实测最大的亏损来源是逆势单(逆 5-30 天趋势做单, 超额 -22 到 -34
            # 个百分点)。模型的"大背景"窗口只有 20 根分析周期 K 线(30m 下约
            # 10 小时), 与执行层 counter_trend 闸门用的日线尺度差两个数量级,
            # 所以把同源的趋势数字直接喂给它。
            trend_line = self._daily_trend_line(
                state, target.symbol, target.timeframe
            )
            if trend_line:
                htf_block = f"{trend_line}\n{htf_block}" if htf_block else trend_line
            call_kwargs = self._incremental_kwargs(state, now)
            call_kwargs["record_sink"] = state
            if htf_block:
                call_kwargs["htf_block"] = htf_block
            decision = self._analyze(frame, **call_kwargs)
            if self._on_result is not None:
                try:
                    self._on_result(frame, decision)
                except Exception:
                    logger.exception(
                        "Monitor result callback failed for %s %s",
                        target.symbol,
                        target.timeframe,
                    )
            state.last_processed_closed_ts = closed_ts
            self._persisted_closed_ts[self._key_text((target.symbol, target.timeframe))] = closed_ts
            self._save_state()
            state.retry_count = 0
            state.last_error = ""
            self._schedule_next(state, now)
            self._report(
                f"Monitor analysis completed for {target.symbol} {target.timeframe}; next poll "
                f"{time.strftime('%H:%M:%S', time.localtime(state.next_poll_at))}"
            )
        except Exception as exc:
            state.last_error = str(exc)
            state.retry_count += 1
            self._report(
                f"Monitor poll failed for {target.symbol} {target.timeframe}: {exc}",
                level=logging.WARNING,
            )
            if state.retry_count <= self._cfg.poll_retry_attempts:
                state.next_poll_at = now + self._cfg.poll_retry_seconds
                self._report(
                    f"Monitor retry {state.retry_count}/{self._cfg.poll_retry_attempts} for "
                    f"{target.symbol} {target.timeframe} at "
                    f"{time.strftime('%H:%M:%S', time.localtime(state.next_poll_at))}",
                    level=logging.WARNING,
                )
            else:
                state.retry_count = 0
                self._schedule_next(state, now)
                self._report(
                    f"Monitor retries exhausted for {target.symbol} {target.timeframe}; "
                    f"next natural poll "
                    f"{time.strftime('%H:%M:%S', time.localtime(state.next_poll_at))}",
                    level=logging.WARNING,
                )

    def _ensure_source(self, state: _TargetState) -> DataSource:
        if state.source is not None:
            return state.source
        if self._source_factory is None:
            from pa_agent.data.factory import create_data_source

            source = create_data_source(
                self._settings.general.last_data_source, settings=self._settings
            )
        else:
            source = self._source_factory(self._settings.general.last_data_source)
        state.source = source
        try:
            source.connect()
            if self._settings.general.last_data_source == "tradingview":
                set_exchange = getattr(source, "set_exchange", None)
                if callable(set_exchange):
                    set_exchange(self._settings.general.last_tradingview_exchange)
            source.subscribe(state.target.symbol, state.target.timeframe)
            return source
        except Exception:
            state.source = None
            try:
                source.disconnect()
            except Exception:
                logger.debug("Monitor source cleanup failed", exc_info=True)
            raise

    def _schedule_next(self, state: _TargetState, now: float) -> None:
        state.next_poll_at = next_poll_at(
            state.target.timeframe, now=now, lead_seconds=self._cfg.poll_lead_seconds
        )

    def _report(self, message: str, *, level: int = logging.INFO) -> None:
        logger.log(level, message)
        if self._on_status is None:
            return
        try:
            self._on_status(message)
        except Exception:
            logger.exception("Monitor status callback failed")

    def _incremental_kwargs(self, state: _TargetState, now: float) -> dict[str, object]:
        """Return kwargs enabling incremental Stage-1 when a prior record exists.

        Incremental re-analysis reuses the previous Stage-1 prompt chain (K-line
        table stays in the cached prefix) and only asks about the bars closed
        since the last poll. Without a prior record this returns empty, so the
        full two-stage pipeline runs unchanged.
        """
        if state.previous_record is None or state.last_processed_closed_ts is None:
            return {}
        bar_s = timeframe_seconds(state.target.timeframe)
        prev_close_ms = state.last_processed_closed_ts + int(bar_s * 1000)
        new_bars = max(1, round((int(now * 1000) - prev_close_ms) / (bar_s * 1000)))
        if new_bars >= 2:
            # 跨 >=2 根收盘 K 线（限流暂停/停机/断网恢复）：基于过期 previous_record
            # 的增量上下文不可靠，丢弃并回退全量分析。
            state.previous_record = None
            return {}
        if new_bars < 1:
            return {}
        return {
            "previous_record": state.previous_record,
            "incremental_new_bar_count": new_bars,
        }

    def _analyze_and_notify(
        self,
        frame: Any,
        *,
        previous_record: Any = None,
        incremental_new_bar_count: int | None = None,
        record_sink: Any = None,
        htf_block: str = "",
    ) -> dict | None:
        from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
        from pa_agent.util.threading import CancelToken

        orchestrator = TwoStageOrchestrator(
            client=self._ctx.client,
            assembler=self._ctx.assembler,
            router=self._ctx.router,
            validator=self._ctx.validator,
            pending_writer=self._ctx.pending_writer,
            exp_reader=self._ctx.exp_reader,
            settings=self._settings,
        )
        key = self._key_text((frame.symbol, frame.timeframe))
        light_judge = self._light_mode_judge(key, previous_record)
        record = orchestrator.submit(
            frame,
            CancelToken(),
            lambda _event: None,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
            light_skip_judge=light_judge,
            htf_block=htf_block,
        )
        if record_sink is not None and record is not None:
            record_sink.previous_record = record
        self._evaluate_structure_exit(frame, record)
        decision = record.stage2_decision if record is not None else None
        if not isinstance(decision, dict):
            return None
        inner = decision.get("decision") or {}
        # 统一安全管线：置信度机会判定、方向闸门、atr 注入/止损下限抬升、
        # 落盘、执行、失败告警与信号推送全部在 SignalPipeline.dispatch 内。
        # 默认配置 binance_usdm_testnet.enabled=False 时执行层自行跳过。
        meta = getattr(record, "meta", None)
        provider = getattr(meta, "ai_provider", None) or {}
        from pa_agent.trading.signal_pipeline import OrderSignal

        pipeline_signal = OrderSignal(
            decision=decision,
            inner=inner,
            symbol=frame.symbol,
            timeframe=str(getattr(frame, "timeframe", "") or ""),
            frame=frame,
            stage1_diagnosis=getattr(record, "stage1_diagnosis", None),
            previous_record=previous_record,
            decision_stance=str(getattr(meta, "decision_stance", "") or ""),
            model_name=(
                str(provider.get("model") or "") if isinstance(provider, dict) else ""
            ),
        )
        try:
            self._signal_pipeline.dispatch(pipeline_signal)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Monitor trade-record/execution failed for %s %s: %s",
                frame.symbol,
                frame.timeframe,
                exc,
            )
        return decision

    def _bump_direction_gate_stats(self, gates_key: str) -> None:
        self._direction_gate_stats[gates_key] = (
            self._direction_gate_stats.get(gates_key, 0) + 1
        )

    def _fetch_htf_context(self, state: _TargetState, symbol: str, timeframe: str) -> str:
        """D1: fetch higher-timeframe bars and summarize them programmatically.

        Gated by monitoring.htf_context_enabled (default off). The source is
        re-subscribed per timeframe and switched back afterwards; every fetch
        failure degrades to no HTF context, never to a poll error.
        """
        if not getattr(self._cfg, "htf_context_enabled", False):
            return ""
        timeframes = [
            str(t) for t in (getattr(self._cfg, "htf_timeframes", None) or [])
            if str(t) and str(t) != timeframe
        ]
        if not timeframes:
            return ""
        source = state.source
        if source is None:
            return ""
        parts: dict[str, str] = {}
        now = self._clock()
        try:
            from pa_agent.ai.htf_summary import (
                build_htf_context_text,
                summarize_htf,
            )
            from pa_agent.data.snapshot import build_analysis_frame

            bar_count = int(self._settings.general.analysis_bar_count)
            for tf in timeframes:
                try:
                    source.subscribe(symbol, tf)
                    bars = source.latest_snapshot(bar_count + INDICATOR_WARMUP_BARS + 5)
                    htf_frame = build_analysis_frame(
                        bars,
                        bar_count,
                        symbol,
                        tf,
                        now_ms=int(now * 1000),
                    )
                    if htf_frame is not None:
                        parts[tf] = summarize_htf(htf_frame)
                except Exception as exc:  # noqa: BLE001 best-effort HTF fetch
                    logger.warning(
                        "HTF context fetch failed for %s %s: %s", symbol, tf, exc
                    )
        finally:
            try:
                source.subscribe(symbol, timeframe)
            except Exception:  # noqa: BLE001 restore original subscription
                logger.warning(
                    "HTF fetch: re-subscribe failed for %s %s", symbol, timeframe
                )
        return build_htf_context_text(parts)

    def _daily_trend_line(
        self, state: _TargetState, symbol: str, timeframe: str
    ) -> str:
        """One-line daily-trend summary, in the same window as the executor.

        The window comes from binance_usdm_testnet.trend_lookback_days so the model
        and the counter-trend gate judge alignment against the same number; a
        30-day reference is appended for context. Cached per local day: daily
        bars change slowly and re-subscribing the source every bar is wasteful.
        Every failure degrades to no injection, never to a poll error.
        """
        source = getattr(state, "source", None)
        if source is None:
            return ""
        cfg = self._binance_cfg  # 环境感知: live 时读 live 节, 而非写死 testnet 节
        if cfg is None or not getattr(cfg, "enabled", False):
            return ""
        days = int(getattr(cfg, "trend_lookback_days", 7) or 7)
        neutral = float(getattr(cfg, "trend_neutral_band_pct", 3.0) or 3.0)
        today = time.strftime("%Y%m%d")
        cached = self._daily_trend_cache.get(symbol)
        if cached is not None and cached[0] == today:
            return cached[1]
        want = max(days, 30) + 2
        try:
            source.subscribe(symbol, "1d")
            bars = source.latest_snapshot(want + 5)
        except Exception as exc:  # best-effort trend fetch
            logger.warning("Daily trend fetch failed for %s: %s", symbol, exc)
            return ""
        finally:
            try:
                source.subscribe(symbol, timeframe)
            except Exception:  # restore original subscription
                logger.warning(
                    "Daily trend: re-subscribe failed for %s %s", symbol, timeframe
                )
        closes: list[float] = []
        for bar in bars:
            try:
                value = float(bar.close)
            except (AttributeError, TypeError, ValueError):
                continue
            if value > 0:
                closes.append(value)
        if len(closes) < days + 1:
            return ""

        def _pct(back: int) -> float | None:
            if len(closes) <= back or closes[back] <= 0:
                return None
            return (closes[0] - closes[back]) / closes[back] * 100

        def _fmt(value: float | None) -> str:
            return "-" if value is None else f"{value:+.1f}%"

        def _label(value: float | None) -> str:
            if value is None:
                return "-"
            if abs(value) <= neutral:
                return "中性"
            return "偏多" if value > 0 else "偏空"

        main = _pct(days)
        cells = [f"{days}天 {_fmt(main)} {_label(main)}"]
        ref = _pct(30)
        if ref is not None and days != 30:
            cells.append(f"30天 {_fmt(ref)} {_label(ref)}")
        line = (
            "## 日线趋势(程序摘要)\n"
            + " | ".join(cells)
            + "\n该口径与执行层逆势闸门同源; 逆势方向的单需额外谨慎。"
        )
        self._daily_trend_cache[symbol] = (today, line)
        return line

    def _light_mode_judge(self, key: str, previous_record: Any):
        """Return a stage1->reason judge when light mode is enabled (C3).

        Called by the orchestrator after stage 1 completes with the real
        stage-1 JSON. The judge resets on structure events / active plans and
        only returns a skip reason after max_quiet_bars consecutive quiet
        bars; a non-empty reason means the model Stage-2 call is skipped.
        """
        if not getattr(self._cfg, "light_mode_enabled", False):
            return None

        def _judge(stage1_json: dict) -> str:
            from pa_agent.monitoring.light_gate import (
                record_has_active_plan,
                stage1_of_record,
                structure_event_detected,
            )

            if record_has_active_plan(previous_record):
                self._light_quiet[key] = 0
                return ""
            if structure_event_detected(stage1_of_record(previous_record), stage1_json):
                self._light_quiet[key] = 0
                return ""
            max_quiet = int(getattr(self._cfg, "light_mode_max_quiet_bars", 3) or 3)
            self._light_quiet[key] = self._light_quiet.get(key, 0) + 1
            if self._light_quiet[key] < max_quiet:
                return ""
            return f"连续安静 {self._light_quiet[key]} 根无结构事件"

        return _judge

    def _evaluate_structure_exit(self, frame: Any, record: Any) -> None:
        """React to diagnosis negation while a pa-entry position is open.

        Runs after every closed-bar analysis (order or no-order rounds alike).
        Best-effort: any failure only logs and never disturbs the analysis.
        Mode comes from settings.binance_usdm_testnet.structure_exit_mode.
        """
        if record is None:
            return
        cfg = self._binance_cfg
        if not getattr(cfg, "enabled", False):
            return
        mode = str(getattr(cfg, "structure_exit_mode", "off") or "off").strip()
        if mode == "off":
            return
        try:
            from pa_agent.notify.telegram_notifier import send_structure_exit_notice
            from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient
            from pa_agent.trading.structure_exit import evaluate_structure_failure_exit

            client = BinanceUSDMTestnetClient(
                cfg.api_key, cfg.api_secret, base_url=self._binance_env.rest_base
            )
            verdict = evaluate_structure_failure_exit(
                symbol=frame.symbol,
                timeframe=frame.timeframe,
                record=record,
                frame=frame,
                settings=self._settings,
                counts=self._structure_exit_streak,
                client=client,
            )
        except Exception as exc:
            logger.warning(
                "Structure-exit evaluation failed for %s %s: %s",
                frame.symbol,
                frame.timeframe,
                exc,
            )
            return
        action = str(verdict.get("action") or "none")
        if action not in ("exit", "dry_exit", "failed"):
            return
        try:
            sent = send_structure_exit_notice(
                symbol=frame.symbol,
                timeframe=frame.timeframe,
                mode=mode,
                action=action,
                reason=str(verdict.get("reason") or ""),
                settings=self._settings,
            )
            logger.info(
                "Structure-exit notice for %s %s: telegram=%s action=%s",
                frame.symbol,
                frame.timeframe,
                sent,
                action,
            )
        except Exception:
            logger.exception("Structure-exit notice failed for %s", frame.symbol)

    @staticmethod
    def _key_text(key: tuple[str, str]) -> str:
        return f"{key[0]}::{key[1]}"

    def _load_state(self) -> None:
        self._persisted_closed_ts: dict[str, int] = {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            values = raw.get("last_processed_closed_ts", {})
            if isinstance(values, dict):
                self._persisted_closed_ts = {str(k): int(v) for k, v in values.items()}
            streak = raw.get("structure_exit_streak", {})
            if isinstance(streak, dict):
                self._structure_exit_streak = {
                    str(k): v for k, v in streak.items() if isinstance(v, dict)
                }
            stats = raw.get("direction_gate_stats", {})
            if isinstance(stats, dict):
                self._direction_gate_stats = {
                    str(k): int(v) for k, v in stats.items() if isinstance(v, (int, float))
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable monitoring state: %s", exc)

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {
                        "last_processed_closed_ts": self._persisted_closed_ts,
                        "structure_exit_streak": self._structure_exit_streak,
                        "direction_gate_stats": self._direction_gate_stats,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            temp.replace(self._state_path)
        except OSError as exc:
            logger.warning("Could not save monitoring state: %s", exc)
