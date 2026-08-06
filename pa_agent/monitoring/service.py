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
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._cfg = settings.monitoring
        self._state_path = state_path
        self._source_factory = source_factory
        self._clock = clock
        self._analyze = analyze or self._analyze_and_notify
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
        logger.info("Started multi-symbol monitor for %d target(s)", len(self._states))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        for state in self._states.values():
            if state.source is not None:
                try:
                    state.source.disconnect()
                except Exception:
                    logger.debug("Monitor source disconnect failed", exc_info=True)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
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

            self._analyze(frame)
            state.last_processed_closed_ts = closed_ts
            self._persisted_closed_ts[self._key_text((target.symbol, target.timeframe))] = closed_ts
            self._save_state()
            state.retry_count = 0
            state.last_error = ""
            self._schedule_next(state, now)
        except Exception as exc:
            state.last_error = str(exc)
            state.retry_count += 1
            logger.warning("Monitor poll failed for %s %s: %s", target.symbol, target.timeframe, exc)
            if state.retry_count <= self._cfg.poll_retry_attempts:
                state.next_poll_at = now + self._cfg.poll_retry_seconds
            else:
                state.retry_count = 0
                self._schedule_next(state, now)

    def _ensure_source(self, state: _TargetState) -> DataSource:
        if state.source is not None:
            return state.source
        if self._source_factory is None:
            from pa_agent.data.factory import create_data_source

            source = create_data_source(self._settings.general.last_data_source)
        else:
            source = self._source_factory(self._settings.general.last_data_source)
        source.connect()
        if self._settings.general.last_data_source == "tradingview":
            set_exchange = getattr(source, "set_exchange", None)
            if callable(set_exchange):
                set_exchange(self._settings.general.last_tradingview_exchange)
        source.subscribe(state.target.symbol, state.target.timeframe)
        state.source = source
        return source

    def _schedule_next(self, state: _TargetState, now: float) -> None:
        state.next_poll_at = next_poll_at(
            state.target.timeframe, now=now, lead_seconds=self._cfg.poll_lead_seconds
        )

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
        from pa_agent.gui.order_opportunity import has_order_opportunity

        threshold = int(self._settings.general.decision_confidence_threshold)
        if not has_order_opportunity(inner, confidence_threshold=threshold):
            return decision
        # Do not call Binance execution here. Monitoring is alert-only by design.
        from pa_agent.notify.feishu_notifier import send_order_signal as send_feishu
        from pa_agent.notify.pushplus_notifier import send_order_signal as send_pushplus

        send_feishu(
            decision_inner=inner,
            stage2_full=decision,
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            settings=self._settings,
        )
        send_pushplus(
            decision_inner=inner,
            stage2_full=decision,
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            settings=self._settings,
        )
        return decision

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
