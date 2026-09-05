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
_ORDER_OPPORTUNITY_TYPES = frozenset({"限价单", "突破单", "市价单"})


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


def _has_order_opportunity(decision: dict[str, Any], confidence_threshold: int) -> bool:
    """Return whether a decision is eligible for alert-only notification."""
    if str(decision.get("order_type") or "") not in _ORDER_OPPORTUNITY_TYPES:
        return False
    try:
        confidence = int(float(str(decision.get("trade_confidence") or "")))
    except (TypeError, ValueError):
        return False
    return confidence >= confidence_threshold


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
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._cfg = settings.monitoring
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
        # 让自动发现的品种也能通过 testnet 下单的白名单检查：
        # 合并进 symbol_whitelist（保留用户手动配置的条目）。
        auto_cfg = self._settings.binance_usdm_testnet
        merged = list(dict.fromkeys([*auto_cfg.symbol_whitelist, *symbols]))
        if auto_cfg.symbol_whitelist != merged:
            auto_cfg.symbol_whitelist = merged
            self._report(
                f"symbol_whitelist 已同步自动发现品种: {len(merged)} 个 "
                f"({', '.join(merged)})"
            )
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

    def run_due_once(self, now: float | None = None) -> int:
        """Schedule due targets once. Public primarily for deterministic tests."""
        now = self._clock() if now is None else now
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

            decision = self._analyze(
                frame,
                record_sink=state,
                **self._incremental_kwargs(state, now),
            )
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
        record = orchestrator.submit(
            frame,
            CancelToken(),
            lambda _event: None,
            previous_record=previous_record,
            incremental_new_bar_count=incremental_new_bar_count,
        )
        if record_sink is not None and record is not None:
            record_sink.previous_record = record
        decision = record.stage2_decision if record is not None else None
        if not isinstance(decision, dict):
            return None
        inner = decision.get("decision") or {}
        from pa_agent.ai.decision_stance import confidence_threshold_for_stance
        threshold = confidence_threshold_for_stance(self._settings.general.decision_stance)
        if not _has_order_opportunity(inner, threshold):
            return decision

        # Persist a trade record and (when configured) auto-execute the Testnet
        # market order. Both are best-effort: a failure never disrupts analysis
        # or notifications. The default settings keep automated execution
        # disabled (binance_usdm_testnet.enabled=False), so this is a no-op
        # unless the operator explicitly enables it.
        try:
            self._save_order_opportunity(frame, decision, inner, record)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Monitor trade-record/execution failed for %s %s: %s",
                frame.symbol,
                frame.timeframe,
                exc,
            )

        from pa_agent.notify.feishu_notifier import send_order_signal as send_feishu
        from pa_agent.notify.pushplus_notifier import send_order_signal as send_pushplus
        from pa_agent.notify.telegram_notifier import send_order_signal as send_telegram

        feishu_sent = send_feishu(
            decision_inner=inner,
            stage2_full=decision,
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            settings=self._settings,
        )
        pushplus_sent = send_pushplus(
            decision_inner=inner,
            stage2_full=decision,
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            settings=self._settings,
        )
        telegram_sent = send_telegram(
            decision_inner=inner,
            stage2_full=decision,
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            settings=self._settings,
        )
        logger.info(
            "Monitor notification outcomes for %s %s: feishu=%s pushplus=%s telegram=%s",
            frame.symbol,
            frame.timeframe,
            feishu_sent,
            pushplus_sent,
            telegram_sent,
        )
        return decision

    def _save_order_opportunity(self, frame: Any, decision: dict, inner: dict, record: Any) -> None:
        """Persist the trade record and auto-execute the Testnet market signal."""
        from pa_agent.records.trade_logger import save_trade_record
        from pa_agent.trading.binance_usdm_testnet import execute_market_signal

        meta = getattr(record, "meta", None)
        decision_stance = ""
        model_name = ""
        if meta is not None:
            decision_stance = getattr(meta, "decision_stance", "") or ""
            provider = getattr(meta, "ai_provider", None) or {}
            if isinstance(provider, dict):
                model_name = str(provider.get("model") or "")
        flip_cooldown = int(getattr(self._settings.general, "structure_flip_cooldown_bars", 3) or 3)
        save_trade_record(
            decision_inner=inner,
            stage2_full=decision,
            stage1_diagnosis=getattr(record, "stage1_diagnosis", None),
            frame=frame,
            meta_symbol=frame.symbol,
            meta_timeframe=frame.timeframe,
            decision_stance=decision_stance,
            model_name=model_name,
            structure_flip_cooldown_bars=flip_cooldown,
        )

        result = execute_market_signal(inner, self._settings, analysis_symbol=frame.symbol)
        logger.info(
            "Binance U本位 Testnet 自动执行: status=%s symbol=%s reason=%s",
            result.status,
            result.symbol,
            result.reason,
        )
        # A failed execution must not be silent: notify besides the signal message.
        if result.status == "failed":
            try:
                from pa_agent.notify.telegram_notifier import send_execution_failure

                failed_sent = send_execution_failure(
                    symbol=frame.symbol,
                    timeframe=getattr(frame, "timeframe", ""),
                    status=result.status,
                    reason=result.reason,
                    settings=self._settings,
                )
                logger.info(
                    "Monitor execution-failure notification for %s: telegram=%s",
                    frame.symbol,
                    failed_sent,
                )
            except Exception:  # noqa: BLE001 - best-effort alerting
                logger.exception(
                    "Execution-failure notification failed for %s", frame.symbol
                )

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
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable monitoring state: %s", exc)

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps({"last_processed_closed_ts": self._persisted_closed_ts}, indent=2),
                encoding="utf-8",
            )
            temp.replace(self._state_path)
        except OSError as exc:
            logger.warning("Could not save monitoring state: %s", exc)
