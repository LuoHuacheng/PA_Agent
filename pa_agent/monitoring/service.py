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
        clock: Callable[[], float] = time.time,
        analyze: Callable[[Any], dict | None] | None = None,
        on_result: Callable[[Any, dict | None], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._cfg = settings.monitoring
        self._state_path = state_path
        self._source_factory = source_factory
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
        self._load_state()

        for target in self._cfg.targets:
            if target.enabled:
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
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self._cfg.enabled or not self._states or self.is_running:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._cfg.max_concurrent_analyses,
            thread_name_prefix="symbol-monitor",
        )
        self._thread = threading.Thread(target=self._run, name="symbol-monitor-scheduler", daemon=True)
        self._thread.start()
        targets = ", ".join(
            f"{state.target.symbol} {state.target.timeframe}"
            f" (next poll {time.strftime('%H:%M:%S', time.localtime(state.next_poll_at))})"
            for state in self._states.values()
        )
        self._report(
            f"Started multi-symbol monitor for {len(self._states)} target(s), "
            f"max_concurrent_analyses={self._cfg.max_concurrent_analyses}: {targets}"
        )

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        # Disconnect before waiting for executor threads. TradingView's blocking
        # socket read is released by disconnect(), allowing the worker to finish.
        for state in self._states.values():
            if state.source is not None:
                try:
                    state.source.disconnect()
                except Exception:
                    logger.debug("Monitor source disconnect failed", exc_info=True)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._save_state()

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

    def _done(self, state: _TargetState, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)
            state.running = False
        try:
            future.result()
        except Exception:
            logger.exception("Monitor task crashed for %s %s", state.target.symbol, state.target.timeframe)

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
            if state.last_processed_closed_ts is not None and closed_ts < state.last_processed_closed_ts:
                raise ValueError("data source returned an older closed bar")

            decision = self._analyze(frame)
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

            source = create_data_source(self._settings.general.last_data_source)
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

    def _analyze_and_notify(self, frame: Any) -> dict | None:
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
        record = orchestrator.submit(frame, CancelToken(), lambda _event: None)
        decision = record.stage2_decision if record is not None else None
        if not isinstance(decision, dict):
            return None
        inner = decision.get("decision") or {}
        threshold = int(self._settings.general.decision_confidence_threshold)
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
        logger.info(
            "Monitor notification outcomes for %s %s: feishu=%s pushplus=%s",
            frame.symbol,
            frame.timeframe,
            feishu_sent,
            pushplus_sent,
        )
        return decision

    def _save_order_opportunity(
        self, frame: Any, decision: dict, inner: dict, record: Any
    ) -> None:
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
        flip_cooldown = int(
            getattr(self._settings.general, "structure_flip_cooldown_bars", 3) or 3
        )
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
