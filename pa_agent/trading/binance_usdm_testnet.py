"""Binance USDⓈ-M Futures order execution (testnet default, live opt-in).

The execution environment follows ``settings.binance_usdm_environment``
(default testnet: behaviour unchanged). Live (实盘) switches the REST/WS
gateways, runtime state file and message labels via
``pa_agent.trading.binance_env``; the live section still requires explicit
``enabled`` / ``dry_run=false`` / ``emergency_stop=false`` before any order.
Credentials are read from the local gitignored ``settings.json`` file and
never written to application logs.
"""

import csv
import hashlib
import hmac
import json
import logging
import math
import os
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pa_agent.config.settings import BinanceUSDMTestnetSettings, Settings
from pa_agent.records import cancel_log
from pa_agent.trading import binance_env
from pa_agent.trading.binance_env import BinanceTradeEnv
from pa_agent.trading.rate_limit import parse_banned_until_ms, rate_limiter
from pa_agent.trading.runtime_state import RuntimeStateStore as _RuntimeStateStore
from pa_agent.util.trade_metrics import (
    compute_risk_reward,
    min_risk_reward_ratio,
    passes_trader_equation,
)

logger = logging.getLogger(__name__)

_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
_TIMEOUT_SECONDS = 12
_RUNTIME_STATE_PATH = "trade_records/binance_usdm_testnet_state.json"
# P2-3: conf 5分位桶实测胜率 (tools/trade_pnl_report.py --conf-buckets-out 生成)
_CONF_BUCKETS_PATH = "trade_records/conf_buckets.json"
# RLock: 遗留外层作用域(resume_*)与 RuntimeStateStore 内部方法对同一把锁嵌套加锁。
_STATE_LOCK = threading.RLock()
# Runtime state store: lock + file format + namespaced ops live in one module.
# path provider 每次调用时求值, 保证 monkeypatch _RUNTIME_STATE_PATH 仍生效.
_STATE_STORE: "_RuntimeStateStore" = _RuntimeStateStore(
    lambda: os.fspath(
        os.path.join(os.path.dirname(_RUNTIME_STATE_PATH), active_environment().state_file)
    ),
    lock=_STATE_LOCK,
)

# --- runtime execution environment (testnet/live) ------------------------
# The environment follows settings.binance_usdm_environment and is adopted at
# every public entry point that holds settings (execute/resume/pnl), so one
# process stays on one environment. The runtime profile drives the client
# default gateway, the per-environment state file and message labels.
_runtime_env_lock = threading.Lock()
_runtime_env: BinanceTradeEnv = binance_env.TESTNET_ENV


def configure_binance_environment(settings: Settings | None) -> BinanceTradeEnv:
    """Adopt the settings-declared execution environment (idempotent).

    Public entry points call this automatically; the monitor also calls it at
    startup so poller/WS wiring is consistent before any thread starts.
    """
    global _runtime_env
    env = binance_env.resolve_env(settings)
    with _runtime_env_lock:
        if env.key != _runtime_env.key:
            logger.info(
                "Binance execution environment: %s (%s)",
                env.label_zh,
                env.key,
            )
            _runtime_env = env
    return _runtime_env


def active_environment() -> BinanceTradeEnv:
    """Profile of the environment this process currently runs (default testnet)."""
    with _runtime_env_lock:
        return _runtime_env


def _new_client(config: BinanceUSDMTestnetSettings) -> "BinanceUSDMTestnetClient":
    """Construct the REST client from active settings (test patch point)."""
    return BinanceUSDMTestnetClient(config.api_key, config.api_secret)


def _maybe_guard(*args: Any, **kwargs: Any) -> None:
    """Arm position managers after a protected entry (proxy to position_manager)."""
    from pa_agent.trading.position_manager import _maybe_guard as _arm

    _arm(*args, **kwargs)


#: 持仓生命周期机制(四看护循环 + 补挂/桥接/swap + resume)已迁至 position_manager。
#: PEP 562 __getattr__ 保持旧访问路径 binance_usdm_testnet.<name> 有效(测试/CLI)。
_POSITION_MANAGER_NAMES = frozenset({
    "_MANAGER_LOCKS", "_MANAGER_LOCKS_GUARD", "_manager_lock",
    "_ALGO_LIVE_STATUSES", "_algo_status_live", "_rehang_stop_candidates",
    "_REHANG_SKIP_MARKERS", "_stop_resting_alive", "_rehang_protective_stop",
    "_ensure_protective_stop", "_swap_stop_to_price",
    "_breakeven_guard_loop", "_tp_runner_loop", "_timestop_deadline_hit",
    "_timestop_loop",
    "_resume_guards", "resume_breakeven_guards", "resume_tp_runners",
    "resume_time_stops", "resume_stop_watchdogs",
    "_UNMOVED_STOP_VERIFY_TICKS", "_stop_watchdog_loop",
})


def __getattr__(name: str):
    if name in _POSITION_MANAGER_NAMES:
        from pa_agent.trading import position_manager

        return getattr(position_manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



_ENTRY_CLIENT_PREFIX = "pa-entry-"

# --- user-data stream -> fill watcher wake-up registry --------------------
# The limit-entry watcher polls REST order status every poll_interval. With the
# user-data websocket live, an ORDER_TRADE_UPDATE for a resting clientOrderId
# should wake its watcher immediately; the watcher still confirms via REST, so
# correctness never depends on the event (missed events only cost latency).
# Entries are removed by the watcher on every exit path; a stale entry after a
# hard kill is a tiny dict slot and is overwritten on the next watcher run.
_watcher_wake_events: dict[str, "threading.Event"] = {}
_watcher_wake_lock = threading.Lock()


def _register_watcher_wake(client_id: str) -> "threading.Event":
    """Register (or reuse) the wake event for one limit-entry watcher."""
    with _watcher_wake_lock:
        event = _watcher_wake_events.get(client_id)
        if event is None:
            event = threading.Event()
            _watcher_wake_events[client_id] = event
        return event


def _unregister_watcher_wake(client_id: str) -> None:
    with _watcher_wake_lock:
        _watcher_wake_events.pop(client_id, None)


def notify_user_order_update(payload: dict[str, Any]) -> None:
    """Wake the fill watcher whose clientOrderId just got a user-data event."""
    order = payload.get("o") or {}
    client_id = str(order.get("c") or "")
    if not client_id:
        return
    with _watcher_wake_lock:
        event = _watcher_wake_events.get(client_id)
    if event is not None:
        event.set()

# Bounded retry for transient transport failures (torn TLS connections, stale
# timestamps under high latency). Only idempotent-safe requests are retried:
# every GET, and POSTs that carry an explicit idempotency key
# (newClientOrderId / clientAlgoId). Business errors are never retried.
_REQUEST_RETRIES = 2
_REQUEST_RETRY_SLEEP_S = 1.0
_RETRY_MARKERS = ("network error", "-1021", "invalid JSON")

# Whole-signal retry on Binance rate-limit bans (HTTP 418 -1003 / 429). The
# request layer never retries these: a banned IP needs seconds of rest, so the
# retry lives at signal level with exponential backoff (see execute_market_signal).
_RATE_LIMIT_MARKERS = ("http 418", "-1003", "http 429", "too many requests")


def _is_rate_limit_reason(reason: str) -> bool:
    low = (reason or "").lower()
    return any(marker in low for marker in _RATE_LIMIT_MARKERS)


# Bare-429 backoff (no banned-until in the body): the shared Testnet IP keeps
# answering 429 while it is hot, and a fixed 60s breaker window lets every
# watcher/guard fire again right after it lifts and re-trip the limit. Escalate
# the local window 60s -> 120s -> 240s (capped) across repeated bare 429s; a
# real banned-until response (HTTP 418) resets the escalation.
_RATE_LIMIT_429_BASE_SECONDS = 60.0
_RATE_LIMIT_429_MAX_SECONDS = 300.0
_RATE_LIMIT_429_ESCALATION_RESET_SECONDS = 600.0

_429_lock = threading.Lock()
_429_strikes = 0
_429_last_ts = 0.0


def _reset_429_strikes() -> None:
    """Clear the bare-429 escalation counter (call on real banned-until)."""
    global _429_strikes, _429_last_ts
    with _429_lock:
        _429_strikes = 0
        _429_last_ts = 0.0


def _next_429_backoff_until_ms() -> int:
    """Escalating breaker deadline (epoch ms) for a bare HTTP 429."""
    global _429_strikes, _429_last_ts
    with _429_lock:
        now = time.time()
        if now - _429_last_ts > _RATE_LIMIT_429_ESCALATION_RESET_SECONDS:
            _429_strikes = 0
        _429_strikes += 1
        _429_last_ts = now
        window = min(
            _RATE_LIMIT_429_MAX_SECONDS,
            _RATE_LIMIT_429_BASE_SECONDS * (2 ** (_429_strikes - 1)),
        )
    return int((now + window) * 1000)


def _observe_rate_limit_error(message: str) -> None:
    """Record a ban window into the shared breaker when *message* is a ban.

    Called right before a rate-limited request raises, so the monitor's
    scheduler can pause analysis and the guard loops can sleep through the
    ban instead of hammering the API.

    HTTP 418 with a banned-until timestamp wins (server-authoritative). A bare
    HTTP 429 without a timestamp gets an escalating local window instead of the
    fixed fallback, because repeated 429s mean the shared IP is still hot.
    """
    if not _is_rate_limit_reason(message):
        return
    until_ms = parse_banned_until_ms(message)
    if until_ms is None:
        until_ms = _next_429_backoff_until_ms()
    else:
        _reset_429_strikes()
    rate_limiter.record_ban(until_ms=until_ms)


def _stop_would_immediately_trigger(
    exit_side: str, trigger_price: Decimal, mark: Decimal
) -> bool:
    """True when a STOP at *trigger_price* would be rejected (-2021) because
    the mark is already on/beyond the trigger side.

    A breakeven stop sits at the entry price; when the mark has already moved
    back through the entry (profit given back), placing it would immediately
    trigger and the exchange answers -2021. Callers skip the placement (the
    original protective stop stays in place) instead of retrying into the
    error.
    """
    # 严格比较: mark 与 trigger 相等时交易所不判立即触发(可挂单),
    # 只有 mark 已严格越过 trigger 侧才拒绝.
    if exit_side == "SELL":  # 多单离场: 价格跌破 trigger 触发
        return mark < trigger_price
    if exit_side == "BUY":  # 空单离场: 价格升破 trigger 触发
        return mark > trigger_price
    return False


#: Upper bound for guard-loop cooldown sleeps while a ban is live; keeps a
#: guard responsive shortly after the ban ends without spamming the API.
_RATE_LIMIT_COOLDOWN_CAP_SECONDS = 300.0


def _rate_limit_cooldown_seconds(poll_seconds: float) -> float:
    """Sleep budget after a rate-limit failure: ride out the ban (capped)."""
    remaining = rate_limiter.remaining_seconds()
    if remaining is None:
        return float(poll_seconds)
    return min(
        max(float(remaining) + 5.0, float(poll_seconds)),
        _RATE_LIMIT_COOLDOWN_CAP_SECONDS,
    )


def _guard_rate_limit_wait(poll_seconds: float) -> None:
    """Sleep through a live ban plus a small jitter to stagger wake-ups.

    Guard/runner/time-stop loops would otherwise all wake at the same
    ban-until instant if they slept the exact remaining time; the jitter
    spreads the post-ban first polls so the shared IP is not re-tripped by a
    synchronized burst.
    """
    time.sleep(
        _rate_limit_cooldown_seconds(poll_seconds)
        + random.uniform(0.0, float(poll_seconds))
    )


class _RequestGate:
    """Process-wide throttle for every outbound Binance request.

    Guards/TP runners/snapshot poller run on independent daemon threads; even
    with a fresh snapshot they can burst together at bar close / ban release,
    and the shared Testnet egress IP turns any synchronized burst into a 429 or
    a punishing 418 ban. The gate caps in-flight requests and spaces the start
    of every request by a minimum gap, flattening pulses into a low trickle.

    Module-level on purpose: distinct client instances (guards create their own)
    must share the same gate.
    """

    def __init__(
        self, *, max_concurrency: int, min_gap_seconds: float
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if min_gap_seconds < 0:
            raise ValueError("min_gap_seconds must be >= 0")
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._min_gap = float(min_gap_seconds)
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def __enter__(self) -> "_RequestGate":
        self._sem.acquire()
        try:
            with self._lock:
                now = time.monotonic()
                target = max(now, self._last_ts + self._min_gap)
                self._last_ts = target
                delay = target - now
        except BaseException:
            self._sem.release()
            raise
        if delay > 0:
            time.sleep(delay)
        return self

    def __exit__(self, *exc: object) -> None:
        self._sem.release()


#: Shared throttle for all Binance REST traffic in this process.
_REQUEST_GATE = _RequestGate(max_concurrency=2, min_gap_seconds=0.2)


class BinanceAPIError(RuntimeError):
    """A rejected or unavailable Binance API request."""


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str
    symbol: str = ""
    quantity: str = ""
    entry_order_id: str = ""


def _is_retryable(exc: BinanceAPIError, method: str, params: dict[str, Any] | None) -> bool:
    """True when a transient transport failure may be retried safely.

    Limited to markers of network/clock-skew problems, restricted to idempotent
    requests: all GETs, and POSTs that carry an explicit idempotency key
    (newClientOrderId / clientAlgoId) so a retried write cannot duplicate.
    """
    message = str(exc)
    if not any(marker in message for marker in _RETRY_MARKERS):
        return False
    if method == "GET":
        return True
    payload = {k: str(v) for k, v in (params or {}).items() if v is not None}
    return "newClientOrderId" in payload or "clientAlgoId" in payload


def _raise_if_banned() -> None:
    """Raise a rate-limit error when a live ban blocks direct REST access."""
    if rate_limiter.is_banned():
        raise BinanceAPIError(
            f"Binance HTTP 418: IP banned until {rate_limiter.banned_until_ms()} "
            "(blocked locally, no request sent)"
        )


def _stop_gap_pct(reference: Decimal, stop: Decimal) -> Decimal:
    """Distance from *reference* (entry/mark price) to the stop, in percent.

    Used to reject decisions whose structural stop is too close to the entry:
    fills land straight on the protective stop and lock in the loss plus both
    legs of fees (P0-2).
    """
    if reference is None or reference == 0 or stop is None:
        return Decimal("100")
    return (abs(reference - stop) / reference) * 100


def _stop_distance_floor_pct(
    config: BinanceUSDMTestnetSettings, decision: dict[str, Any]
) -> Decimal:
    """Effective minimum entry->stop distance in percent.

    fixed mode returns the constant min_stop_distance_pct. atr mode lifts the
    floor to min_stop_atr_multiple x the latest analyzed bar ATR% (decision
    field atr_pct, injected by the monitor) whenever that is wider, so
    high-volatility symbols keep a noise-safe minimum while quiet symbols
    retain tight structural stops. Without ATR info it degrades to the fixed
    floor.
    """
    floor = Decimal(str(getattr(config, "min_stop_distance_pct", 0.45) or 0.45))
    mode = str(getattr(config, "min_stop_mode", "fixed") or "fixed").strip().lower()
    if mode != "atr":
        return floor
    try:
        atr_pct = float(decision.get("atr_pct") or 0.0)
    except (TypeError, ValueError):
        atr_pct = 0.0
    if atr_pct <= 0:
        return floor
    multiple = Decimal(str(getattr(config, "min_stop_atr_multiple", 0.8) or 0.8))
    return max(floor, multiple * Decimal(str(atr_pct)))


def _align_stop_tick(price: float, tick: float, *, down: bool) -> float:
    """Tick-align an outward stop move so the floor is strictly met."""
    scaled = price / tick
    aligned = (math.floor(scaled + 1e-12) if down else math.ceil(scaled - 1e-12)) * tick
    return round(aligned, 10)


def lift_stop_to_min_distance_floor(
    decision: dict[str, Any],
    config: BinanceUSDMTestnetSettings,
    *,
    tick: float | None = None,
) -> bool:
    """Plan-stage stop widening up to the executor's min entry-stop distance.

    The executor refuses ("Stop loss too close to entry", P0-2) any decision
    whose structural stop sits closer than the configured floor (fixed pct or
    min_stop_atr_multiple x ATR%): the plan gets recorded, then silently
    rejected with no order placed. Applying the same floor here, before the
    record is persisted, widens the stop outward (entry/TP untouched) by the
    smallest tick-aligned amount that satisfies the floor, so the recorded
    plan is actually executable.

    Conservative by design: never moves the stop when lifting would push TP1
    reward:risk below the minimum or break the §10.3 trader's equation - the
    decision is then left untouched and the executor keeps rejecting rather
    than silently changing a plan into an uneconomic trade.

    Returns True when the stop_loss_price was widened in place.
    """
    if str(decision.get("order_type") or "") not in ("限价单", "市价单"):
        return False
    side = _side_from_decision(decision.get("order_direction"))
    if side not in ("BUY", "SELL"):
        return False
    try:
        entry = float(decision["entry_price"])
        stop = float(decision["stop_loss_price"])
        tp = float(decision["take_profit_price"])
    except (TypeError, ValueError, KeyError):
        return False
    if not entry or entry <= 0:
        return False
    floor_pct = _stop_distance_floor_pct(config, decision)
    if floor_pct <= 0:
        return False
    gap_pct = (abs(entry - stop) / entry) * 100.0
    if gap_pct + 1e-9 >= float(floor_pct):
        return False
    distance = entry * float(floor_pct) / 100.0
    if side == "BUY":
        if stop >= entry:
            return False
        ideal = entry - distance
        new_stop = _align_stop_tick(ideal, tick, down=True) if tick and tick > 0 else round(ideal, 10)
        if new_stop >= entry:
            return False
    else:
        if stop <= entry:
            return False
        ideal = entry + distance
        new_stop = _align_stop_tick(ideal, tick, down=False) if tick and tick > 0 else round(ideal, 10)
        if new_stop <= entry:
            return False
    if abs(new_stop - stop) < 1e-12:
        return False
    rr = compute_risk_reward(entry, tp, new_stop, decision.get("order_direction"))
    if rr is None:
        return False
    risk = float(rr["risk"])
    reward = float(rr["reward"])
    min_ratio = min_risk_reward_ratio()
    if risk <= 0 or reward <= 0 or float(rr["ratio"]) + 1e-9 < min_ratio:
        return False
    win_rate = _parse_win_rate(decision.get("estimated_win_rate"))
    if win_rate is None or not passes_trader_equation(win_rate, risk, reward):
        return False
    decision["stop_loss_price"] = float(new_stop)
    return True


def _entry_client_id(signal_id: str) -> str:
    """Deterministic clientOrderId per signal.

    A retried placement reuses the same id so Binance deduplicates instead of
    creating a second entry order for the same signal.
    """
    return f"{_ENTRY_CLIENT_PREFIX}{signal_id}"[:36]


class BinanceUSDMTestnetClient:
    """Small signed REST client for the USDⓈ-M Futures API.

    Connects to the process runtime environment's REST gateway by default
    (testnet); pass *base_url* to pin a gateway explicitly.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        opener: Callable[..., Any] = urlopen,
        now_ms: Callable[[], int] | None = None,
        base_url: str | None = None,
    ) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise ValueError(
                f"Binance {active_environment().label_en} API key and secret are required"
            )
        self._base_url = (base_url or active_environment().rest_base).rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._opener = opener
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        # exchangeInfo is a big full-market response: cache per-symbol results
        # briefly so tick rounding never triggers an extra full fetch.
        self._info_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> dict[str, Any] | list[Any]:
        _raise_if_banned()
        for attempt in range(_REQUEST_RETRIES + 1):
            try:
                with _REQUEST_GATE:
                    return self._request_once(method, path, params, signed=signed)
            except BinanceAPIError as exc:
                if attempt >= _REQUEST_RETRIES or not _is_retryable(exc, method, params):
                    raise
                time.sleep(_REQUEST_RETRY_SLEEP_S * (attempt + 1))
        raise AssertionError("unreachable")  # pragma: no cover

    def _request_once(
        self, method: str, path: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> dict[str, Any] | list[Any]:
        payload = {k: str(v) for k, v in (params or {}).items() if v is not None}
        if signed:
            payload.setdefault("timestamp", str(self._now_ms()))
            payload.setdefault("recvWindow", "10000")
            query = urlencode(payload)
            payload["signature"] = hmac.new(
                self._api_secret, query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        query = urlencode(payload)
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        request = Request(url, method=method, headers={"X-MBX-APIKEY": self._api_key})
        try:
            with self._opener(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            _observe_rate_limit_error(f"Binance HTTP {exc.code}: {body}")
            raise BinanceAPIError(f"Binance HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise BinanceAPIError(f"Binance network error: {exc.reason}") from exc
        except OSError as exc:
            raise BinanceAPIError(f"Binance network error: {exc}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BinanceAPIError("Binance returned invalid JSON") from exc
        # Binance conditional (Algo) service answers success with an HTTP 200
        # body {"code":200,"msg":"success"}; only non-200 codes are errors.
        # The code arrives as int normally but has been observed serialized as
        # the string "200" (real testnet repro), so coerce before comparing.
        if isinstance(result, dict) and result.get("code") is not None:
            try:
                code = int(result["code"])
            except (TypeError, ValueError):
                code = -1
            if code not in (0, 200):
                _observe_rate_limit_error(f"Binance error {code}: {result.get('msg', '')}")
                raise BinanceAPIError(f"Binance error {code}: {result.get('msg', '')}")
        return result

    def create_listen_key(self) -> str:
        """Open a user-data stream; returns the listenKey (must be kept alive)."""
        result = self._request("POST", "/fapi/v1/listenKey")
        key = str(result.get("listenKey") or "")
        if not key:
            raise BinanceAPIError("Binance returned no listenKey")
        return key

    def keepalive_listen_key(self, listen_key: str) -> None:
        """Extend the user-data stream lifetime (call every < 60 minutes)."""
        self._request("PUT", "/fapi/v1/listenKey", {"listenKey": listen_key})

    def close_listen_key(self, listen_key: str) -> None:
        """Close the user-data stream (no more events after this)."""
        self._request("DELETE", "/fapi/v1/listenKey", {"listenKey": listen_key})

    _EXCHANGE_INFO_TTL_SECONDS = 300.0

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._info_cache.get(symbol)
        if cached is not None and now - cached[0] < self._EXCHANGE_INFO_TTL_SECONDS:
            return cached[1]
        response = self._request("GET", "/fapi/v1/exchangeInfo")
        item = None
        for candidate in response.get("symbols", []) if isinstance(response, dict) else []:
            if candidate.get("symbol") == symbol:
                item = candidate
                break
        if item is None:
            raise BinanceAPIError(f"{active_environment().label_en} does not list symbol {symbol}")
        self._info_cache[symbol] = (now, item)
        return item

    def mark_price(self, symbol: str) -> Decimal:
        result = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        try:
            return Decimal(str(result["markPrice"]))
        except (KeyError, ValueError) as exc:
            raise BinanceAPIError("Binance returned no mark price") from exc

    def all_mark_prices(self) -> dict[str, Decimal]:
        """Latest mark price for every listed symbol in one batched request.

        Replaces N per-symbol premiumIndex polls; the shared account-snapshot
        poller is the only regular reader of this endpoint.
        """
        result = self._request("GET", "/fapi/v1/premiumIndex")
        prices: dict[str, Decimal] = {}
        if isinstance(result, list):
            for row in result:
                symbol = str(row.get("symbol") or "")
                if not symbol:
                    continue
                try:
                    prices[symbol] = Decimal(str(row["markPrice"]))
                except Exception:  # malformed row: skip, keep the rest
                    continue
        if not prices:
            raise BinanceAPIError("Binance returned no mark prices")
        return prices

    def daily_close_series(self, symbol: str, days: int) -> list[float]:
        """Daily close prices (oldest first) covering the last ``days`` days."""
        start_ms = self._now_ms() - (days + 2) * 86_400_000
        result = self._request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1d", "startTime": start_ms, "limit": days + 2},
        )
        closes: list[float] = []
        if isinstance(result, list):
            for row in result:
                try:
                    closes.append(float(row[4]))
                except (TypeError, ValueError, IndexError):
                    continue
        if len(closes) < 2:
            raise BinanceAPIError(f"Binance returned no daily klines for {symbol}")
        return closes

    def one_way_mode(self) -> bool:
        result = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return not bool(result.get("dualSidePosition"))

    def set_one_way_mode(self) -> None:
        """Switch the account to one-way position mode (dualSidePosition=false).

        Binance rejects this while positions are open in hedge mode; callers
        should check for that BinanceAPIError and surface it as rejected.
        """
        self._request(
            "POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, signed=True
        )

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True
        )

    def place_market_order(
        self, *, symbol: str, side: str, quantity: Decimal, client_id: str
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": _decimal_text(quantity),
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )
        return _dict_response(result)

    def place_limit_order(
        self, *, symbol: str, side: str, quantity: Decimal, price: Decimal, client_id: str
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "LIMIT",
                "quantity": _decimal_text(quantity),
                "price": _decimal_text(price),
                "timeInForce": "GTC",
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )
        return _dict_response(result)

    def order_status(self, *, symbol: str, client_id: str) -> str:
        """Return the exchange status of an order placed with ``client_id``."""
        result = self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_id},
            signed=True,
        )
        return str(_dict_response(result).get("status") or "")

    def cancel_order(self, *, symbol: str, client_id: str) -> None:
        self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_id},
            signed=True,
        )

    def net_position(self, symbol: str) -> Decimal:
        """Signed open position amount for ``symbol`` (0.0 when flat).

        Read-only guard used before any automated entry so the bot never stacks
        a new position on an open one (P0-1).
        """
        rows = self._request(
            "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
        )
        if not isinstance(rows, list) or not rows:
            return Decimal("0")
        raw = rows[0].get("positionAmt")
        try:
            return Decimal(str(raw))
        except Exception as exc:
            raise BinanceAPIError(
                f"Unexpected position payload for {symbol}: {exc}"
            ) from exc

    def position_info(self, symbol: str) -> dict[str, Any]:
        """Signed open amount and average entry price for ``symbol``.

        Returns ``{"amount": Decimal, "entry": Decimal | None}`` (amount 0 when
        flat). Used by the breakeven guard to track real fill prices.
        """
        rows = self._request(
            "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True
        )
        if not isinstance(rows, list) or not rows:
            return {"amount": Decimal("0"), "entry": None}
        row = rows[0]
        try:
            amount = Decimal(str(row.get("positionAmt") or 0))
        except Exception as exc:
            raise BinanceAPIError(f"Unexpected position payload for {symbol}: {exc}") from exc
        entry: Decimal | None = None
        try:
            raw_entry = row.get("entryPrice")
            if raw_entry not in (None, "", "0"):
                entry = Decimal(str(raw_entry))
        except Exception:
            entry = None
        return {"amount": amount, "entry": entry}

    def all_positions(self) -> dict[str, dict[str, Any]]:
        """Open amount/entry for every symbol in one batched request.

        Only symbols reported by the exchange are listed (missing symbol ==
        flat). One signed GET replaces N per-symbol positionRisk polls; the
        shared account-snapshot poller is the only regular reader.
        """
        rows = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        positions: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if not symbol:
                    continue
                try:
                    amount = Decimal(str(row.get("positionAmt") or 0))
                except Exception:
                    continue
                entry: Decimal | None = None
                try:
                    raw_entry = row.get("entryPrice")
                    if raw_entry not in (None, "", "0"):
                        entry = Decimal(str(raw_entry))
                except Exception:
                    entry = None
                positions[symbol] = {"amount": amount, "entry": entry}
        if not positions:
            raise BinanceAPIError("Binance returned no positions")
        return positions

    def income_history(
        self, *, start_ms: int, end_ms: int | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Fetch account income ledger rows, paging forward by last row time.

        /fapi/v1/income pages by startTime (no cursor): advance it past the last
        returned row until a short page confirms the end.
        """
        rows: list[dict[str, Any]] = []
        cursor = start_ms
        while True:
            params: dict[str, Any] = {"startTime": cursor, "limit": limit}
            if end_ms is not None:
                params["endTime"] = end_ms
            batch = self._request("GET", "/fapi/v1/income", params, signed=True)
            if not isinstance(batch, list):
                break
            rows.extend(batch)
            if len(batch) < limit:
                break
            last_ms = int(batch[-1]["time"])
            if last_ms + 1 <= cursor:
                break
            cursor = last_ms + 1
        return rows

    def place_close_algo_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        stop_price: Decimal,
        client_algo_id: str,
        quantity: Decimal | None = None,
        close_position: bool = True,
    ) -> None:
        # Binance migrated conditional orders to the Algo Service in December 2025.
        # triggerPrice must respect the symbol's PRICE_FILTER tick size, or the
        # exchange answers -1111 "Precision is over the maximum defined for
        # this asset" (seen on breakeven stops placed at raw average-fill
        # prices). Round down to tick before signing; exchange_info is cached.
        try:
            info = self.exchange_info(symbol)
            stop_price = _price_for_tick(stop_price, info)
        except BinanceAPIError:
            pass  # keep legacy behavior: never block a protective order on tick data
        params: dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": _decimal_text(stop_price),
            "workingType": "MARK_PRICE",
            "priceProtect": "TRUE",
            "clientAlgoId": client_algo_id,
        }
        if close_position:
            params["closePosition"] = "true"
        if quantity is not None:
            # reduceOnly 部分单(如 TP1 半仓): 只带 quantity, 绝不 closePosition,
            # 否则会把剩余仓位也一次性平掉(Testnet 实测接受的参数形态)。
            params["quantity"] = _decimal_text(quantity)
            params["reduceOnly"] = "true"
        self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def cancel_algo_order(self, *, client_algo_id: str) -> None:
        self._request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {"clientAlgoId": client_algo_id},
            signed=True,
        )

    def algo_order_status(self, *, client_algo_id: str) -> dict[str, Any]:
        """Query a conditional algo order by its client id."""
        return self._request(
            "GET",
            "/fapi/v1/algoOrder",
            {"clientAlgoId": client_algo_id},
            signed=True,
        )

    def close_market_position(self, *, symbol: str, side: str, quantity: Decimal) -> None:
        self._request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": _decimal_text(quantity),
                "reduceOnly": "true",
                "newClientOrderId": f"pa-rollback-{uuid.uuid4().hex[:20]}",
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )


def _daily_loss_guard(
    client: BinanceUSDMTestnetClient, config: BinanceUSDMTestnetSettings
) -> ExecutionResult | None:
    """日度亏损熔断: 当日净亏损超限则拒绝开新仓, 并把熔断状态写进 state.

    账本查询 weight 30, 所以当天一旦触发就不再重复查询(直接读 state)。
    账本拉取失败一律放行(fail-open): 熔断是保护, 不该因为一次网络抖动把正常
    交易全部卡死。
    """
    from pa_agent.trading import risk_guard

    limit = float(getattr(config, "daily_loss_limit_usdt", 0.0) or 0.0)
    if limit <= 0:
        return None
    now_ms = int(time.time() * 1000)
    record = risk_guard.halt_record(_STATE_STORE.load(), now_ms)
    if record is not None:
        return ExecutionResult(
            "skipped",
            f"Daily loss halt active for {record.get('day')} "
            f"(net {float(record.get('net_usdt') or 0.0):+.2f}U vs -{limit:.2f}U); "
            f"resumes next local day",
        )
    try:
        rows = client.income_history(start_ms=risk_guard.day_start_ms(now_ms))
    except BinanceAPIError as exc:
        logger.warning("Daily loss check skipped (income fetch failed): %s", exc)
        return None
    hit, net = risk_guard.breach(rows, limit)
    if not hit:
        return None
    _STATE_STORE.update(
        lambda state: state.__setitem__(
            risk_guard.HALT_KEY, risk_guard.build_halt(net, limit, now_ms)
        )
    )
    logger.error(
        "Daily loss limit hit: net %+.2fU <= -%.2fU; auto-entry halted until next local day",
        net,
        limit,
    )
    return ExecutionResult(
        "rejected",
        f"Daily loss limit hit (net {net:+.2f}U <= -{limit:.2f}U); "
        f"auto-entry halted until next local day",
    )


def execute_market_signal(
    decision: dict[str, Any],
    settings: Settings | None,
    *,
    analysis_symbol: str = "",
    client: BinanceUSDMTestnetClient | None = None,
    prefetched_trend_pct: float | None = None,
) -> ExecutionResult:
    """Execute one validated market signal, with mandatory TP/SL protection.

    One-shot; no rate-limit retries. Binance Testnet shares public egress IPs
    and frequently answers HTTP 418 (code -1003, IP banned). The request layer
    now refuses to send anything while a ban is live, and retrying a
    rate-limited submission only extends the penalty, so rate-limit failures
    are NOT retried - every other failure also stays one-shot. Re-entry is safe
    because the first attempt never records the signal on a failed path and the
    open-position guard blocks duplicate entries.
    """
    configure_binance_environment(settings)
    conflict = binance_env.env_conflicts(settings)
    if conflict:
        return ExecutionResult("rejected", conflict)
    config = binance_env.active_cfg(settings)
    if not config.enabled:
        return ExecutionResult(
            "skipped", f"Binance {active_environment().label_en} automation disabled"
        )
    if config.emergency_stop:
        return ExecutionResult("skipped", "Emergency stop enabled")
    if config.dry_run:
        return ExecutionResult("dry_run", "Dry-run enabled, no API request sent")
    if not isinstance(decision, dict):
        return ExecutionResult("rejected", "Invalid decision")
    order_type = str(decision.get("order_type") or "")
    if order_type not in {"市价单", "限价单"}:
        return ExecutionResult(
            "rejected", "Only 市价单/限价单 is automated; breakout plans require manual review"
        )
    if order_type == "限价单" and not config.limit_order_enabled:
        return ExecutionResult("skipped", "Limit order automation disabled")

    symbol = str(config.symbol or "").upper().strip()
    whitelist = {item.upper().strip() for item in config.symbol_whitelist}
    if not whitelist:
        return ExecutionResult("rejected", "No whitelisted symbols configured")
    analysis = (analysis_symbol or "").upper().strip()
    if config.require_analysis_symbol_match:
        # Multi-symbol monitoring: when the analyzed symbol is whitelisted it is
        # the trade target; otherwise fall back to the configured default.
        if analysis and analysis in whitelist:
            symbol = analysis
        elif not symbol or symbol not in whitelist:
            return ExecutionResult("rejected", f"Symbol {symbol or 'unset'} is not whitelisted")
    else:
        if not symbol or symbol not in whitelist:
            return ExecutionResult("rejected", f"Symbol {symbol or 'unset'} is not whitelisted")
    side = _side_from_decision(decision.get("order_direction"))
    if side is None:
        return ExecutionResult("rejected", "Unsupported order direction")
    stop = _positive_decimal(decision.get("stop_loss_price"))
    target = _positive_decimal(decision.get("take_profit_price"))
    if stop is None or target is None:
        return ExecutionResult("rejected", "Stop loss and take profit are required")
    signal_conf = _parse_win_rate(decision.get("trade_confidence"))
    if config.require_trader_equation:
        win_rate = _measured_win_rate_override(signal_conf, config)
        if win_rate is None:
            win_rate = _parse_win_rate(decision.get("estimated_win_rate"))
        if win_rate is None:
            return ExecutionResult(
                "rejected", "estimated_win_rate missing; cannot verify trader's equation"
            )
        entry = _positive_decimal(decision.get("entry_price"))
        # Risk/reward measured from entry (not stop↔target): Brooks equation is
        # win_rate×reward > (1−win_rate)×risk with risk=entry→SL, reward=entry→TP.
        rr = compute_risk_reward(entry, target, stop, decision.get("order_direction"))
        if rr is None or not passes_trader_equation(
            win_rate, float(rr["risk"]), float(rr["reward"])
        ):
            return ExecutionResult(
                "rejected", "Trader's equation not satisfied (§10.3), refusing auto-order"
            )
    signal_id = _signal_id(symbol, decision)
    if _is_recent_signal(signal_id, config.cooldown_minutes):
        return ExecutionResult("skipped", "Duplicate signal is within cooldown period", symbol)

    try:
        active_client = client or BinanceUSDMTestnetClient(
            config.api_key,
            config.api_secret,
        )
        halted = _daily_loss_guard(active_client, config)
        if halted is not None:
            return halted
        # Resolve any previous resting entry for this symbol before the
        # open-position guard: a watcher that died between fill and protection
        # (process restart) or a partial fill is repaired here with the SL/TP
        # recorded at placement time.
        replacement = _replace_pending_limit(active_client, symbol, config)
        if replacement is not None:
            return replacement
        if not active_client.one_way_mode():
            # Program is one-way-mode only; auto-switch the account instead of
            # rejecting the signal. Fails if hedge-mode positions are open.
            try:
                active_client.set_one_way_mode()
                logger.info(f"{active_environment().label_en} account switched to one-way position mode")
            except BinanceAPIError:
                return ExecutionResult(
                    "rejected",
                    "Hedge mode unsupported and auto-switch failed; "
                    "close hedge positions or switch to one-way mode manually",
                )
        position = active_client.net_position(symbol)
        if position != 0:
            return ExecutionResult(
                "rejected",
                f"Position already open for {symbol} "
                f"({_decimal_text(position)}); refusing duplicate entry",
                symbol,
            )
        info = active_client.exchange_info(symbol)
        price = active_client.mark_price(symbol)
        stop = _price_for_tick(stop, info)
        target = _price_for_tick(target, info)
        # Daily-trend guard (whitelisted symbols only): a signal against the
        # daily-close trend of the symbol over the last trend_lookback_days needs
        # higher confidence and, when allowed, runs at reduced leverage/size.
        leverage = config.leverage
        size_scale = 1.0
        trend_note = ""
        guard_active = (
            bool(getattr(config, "counter_trend_block", False))
            or config.counter_trend_min_confidence > 0
            or config.counter_trend_size_scale < 1.0
        )
        if guard_active:
            trend_pct = prefetched_trend_pct
            if trend_pct is None:
                trend_pct = _daily_trend_pct(active_client, symbol, config.trend_lookback_days)
            bucket = _trend_bucket(trend_pct, config.trend_neutral_band_pct)
            counter = (side == "BUY" and bucket == "bear") or (side == "SELL" and bucket == "bull")
            if counter:
                if bool(getattr(config, "counter_trend_block", False)):
                    return ExecutionResult(
                        "rejected",
                        f"Counter-trend vs {config.trend_lookback_days}d trend "
                        f"({trend_pct:+.1f}%) blocked by counter_trend_block",
                        symbol,
                    )
                conf = signal_conf
                if config.counter_trend_min_confidence > 0 and (
                    conf is None or conf < config.counter_trend_min_confidence
                ):
                    return ExecutionResult(
                        "rejected",
                        f"Counter-trend vs 30d trend ({trend_pct:+.1f}%) rejected: "
                        f"trade_confidence {conf} < {config.counter_trend_min_confidence}",
                        symbol,
                    )
                if config.counter_trend_size_scale < 1.0:
                    size_scale = float(config.counter_trend_size_scale)
                    leverage = max(1, round(config.leverage * size_scale))
                    trend_note = (
                        f"; counter-trend 30d ({trend_pct:+.1f}%) scaled leverage to {leverage}x "
                        f"and size to {size_scale:.2f}x"
                    )
        # 保证金恒定：名义价值 = 保证金(margin_usdt) × 杠杆，杠杆变化不影响保证金。
        margin_usdt = float(config.max_notional_usdt)
        risk_usdt = float(config.risk_per_trade_usdt or 0.0)
        if risk_usdt > 0:
            # 逆势减仓必须落在风险金上。风险定仓模式下数量只由 risk 与止损距离
            # 决定, 缩放 leverage 只动名义上限(cap): 在 min_stop_distance_pct
            # 下限之下 cap 永远够用, 所以旧写法等于没有减仓(2026-09-10 复核).
            risk_usdt *= size_scale
            # P2-1 风险等额: 锚=市价(mark)或 resting 限价(entry); 越过市价的
            # 限价按市价成交, 锚回退为 mark. 名义超 margin*leverage 则拒单.
            anchor = price
            if order_type == "限价单":
                limit_entry = _positive_decimal(decision.get("entry_price"))
                if limit_entry is not None and (
                    (side == "BUY" and limit_entry < price)
                    or (side == "SELL" and limit_entry > price)
                ):
                    anchor = limit_entry
            quantity = _quantity_for_risk(risk_usdt, anchor, stop, info)
            cap = margin_usdt * leverage
            if quantity is not None and float(quantity) * float(anchor) > cap * 1.0001:
                return ExecutionResult(
                    "rejected",
                    f"Risk sizing needs {float(quantity) * float(anchor):.2f} USDT "
                    f"notional > margin cap {cap:.2f}",
                    symbol,
                )
        else:
            notional = margin_usdt * leverage
            quantity = _quantity_for_notional(notional, price, info)
        if quantity is None:
            return ExecutionResult(
                "rejected", "Configured margin is below symbol minimum or invalid"
            )
        if order_type == "限价单":
            return _execute_limit_signal(
                active_client,
                decision,
                config,
                symbol,
                side,
                stop,
                target,
                quantity,
                price,
                info,
                signal_id,
                leverage,
                trend_note,
                signal_conf,
            )
        if side == "BUY" and not (stop < price < target):
            return ExecutionResult("rejected", "Long requires stop < mark price < target")
        if side == "SELL" and not (target < price < stop):
            return ExecutionResult("rejected", "Short requires target < mark price < stop")
        gap = _stop_gap_pct(price, stop)
        stop_floor = _stop_distance_floor_pct(config, decision)
        if gap < stop_floor:
            return ExecutionResult(
                "rejected",
                f"Stop loss too close to market price "
                f"({gap:.3f}% < {stop_floor:.3f}% minimum)",
                symbol,
            )
        return _enter_and_protect(
            active_client,
            symbol=symbol,
            side=side,
            quantity=quantity,
            leverage=leverage,
            stop=stop,
            target=target,
            target2=_positive_decimal(decision.get("take_profit_price_2")),
            partial_pct=float(config.tp_partial_close_pct or 0.0),
            signal_id=signal_id,
            conf=signal_conf,
            config=config,
            message=(
                f"{active_environment().label_en} entry and protective orders submitted"
                + trend_note
            ),
        )
    except (BinanceAPIError, ValueError) as exc:
        message = str(exc)
        if "-2015" in message or "HTTP 401" in message:
            message += " (Hint: check configured API key/secret pair and futures permission.)"
        logger.warning(f"Binance {active_environment().label_en} automatic order rejected: %s", message)
        return ExecutionResult("failed", message, symbol)


def _execute_limit_signal(
    client: BinanceUSDMTestnetClient,
    decision: dict[str, Any],
    config: BinanceUSDMTestnetSettings,
    symbol: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    quantity: Decimal,
    mark_price: Decimal,
    exchange_info: dict[str, Any],
    signal_id: str,
    leverage: int,
    trend_note: str = "",
    conf: float | None = None,
) -> ExecutionResult:
    if not config.limit_order_enabled:
        return ExecutionResult("skipped", "Limit order automation disabled", symbol)
    entry_price = _positive_decimal(decision.get("entry_price"))
    if entry_price is None:
        return ExecutionResult("rejected", "Limit order entry price required")
    entry_price = _price_for_tick(entry_price, exchange_info)
    if side == "BUY":
        if not stop < entry_price:
            return ExecutionResult("rejected", "Long limit requires stop < limit price")
        crosses_mark = entry_price >= mark_price
    else:
        if not entry_price < stop:
            return ExecutionResult("rejected", "Short limit requires limit price < stop")
        crosses_mark = entry_price <= mark_price
    gap = _stop_gap_pct(mark_price if crosses_mark else entry_price, stop)
    stop_floor = _stop_distance_floor_pct(config, decision)
    if gap < stop_floor:
        return ExecutionResult(
            "rejected",
            f"Stop loss too close to entry "
            f"({gap:.3f}% < {stop_floor:.3f}% minimum)",
            symbol,
        )
    if crosses_mark:
        # A crossed limit would fill immediately. Submit a market entry instead
        # so protection is attached through the same rollback-safe path.
        return _enter_and_protect(
            client,
            symbol=symbol,
            side=side,
            quantity=quantity,
            leverage=leverage,
            stop=stop,
            target=target,
            target2=_positive_decimal(decision.get("take_profit_price_2")),
            partial_pct=float(config.tp_partial_close_pct or 0.0),
            signal_id=signal_id,
            conf=conf,
            config=config,
            message=(
                "Limit entry crossed mark price; submitted market entry and "
                "protective orders" + trend_note
            ),
        )
    replacement = _replace_pending_limit(client, symbol, config)
    if replacement is not None:
        return replacement
    cooldown_reason = _repricing_cooldown_reason(symbol, entry_price, exchange_info, config)
    if cooldown_reason is not None:
        logger.info(
            f"Skipped {active_environment().label_en} limit entry for %s: %s",
            symbol,
            cooldown_reason,
        )
        return ExecutionResult("skipped", cooldown_reason, symbol)
    client.set_leverage(symbol, leverage)
    entry_client_id = _entry_client_id(signal_id)
    target2 = _positive_decimal(decision.get("take_profit_price_2"))
    pending_record = {
        "client_id": entry_client_id,
        "signal_id": signal_id,
        "side": side,
        "quantity": _decimal_text(quantity),
        "stop": _decimal_text(stop),
        "target": _decimal_text(target),
        "target2": "" if target2 is None else _decimal_text(target2),
        "conf": "" if conf is None else str(conf),
        "entry": _decimal_text(entry_price),
        "placed_at": time.time(),
    }
    _persist_pending(symbol, pending_record)
    try:
        order = client.place_limit_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=entry_price,
            client_id=entry_client_id,
        )
    except (BinanceAPIError, ValueError):
        _drop_pending(symbol, entry_client_id)
        raise
    watcher = threading.Thread(
        target=_watch_limit_entry,
        kwargs={
            "client": client,
            "symbol": symbol,
            "client_id": entry_client_id,
            "side": side,
            "stop": stop,
            "target": target,
            "target2": target2,
            "quantity": quantity,
            "signal_id": signal_id,
            "conf": conf,
            "config": config,
            "timeout_seconds": config.limit_fill_timeout_minutes * 60,
            "poll_interval": config.limit_poll_interval_seconds,
        },
        daemon=True,
    )
    watcher.start()
    logger.info(
        f"{active_environment().label_en} limit entry placed for %s: order=%s entry=%s qty=%s",
        symbol,
        entry_client_id,
        _decimal_text(entry_price),
        _decimal_text(quantity),
    )
    return ExecutionResult(
        "pending",
        f"{active_environment().label_en} limit entry placed; awaiting fill",
        symbol,
        _decimal_text(quantity),
        str(order.get("orderId", "")),
    )


def _replace_pending_limit(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    config: BinanceUSDMTestnetSettings | None = None,
) -> ExecutionResult | None:
    """Cancel any resting limit entry for ``symbol``; protect a filled one.

    Returns an ExecutionResult when the previous entry already filled (the
    position is now protected and no new entry is placed), else ``None`` so the
    caller proceeds with a fresh entry.
    """
    old = _STATE_STORE.pending_get(symbol)
    if not isinstance(old, dict):
        return None
    old_client_id = str(old.get("client_id") or "")
    old_signal_id = str(old.get("signal_id") or "")
    side = str(old.get("side") or "")
    stop = _positive_decimal(old.get("stop"))
    target = _positive_decimal(old.get("target"))
    target2 = _positive_decimal(old.get("target2"))
    quantity = _positive_decimal(old.get("quantity"))
    old_conf = _parse_win_rate(old.get("conf"))
    if not old_client_id or side not in ("BUY", "SELL") or stop is None or target is None:
        _drop_pending(symbol, old_client_id)
        return None
    try:
        status = client.order_status(symbol=symbol, client_id=old_client_id)
    except BinanceAPIError as exc:
        if _is_missing_order_error(exc):
            logger.info(f"Removing stale {active_environment().label_en} pending entry for %s: %s", symbol, exc)
            _record_cancel_event(
                symbol,
                old_client_id,
                cancel_log.REASON_STALE_ENTRY_REMOVED,
                detail="Exchange no longer knows the resting entry",
                entry_price=old.get("entry"),
                signal_id=old_signal_id,
            )
            _drop_pending(symbol, old_client_id)
            return None
        logger.warning("Cannot inspect previous limit entry for %s: %s", symbol, exc)
        return ExecutionResult("failed", f"Cannot inspect previous limit entry: {exc}", symbol)
    if status == "FILLED":
        # The watcher died (process restart) before attaching protection: repair.
        try:
            sl_algo_id, tp_algo_id, partial_qty = _attach_protection(
                client, symbol, side, stop, target,
                quantity=quantity,
                target2=target2,
                partial_pct=
                    float(config.tp_partial_close_pct) if config is not None else 0.0,
            )
        except BinanceAPIError as exc:
            logger.error("Filled limit entry for %s left unprotected: %s", symbol, exc)
            return ExecutionResult(
                "failed", f"Filled limit entry needs manual protection: {exc}", symbol
            )
        if old_signal_id:
            _remember_signal(old_signal_id)
        if config is not None:
            _maybe_guard(
                client, config, symbol, side, stop, target, sl_algo_id, old_conf,
                quantity=quantity,
                target2=target2,
                tp_algo_id=tp_algo_id,
            partial_qty=partial_qty,
            )
        _drop_pending(symbol, old_client_id)
        return ExecutionResult("skipped", "Previously filled limit entry now protected", symbol)
    if status in ("NEW", "PARTIALLY_FILLED"):
        try:
            client.cancel_order(symbol=symbol, client_id=old_client_id)
        except BinanceAPIError as exc:
            _record_cancel_event(
                symbol,
                old_client_id,
                cancel_log.REASON_CANCEL_FAILED,
                detail=str(exc),
                entry_price=old.get("entry"),
                signal_id=old_signal_id,
            )
            return ExecutionResult("failed", f"Cannot replace pending limit entry: {exc}", symbol)
        _record_cancel_event(
            symbol,
            old_client_id,
            cancel_log.REASON_PLAN_REPLACED,
            detail="Replaced stale resting limit entry with a fresh plan",
            entry_price=old.get("entry"),
            signal_id=old_signal_id,
        )
        _remember_canceled_entry(symbol, old.get("entry"), cancel_log.REASON_PLAN_REPLACED)
        logger.info(f"Replaced stale {active_environment().label_en} limit entry %s for %s", old_client_id, symbol)
        if status == "PARTIALLY_FILLED":
            # Never discard a partial fill while replacing the entry: protect
            # it with the recorded SL/TP (or roll it back) first.
            _settle_after_entry_gone(
                client,
                symbol,
                side,
                stop,
                target,
                quantity,
                old_client_id,
                old_signal_id,
                context="replaced pending entry",
                config=config,
                conf=old_conf,
                target2=target2,
            )
    else:
        _drop_pending(symbol, old_client_id)
    return None


def _attach_protection(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    *,
    quantity: Decimal | None = None,
    target2: Decimal | None = None,
    partial_pct: float = 0.0,
) -> tuple[str, str]:
    """Attach STOP_MARKET plus TAKE_PROFIT_MARKET protection for a new position.

    When partial TP1 is enabled (partial_pct > 0) and the position/TP2 are
    known, the TP1 order is a reduceOnly half-size order (never closePosition);
    a runner thread later moves the remainder toward TP2. When the partial
    plan is infeasible (TP2 missing / size below lot minimum / pct=100) the
    TP1 order keeps the legacy close-all shape so a position is never left
    without its take-profit.

    Returns the generated client algo ids of the (stop, target) orders.
    On failure, cancels any orders already placed and re-raises so the caller
    can roll back the position.
    """
    exit_side = "SELL" if side == "BUY" else "BUY"
    partial_qty: Decimal | None = None
    if partial_pct > 0 and quantity is not None and quantity > 0 and target2 is not None:
        partial_qty = _partial_quantity(quantity, partial_pct, client.exchange_info(symbol))
    protected_algo_ids: list[str] = []
    try:
        stop_algo_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="STOP_MARKET",
            stop_price=stop,
            client_algo_id=stop_algo_id,
        )
        protected_algo_ids.append(stop_algo_id)
        target_algo_id = f"pa-tp-{uuid.uuid4().hex[:24]}"
        partial_kwargs: dict[str, Any] = {}
        if partial_qty is not None:
            # reduceOnly 部分单: 带 quantity 不带 closePosition, 否则一次平光。
            partial_kwargs = {"quantity": partial_qty, "close_position": False}
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="TAKE_PROFIT_MARKET",
            stop_price=target,
            client_algo_id=target_algo_id,
            **partial_kwargs,
        )
        protected_algo_ids.append(target_algo_id)
    except BinanceAPIError:
        # Avoid leaving a close-all trigger that could affect a later position.
        for client_algo_id in protected_algo_ids:
            try:
                client.cancel_algo_order(client_algo_id=client_algo_id)
            except BinanceAPIError:
                logger.exception(f"Failed to cancel orphaned {active_environment().label_en} protective order")
        raise
    return stop_algo_id, target_algo_id, partial_qty


def _enter_and_protect(
    client: BinanceUSDMTestnetClient,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    leverage: int,
    stop: Decimal,
    target: Decimal,
    target2: Decimal | None,
    partial_pct: float,
    signal_id: str,
    conf: float | None,
    config: BinanceUSDMTestnetSettings,
    message: str,
) -> ExecutionResult:
    """Place a market entry, attach SL/TP protection, roll back on failure.

    Single owner of the entry sequence shared by the market pipeline and the
    crossed-limit pipeline. The rollback invariant ("never leave an
    unprotected automatically-created position") holds by construction instead
    of by keeping two copies in sync.
    """
    client.set_leverage(symbol, leverage)
    entry = client.place_market_order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        client_id=_entry_client_id(signal_id),
    )
    try:
        sl_algo_id, tp_algo_id, partial_qty = _attach_protection(
            client, symbol, side, stop, target,
            quantity=quantity, target2=target2,
            partial_pct=partial_pct,
        )
    except BinanceAPIError:
        # Never leave an unprotected automatically-created position.
        client.close_market_position(
            symbol=symbol, side="SELL" if side == "BUY" else "BUY", quantity=quantity
        )
        raise
    _remember_signal(signal_id)
    _maybe_guard(
        client, config, symbol, side, stop, target, sl_algo_id, conf,
        quantity=quantity,
        target2=target2,
        tp_algo_id=tp_algo_id,
        partial_qty=partial_qty,
    )
    return ExecutionResult(
        "submitted",
        message,
        symbol,
        _decimal_text(quantity),
        str(entry.get("orderId", "")),
    )



# --- 保本移动止损 / TP1 部分止盈 runner -----------------------------
# Registry lives in the runtime state file under key "guards":
#   symbol -> {stop_algo_id, tp_algo_id, stop0, target, target2?, qty?,
#              partial_qty?, side, conf, ts, moved, partial_done?}
#  partial 相关字段仅在 TP1 部分止盈启用时写入; moved 由保本 guard 或
#  TP runner 置位, partial_done 由 TP runner 置位。

def _guard_enabled(config: BinanceUSDMTestnetSettings, conf: float | None) -> bool:
    """True when the breakeven feature applies to a signal with ``conf``."""
    if str(config.breakeven_stop_trigger) == "off":
        return False
    if conf is None:
        return config.breakeven_min_confidence <= 0
    return conf >= config.breakeven_min_confidence


def _guard_trigger_reached(
    *,
    mark: Decimal,
    entry: Decimal,
    stop0: Decimal,
    target: Decimal,
    side: str,
    trigger: str,
) -> bool:
    """True when float profit reaches the configured breakeven trigger.

    1r:        |mark - entry| >= |entry - stop0| (risk R).
    tp:        price reached the TP1 target.
    1r_or_tp:  whichever comes first.
    0.5r:      fractional R, e.g. "0.5r" moves the stop to entry once float
               profit reaches half the risk - useful when TP1 is far and the
               price often gives back shallow profits before TP.
    """
    risk = abs(entry - stop0)
    if risk <= 0:
        return False
    direction = 1 if side == "BUY" else -1
    progress = (mark - entry) * direction
    low = str(trigger or "").strip().lower()
    if low.endswith("r") and "or" not in low and len(low) > 1:
        try:
            factor = Decimal(low[:-1])
            if Decimal("0") < factor <= Decimal("1"):
                return progress >= risk * factor
        except Exception:
            pass  # invalid numeric trigger falls through to legacy options
    if low == "tp":
        return progress >= (target - entry) * direction
    if low == "1r_or_tp":
        return progress >= risk or progress >= (target - entry) * direction
    return False


# ---------------------------------------------------------------------------
# 账户快照轮询器 (account snapshot poller)
# ---------------------------------------------------------------------------
# Breakeven/TP/time-stop guards and structure-exit checks used to poll
# Binance per symbol every few seconds (N symbols x 2 requests / cycle), which
# is the largest request source behind shared-IP rate-limit bans. One
# background poller now keeps a process-wide snapshot via two batched
# requests per cycle; readers consult the snapshot first and only fall back
# to direct REST when no fresh snapshot exists (cold start / poll failure).

_SNAPSHOT_DEFAULT_POLL_SECONDS = 10.0
#: Snapshot freshness limit when no explicit stale_after_seconds is given:
#: auto = max(45s, 1.2 x poll period) so readers never treat the snapshot as
#: stale during the normal gap between cycles (a too-short stale limit made
#: guards fall back to per-symbol direct REST exactly between polls, which
#: amplified shared-IP rate-limit hits).
_SNAPSHOT_STALE_MIN_SECONDS = 45.0
_SNAPSHOT_STALE_AUTO_FACTOR = 1.2
#: +/- jitter applied to every poll cycle so wake-ups are staggered across
#: cycles (and against other testnet users on the shared egress IP).
_SNAPSHOT_POLL_JITTER_FRACTION = 0.2


class AccountSnapshotPoller:
    """Background reader keeping a process-wide account snapshot fresh.

    Each cycle issues two batched reads (all positions + all mark prices)
    instead of N per-symbol polls. Readers get best-effort values; on failure
    the previous snapshot is kept but ages, and readers fall back to direct
    REST once it goes stale.
    """

    def __init__(
        self,
        client: BinanceUSDMTestnetClient,
        *,
        poll_seconds: float = _SNAPSHOT_DEFAULT_POLL_SECONDS,
        stale_after_seconds: float | None = None,
        poll_period_provider: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._poll_seconds = float(poll_seconds)
        if stale_after_seconds is None:
            stale_after_seconds = max(
                _SNAPSHOT_STALE_MIN_SECONDS,
                float(poll_seconds) * _SNAPSHOT_STALE_AUTO_FACTOR,
            )
        self._stale_after = float(stale_after_seconds)
        # When set, the live period is re-read before every cycle (e.g. a WS
        # health probe: wide interval while the user-data stream is up, tight
        # interval while it is down so REST polling carries the load).
        self._poll_period_provider = poll_period_provider
        self._clock = clock
        self._positions: dict[str, dict[str, Any]] = {}
        self._marks: dict[str, Decimal] = {}
        self._marks_last_ok: float = 0.0
        self._last_ok: float = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background refresh loop (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="account-snapshot", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        period = (
            self._poll_period_provider()
            if self._poll_period_provider is not None
            else self._poll_seconds
        )
        while not self._stop.wait(
            period * (1.0 + random.uniform(0.0, _SNAPSHOT_POLL_JITTER_FRACTION))
        ):
            period = (
                self._poll_period_provider()
                if self._poll_period_provider is not None
                else self._poll_seconds
            )
            if rate_limiter.is_banned():
                # 共享 IP 封禁期间完全不请求（熔断器到期后下一周期自然恢复），
                # 避免给封禁续命；熔断状态由请求层在错误抛出处统一记录。
                continue
            self.refresh()

    def refresh(self) -> None:
        """Pull one fresh snapshot; failures keep the previous data."""
        marks_ok = positions_ok = False
        marks: dict[str, Decimal] = {}
        positions: dict[str, dict[str, Any]] = {}
        try:
            marks = self._client.all_mark_prices()
            marks_ok = True
        except Exception as exc:  # rate-limit bans were already recorded upstream
            logger.warning("Account snapshot mark pull failed: %s", exc)
        try:
            positions = self._client.all_positions()
            positions_ok = True
        except Exception as exc:  # rate-limit bans were already recorded upstream
            logger.warning("Account snapshot position pull failed: %s", exc)
        if marks_ok and positions_ok:
            with self._lock:
                self._marks = marks
                self._positions = positions
                self._last_ok = self._clock()

    def _current_period(self) -> float:
        """Live poll period for this moment (provider-aware)."""
        if self._poll_period_provider is not None:
            try:
                return float(self._poll_period_provider())
            except (TypeError, ValueError):
                return self._poll_seconds
        return self._poll_seconds

    def snapshot_ready(self, now: float | None = None) -> bool:
        """True when a recent successful snapshot exists (age <= stale limit).

        With a dynamic poll period the freshness limit follows the live
        period, otherwise the snapshot would go stale during every long cycle
        and send all readers back to direct REST (the very burst the shared
        poller exists to avoid).
        """
        if self._last_ok <= 0:
            return False
        now = self._clock() if now is None else now
        stale_after = max(
            self._stale_after,
            self._current_period() * _SNAPSHOT_STALE_AUTO_FACTOR,
        )
        return (now - self._last_ok) <= stale_after

    def position(self, symbol: str) -> dict[str, Any] | None:
        """Snapshot row for symbol (amount/entry); None == flat or unknown."""
        with self._lock:
            row = self._positions.get(symbol)
            return dict(row) if row is not None else None

    def mark_price(self, symbol: str) -> Decimal | None:
        with self._lock:
            return self._marks.get(symbol)

    def update_mark_price(self, symbol: str, price: Decimal) -> None:
        """Push one mark price (from the public markPrice stream)."""
        with self._lock:
            self._marks[symbol] = price
            self._marks_last_ok = self._clock()

    def mark_price_fresh(
        self, symbol: str, *, max_age_seconds: float = 15.0, now: float | None = None
    ) -> Decimal | None:
        """Mark pushed recently by the WS channel, else None.

        Guards prefer this over the snapshot when the REST snapshot is not
        fresh (WS mark ticks every ~3s; REST positions refresh far less often
        while the user-data stream is healthy).
        """
        with self._lock:
            if self._marks_last_ok <= 0:
                return None
            current = self._clock() if now is None else now
            if current - self._marks_last_ok > max_age_seconds:
                return None
            return self._marks.get(symbol)


# Process-wide singleton: one poller per monitor process, shared by the guard
# threads and the structure-exit checks (module-level state keeps the wiring
# explicit, same as the rate-limit breaker).
_snapshot_poller: AccountSnapshotPoller | None = None
_snapshot_poller_started: bool = False
_snapshot_lock = threading.Lock()


def start_account_snapshot_poller(
    *,
    api_key: str,
    api_secret: str,
    poll_seconds: float = _SNAPSHOT_DEFAULT_POLL_SECONDS,
    stale_after_seconds: float | None = None,
    poll_period_provider: Callable[[], float] | None = None,
    base_url: str | None = None,
) -> bool:
    """Start the shared poller; returns True only when it newly started."""
    global _snapshot_poller, _snapshot_poller_started
    with _snapshot_lock:
        if _snapshot_poller_started and _snapshot_poller is not None:
            return False
        try:
            client = BinanceUSDMTestnetClient(api_key, api_secret, base_url=base_url)
        except ValueError as exc:
            logger.error("Cannot start account snapshot poller: %s", exc)
            return False
        poller = AccountSnapshotPoller(
            client,
            poll_seconds=poll_seconds,
            stale_after_seconds=stale_after_seconds,
            poll_period_provider=poll_period_provider,
        )
        poller.start()
        _snapshot_poller = poller
        _snapshot_poller_started = True
        return True


def stop_account_snapshot_poller() -> None:
    """Stop and drop the shared poller (idempotent)."""
    global _snapshot_poller, _snapshot_poller_started
    with _snapshot_lock:
        poller, _snapshot_poller = _snapshot_poller, None
        _snapshot_poller_started = False
    if poller is not None:
        poller.stop()


def account_snapshot_poller() -> AccountSnapshotPoller | None:
    return _snapshot_poller


def current_position(client: BinanceUSDMTestnetClient, symbol: str) -> dict[str, Any]:
    """Best available position row: fresh snapshot first, else direct REST."""
    poller = _snapshot_poller
    if poller is not None and poller.snapshot_ready():
        return poller.position(symbol) or {"amount": Decimal("0"), "entry": None}
    _raise_if_banned()
    return client.position_info(symbol)


def current_mark_price(client: BinanceUSDMTestnetClient, symbol: str) -> Decimal:
    """Best available mark price: WS push, then fresh snapshot, else REST."""
    poller = _snapshot_poller
    if poller is not None:
        fresh_mark = getattr(poller, "mark_price_fresh", None)
        pushed = fresh_mark(symbol) if fresh_mark is not None else None
        if pushed is not None:
            return pushed
        if poller.snapshot_ready():
            mark = poller.mark_price(symbol)
            if mark is not None:
                return mark
    _raise_if_banned()
    return client.mark_price(symbol)


def _register_guard(symbol: str, record: dict[str, Any]) -> None:
    _STATE_STORE.guard_put(symbol, record)


def _read_guard(symbol: str) -> dict[str, Any] | None:
    return _STATE_STORE.guard_get(symbol)


def _patch_guard(symbol: str, **patch: Any) -> None:
    _STATE_STORE.guard_patch(symbol, **patch)

def _drop_guard(symbol: str) -> None:
    """Remove the guard/runner record for ``symbol`` (position is gone)."""
    _STATE_STORE.guard_drop(symbol)

def _settle_after_entry_gone(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    quantity: Decimal | None,
    client_id: str,
    signal_id: str,
    *,
    context: str,
    config: BinanceUSDMTestnetSettings | None = None,
    conf: float | None = None,
    target2: Decimal | None = None,
) -> None:
    """A limit entry is gone (timed out / cancelled / ended) but may still
    hold a partial position. Protect whatever is open using the recorded
    SL/TP; when protection cannot be attached, roll the position back so an
    automatically-created position is never left unprotected (P0-4).
    """
    try:
        open_qty = client.net_position(symbol)
    except BinanceAPIError as exc:
        logger.error(
            "Cannot inspect %s position after %s (%s): %s", symbol, context, client_id, exc
        )
        _drop_pending(symbol, client_id)
        return
    if open_qty == 0:
        _drop_pending(symbol, client_id)
        return
    # Only protect a position whose direction matches this entry's side: a
    # same-symbol position opened by some other order must keep its own levels.
    if (side == "BUY" and open_qty < 0) or (side == "SELL" and open_qty > 0):
        logger.info(
            f"{active_environment().label_en} %s: open %s position (%s) is opposite to %s; leaving it to its own protection",
            context,
            symbol,
            _decimal_text(open_qty),
            side,
        )
        _drop_pending(symbol, client_id)
        return
    try:
        sl_algo_id, tp_algo_id, partial_qty = _attach_protection(
            client, symbol, side, stop, target,
            quantity=abs(open_qty),
            target2=target2,
            partial_pct=
                float(config.tp_partial_close_pct) if config is not None else 0.0,
        )
    except BinanceAPIError as exc:
        _drop_pending(symbol, client_id)
        try:
            close_qty = abs(open_qty) if quantity is None else min(quantity, abs(open_qty))
            client.close_market_position(
                symbol=symbol,
                side="SELL" if side == "BUY" else "BUY",
                quantity=close_qty,
            )
        except BinanceAPIError:
            logger.exception("Failed to close %s position left by %s", symbol, context)
        logger.error("%s left an open %s position (%s) without protection: %s", context, symbol, client_id, exc)
        return
    if signal_id:
        _remember_signal(signal_id)
    if config is not None:
        _maybe_guard(
            client, config, symbol, side, stop, target, sl_algo_id, conf,
            quantity=abs(open_qty),
            target2=target2,
            tp_algo_id=tp_algo_id,
            partial_qty=partial_qty,
        )
    _drop_pending(symbol, client_id)
    logger.info(
        f"{active_environment().label_en} %s; open %s position (%s) protected with SL/TP",
        context,
        symbol,
        client_id,
    )


def _cancel_and_settle(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    client_id: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    quantity: Decimal | None,
    signal_id: str,
    *,
    context: str,
    config: BinanceUSDMTestnetSettings | None = None,
    conf: float | None = None,
    target2: Decimal | None = None,
) -> None:
    """Cancel the resting remainder of a timed-out entry, then protect or
    roll back any partial fill. When the cancel itself fails the pending
    record is kept so a later signal or a restart resume can still recover.
    """
    entry_price = _pending_entry_price(symbol, client_id)
    try:
        client.cancel_order(symbol=symbol, client_id=client_id)
    except BinanceAPIError as exc:
        _record_cancel_event(
            symbol,
            client_id,
            cancel_log.REASON_CANCEL_FAILED,
            detail=str(exc),
            entry_price=entry_price,
            signal_id=signal_id,
        )
        logger.error(
            "Cancel %s limit entry failed for %s (%s): %s; pending record kept",
            context,
            symbol,
            client_id,
            exc,
        )
        return
    reason = (
        cancel_log.REASON_LIMIT_ENTRY_TIMEOUT
        if "timed out" in context
        else cancel_log.REASON_STALE_ENTRY_REMOVED
    )
    _record_cancel_event(
        symbol,
        client_id,
        reason,
        detail=context,
        entry_price=entry_price,
        signal_id=signal_id,
    )
    _remember_canceled_entry(symbol, entry_price, reason)
    logger.info(
        f"{active_environment().label_en} limit entry %s; resting remainder canceled: %s %s", context, symbol, client_id
    )
    _settle_after_entry_gone(
        client,
        symbol,
        side,
        stop,
        target,
        quantity,
        client_id,
        signal_id,
        context=context,
        config=config,
        conf=conf,
        target2=target2,
    )


def _watch_limit_entry(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    client_id: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    target2: Decimal | None = None,
    quantity: Decimal,
    signal_id: str,
    timeout_seconds: float,
    poll_interval: float,
    conf: float | None = None,
    config: BinanceUSDMTestnetSettings | None = None,
) -> None:
    """Poll a resting limit entry; attach TP/SL on full fill, and protect or
    roll back any partial fill when the order ends (timeout / cancel).
    """
    deadline = time.monotonic() + timeout_seconds
    wake = _register_watcher_wake(client_id)
    while True:
        try:
            status = client.order_status(symbol=symbol, client_id=client_id)
        except BinanceAPIError as exc:
            logger.warning(f"{active_environment().label_en} limit fill check failed for %s: %s", symbol, exc)
            if time.monotonic() >= deadline:
                _cancel_and_settle(
                    client,
                    symbol,
                    client_id,
                    side,
                    stop,
                    target,
                    quantity,
                    signal_id,
                    context="timed out after status failures",
                    config=config,
                    conf=conf,
                    target2=target2,
                )
                _unregister_watcher_wake(client_id)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            wake.clear()
            wake.wait(min(poll_interval, remaining))
            continue
        if status == "FILLED":
            try:
                sl_algo_id, tp_algo_id, partial_qty = _attach_protection(
                    client, symbol, side, stop, target,
                    quantity=quantity,
                    target2=target2,
                    partial_pct=
                        float(config.tp_partial_close_pct) if config is not None else 0.0,
                )
            except BinanceAPIError as exc:
                _drop_pending(symbol, client_id)
                try:
                    client.close_market_position(
                        symbol=symbol,
                        side="SELL" if side == "BUY" else "BUY",
                        quantity=quantity,
                    )
                except BinanceAPIError:
                    logger.exception("Failed to close filled limit position for %s", symbol)
                logger.error("Limit entry filled but protection failed for %s: %s", symbol, exc)
                _unregister_watcher_wake(client_id)
                return
            _remember_signal(signal_id)
            if config is not None:
                _maybe_guard(
                    client, config, symbol, side, stop, target, sl_algo_id, conf,
                    quantity=quantity,
                    target2=target2,
                    tp_algo_id=tp_algo_id,
            partial_qty=partial_qty,
                )
            _drop_pending(symbol, client_id)
            logger.info(f"{active_environment().label_en} limit entry filled and protected: %s %s", symbol, client_id)
            _unregister_watcher_wake(client_id)
            return
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            # The entry may already have filled partially before it ended
            # (e.g. cancelled from the exchange UI): never abandon that.
            _settle_after_entry_gone(
                client,
                symbol,
                side,
                stop,
                target,
                quantity,
                client_id,
                signal_id,
                context=f"order ended ({status})",
                config=config,
                conf=conf,
                target2=target2,
            )
            _unregister_watcher_wake(client_id)
            return
        if time.monotonic() >= deadline:
            _cancel_and_settle(
                client,
                symbol,
                client_id,
                side,
                stop,
                target,
                quantity,
                signal_id,
                context="timed out",
                config=config,
                conf=conf,
                target2=target2,
            )
            _unregister_watcher_wake(client_id)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        wake.clear()
        wake.wait(min(poll_interval, remaining))


def resume_pending_limit_watchers(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm fill watchers for resting limit entries recorded before a restart.

    Watcher threads do not survive a process restart: without this, a resting
    GTC entry could fill later with nobody to attach its TP/SL. Each pending
    record is re-watched with the remaining fill window; entries whose window
    already expired are settled immediately by the watcher (cancel the resting
    remainder, protect or roll back any partial fill).

    Returns the number of watchers resumed.
    """
    configure_binance_environment(settings)
    config = binance_env.active_cfg(settings)
    if not config.enabled or config.dry_run or config.emergency_stop:
        return 0
    with _STATE_LOCK:
        raw_pending = _STATE_STORE.pending_all()
        records = {
            symbol: record
            for symbol, record in raw_pending.items()
            if isinstance(record, dict)
        } if isinstance(raw_pending, dict) else {}
    if not records:
        return 0
    try:
        active_client = client or BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    except ValueError as exc:
        logger.error(f"Cannot resume {active_environment().label_en} limit watchers: %s", exc)
        return 0
    resumed = 0
    for symbol, record in records.items():
        client_id = str(record.get("client_id") or "")
        signal_id = str(record.get("signal_id") or "")
        side = str(record.get("side") or "")
        stop = _positive_decimal(record.get("stop"))
        target = _positive_decimal(record.get("target"))
        target2 = _positive_decimal(record.get("target2"))
        quantity = _positive_decimal(record.get("quantity"))
        conf = _parse_win_rate(record.get("conf"))
        placed_at = record.get("placed_at")
        if (
            not client_id
            or side not in ("BUY", "SELL")
            or stop is None
            or target is None
            or quantity is None
        ):
            logger.warning(f"Dropping incomplete {active_environment().label_en} pending record for %s", symbol)
            _drop_pending(symbol, client_id)
            continue
        timeout_seconds = float(config.limit_fill_timeout_minutes * 60)
        if isinstance(placed_at, (int, float)):
            timeout_seconds = max(0.0, timeout_seconds - max(0.0, time.time() - placed_at))
        watcher = threading.Thread(
            target=_watch_limit_entry,
            kwargs={
                "client": active_client,
                "symbol": symbol,
                "client_id": client_id,
                "side": side,
                "stop": stop,
                "target": target,
                "target2": target2,
                "quantity": quantity,
                "signal_id": signal_id,
                "conf": conf,
                "config": config,
                "timeout_seconds": timeout_seconds,
                "poll_interval": config.limit_poll_interval_seconds,
            },
            daemon=True,
        )
        watcher.start()
        resumed += 1
        logger.info(
            f"Resumed {active_environment().label_en} limit fill watcher for %s %s (remaining %.0fs)",
            symbol,
            client_id,
            timeout_seconds,
        )
    return resumed


def _daily_pnl_aggregate(
    rows: list[dict[str, Any]], tz_hours: float = 8
) -> list[dict[str, float | str]]:
    """Group raw /fapi/v1/income rows by local day (UTC+*tz_hours*).

    Returns per-day totals of REALIZED_PNL / COMMISSION / FUNDING_FEE plus the
    net sum, ordered oldest-first. Days without any income rows are omitted.
    """
    tz = timezone(timedelta(hours=tz_hours))
    by_day: dict[str, dict[str, float]] = {}
    for row in rows:
        day = datetime.fromtimestamp(int(row["time"]) / 1000, tz).strftime("%Y-%m-%d")
        by_day.setdefault(day, {})
        bucket = by_day[day]
        kind = str(row.get("incomeType") or "")
        if kind in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            bucket[kind] = bucket.get(kind, 0.0) + float(row.get("income") or 0.0)
    out: list[dict[str, float | str]] = []
    for day in sorted(by_day):
        bucket = by_day[day]
        realized = bucket.get("REALIZED_PNL", 0.0)
        commission = bucket.get("COMMISSION", 0.0)
        funding = bucket.get("FUNDING_FEE", 0.0)
        out.append(
            {
                "date": day,
                "realized_pnl": realized,
                "commission": commission,
                "funding_fee": funding,
                "net": realized + commission + funding,
            }
        )
    return out


def report_daily_pnl(
    *,
    days: int = 10,
    tz_hours: float = 8,
    csv_path: str | None = None,
    client: BinanceUSDMTestnetClient | None = None,
    settings: Settings | None = None,
) -> list[dict[str, float | str]]:
    """Print (and optionally export) realized P&L per local day.

    Read-only: pulls the account income ledger and groups it by day. *net* is
    realized + commission + funding (actual bottom line). Returns the rows for
    programmatic use.
    """
    configure_binance_environment(settings)
    active_client = client
    if active_client is None:
        config = binance_env.active_cfg(settings)
        if not config.api_key or not config.api_secret:
            raise ValueError(
                f"Binance {active_environment().label_en} API key/secret missing in settings.json"
            )
        active_client = BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    tz = timezone(timedelta(hours=tz_hours))
    now = datetime.now(tz)
    day_start = datetime(now.year, now.month, now.day, tzinfo=tz)
    start_ms = int((day_start - timedelta(days=days - 1)).timestamp() * 1000)
    rows = active_client.income_history(start_ms=start_ms)
    summary = _daily_pnl_aggregate(rows, tz_hours=tz_hours)

    header = ["date", "realized_pnl", "commission", "funding_fee", "net"]
    if csv_path:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=header)
            writer.writeheader()
            for item in summary:
                writer.writerow(item)
        print(f"已导出 CSV: {csv_path}")
    print(
        f"{'日期':<12}{'实现盈亏':>14}{'手续费':>12}{'资金费':>12}{'净合计':>14}"
    )
    for item in summary:
        print(
            f"{item['date']:<12}"
            f"{item['realized_pnl']:>+14.4f}"
            f"{item['commission']:>+12.4f}"
            f"{item['funding_fee']:>+12.4f}"
            f"{item['net']:>+14.4f}"
        )
    return summary


def _signal_id(symbol: str, decision: dict[str, Any]) -> str:
    material = {
        "symbol": symbol,
        "direction": decision.get("order_direction"),
        "type": decision.get("order_type"),
        "entry": decision.get("entry_price"),
        "stop": decision.get("stop_loss_price"),
        "target": decision.get("take_profit_price"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _load_state() -> dict[str, Any]:
    """Load the runtime execution state file (delegates to the state store)."""
    return _STATE_STORE.load()


def _save_state(state: dict[str, Any]) -> None:
    """Atomically persist the runtime execution state file (state store)."""
    _STATE_STORE.save(state)


def _is_recent_signal(signal_id: str, cooldown_minutes: int) -> bool:
    """Return whether a successfully-submitted plan is still in cooldown."""
    seen_at = _STATE_STORE.seen_get(signal_id)
    return seen_at is not None and time.time() - seen_at < cooldown_minutes * 60


def _is_missing_order_error(exc: BinanceAPIError) -> bool:
    """Return whether Binance explicitly reported error code -2013.

    -2013 arrives in two shapes depending on the endpoint: an HTTP 400 with a
    JSON body (``Binance HTTP 400: {"code":-2013,...}``) for order queries, or
    a 200 body with an error code (``Binance error -2013: ...``). Match either.
    """
    message = str(exc).lower()
    return (
        "binance error -2013" in message
        or '"code":-2013' in message
        or '"code": -2013' in message
    )


def _is_missing_algo_order_error(exc: BinanceAPIError) -> bool:
    """Return whether the Algo service reports the conditional order is gone.

    The cancel endpoint answers -2011 "Unknown order sent" once an order was
    already removed (successful earlier cancel / restart residue / manual
    cancel); -2013 shapes may also appear. Treat these as "cancel done": the
    caller must re-hang a replacement stop instead of aborting the move and
    leaving the position unprotected.
    """
    message = str(exc).lower()
    return (
        "-2011" in message
        or "unknown order" in message
        or _is_missing_order_error(exc)
    )



def _remember_signal(signal_id: str) -> None:
    """Persist only after all entry and protective orders were accepted."""
    _STATE_STORE.seen_put(signal_id, time.time())


def _persist_pending(symbol: str, entry: dict[str, Any]) -> None:
    """Record a resting limit entry that is awaiting fill."""
    _STATE_STORE.pending_put(symbol, entry)


def _drop_pending(symbol: str, client_id: str | None = None) -> None:
    """Remove the pending record for ``symbol`` unless it belongs to another order."""
    _STATE_STORE.pending_drop(symbol, client_id)


def _tick_size(exchange_info: dict[str, Any]) -> Decimal | None:
    """Return the PRICE_FILTER tick size, or None when it is unusable."""
    filters = exchange_info.get("filters") if isinstance(exchange_info, dict) else None
    for item in filters or []:
        if isinstance(item, dict) and item.get("filterType") == "PRICE_FILTER":
            return _positive_decimal(item.get("tickSize"))
    return None


def _remember_canceled_entry(symbol: str, entry_price: object, reason: str) -> None:
    """Anchor the repricing cooldown at the price of the entry just canceled."""
    price = _positive_decimal(entry_price)
    if price is None:
        return
    try:
        _STATE_STORE.canceled_entry_put(
            symbol,
            entry=_decimal_text(price),
            reason=reason,
            ts=time.time(),
        )
    except BinanceAPIError as exc:
        # Not being able to persist the anchor only weakens the churn guard;
        # it must never fail an execution path.
        logger.warning("Cannot persist repricing cooldown anchor for %s: %s", symbol, exc)


def _repricing_cooldown_reason(
    symbol: str,
    entry_price: Decimal,
    exchange_info: dict[str, Any],
    config: BinanceUSDMTestnetSettings | None,
) -> str | None:
    """Return why this level must not be re-hung yet (None means allowed).

    A new analysis round can cancel a resting entry and immediately propose an
    entry a couple of ticks away; that only pays spread and fees. After a
    cancel, the same price band stays off limits for the cooldown window.
    """
    if config is None:
        return None
    max_ticks = int(config.limit_repricing_min_ticks or 0)
    cooldown_minutes = int(config.limit_repricing_cooldown_minutes or 0)
    if max_ticks <= 0 or cooldown_minutes <= 0:
        return None
    record = _STATE_STORE.canceled_entries_all().get(symbol)
    if not isinstance(record, dict):
        return None
    canceled_at = record.get("ts")
    if not isinstance(canceled_at, (int, float)):
        return None
    age_seconds = time.time() - canceled_at
    if age_seconds >= cooldown_minutes * 60:
        return None
    previous = _positive_decimal(record.get("entry"))
    tick = _tick_size(exchange_info)
    if previous is None or tick is None:
        return None
    ticks_apart = abs(entry_price - previous) / tick
    if ticks_apart > max_ticks:
        return None
    return (
        f"同价位轮换冷却：上一笔挂单在 {max(0, int(age_seconds // 60))} 分钟前撤销，"
        f"本轮入场价与它相差 {ticks_apart:.0f} 跳（阈值 {max_ticks} 跳），暂不重挂"
    )


def _pending_entry_price(symbol: str, client_id: str) -> str | None:
    """Return the recorded entry price of a resting entry, when still known."""
    record = _STATE_STORE.pending_get(symbol)
    if not isinstance(record, dict) or record.get("client_id") != client_id:
        return None
    entry = record.get("entry")
    return None if entry is None else str(entry)


def _record_cancel_event(
    symbol: str,
    client_id: str,
    reason: str,
    *,
    detail: str = "",
    entry_price: object = None,
    signal_id: str = "",
) -> None:
    """Mirror one cancel into the append-only audit trail.

    The audit trail is best effort: a failure here must never abort or alter
    an order flow, so every exception is logged and swallowed.
    """
    try:
        cancel_log.record_cancel(
            symbol=symbol,
            client_id=client_id,
            reason=reason,
            detail=detail,
            environment=active_environment().label_en,
            entry_price=entry_price,
            signal_id=signal_id,
        )
    except Exception:
        # Best effort by contract: a broken audit trail must not fail a cancel.
        logger.exception("Cancel audit record failed for %s %s", symbol, client_id)


def _dict_response(value: dict[str, Any] | list[Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BinanceAPIError("Unexpected Binance response")
    return value


def _side_from_decision(value: object) -> str | None:
    text = str(value or "").lower()
    if any(token in text for token in ("多", "long", "buy", "bull")):
        return "BUY"
    if any(token in text for token in ("空", "short", "sell", "bear")):
        return "SELL"
    return None


def _parse_win_rate(value: object) -> float | None:
    """Parse estimated_win_rate (number, '61%', or '0.61') into 0-100, or None."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if 0.0 <= number <= 1.0:
        return number * 100.0
    return number


def _measured_win_rate_override(
    conf: float | None, config: BinanceUSDMTestnetSettings
) -> float | None:
    """Measured win rate for conf's 5-point bucket when the feedback gate is on.

    Data comes from trade_records/conf_buckets.json (offline fapi rebuild and
    CSV join, written by tools/trade_pnl_report.py). Returns None when feedback
    is off, conf is unknown, the file is absent/broken, or the bucket sample is
    below conf_feedback_min_samples: the caller keeps the model estimate then
    (fail-open, zero behaviour change).
    """
    if str(config.conf_feedback_mode) != "on" or conf is None:
        return None
    try:
        with open(_CONF_BUCKETS_PATH, encoding="utf-8") as handle:
            buckets = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(buckets, dict):
        return None
    entry = buckets.get(str(int(conf) // 5 * 5))
    if not isinstance(entry, dict):
        return None
    try:
        total = int(entry.get("n") or 0)
        wins = int(entry.get("wins") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or total < max(5, int(config.conf_feedback_min_samples or 0)):
        return None
    return wins / total


def _positive_decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result > 0 else None


def _quantity_for_notional(
    notional: float, price: Decimal, exchange_info: dict[str, Any]
) -> Decimal | None:
    if price <= 0 or notional <= 0:
        return None
    filters = {item.get("filterType"): item for item in exchange_info.get("filters", [])}
    lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
    if not isinstance(lot, dict):
        return None
    step = Decimal(str(lot["stepSize"]))
    minimum = Decimal(str(lot["minQty"]))
    desired = Decimal(str(notional)) / price
    quantity = (desired / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity < minimum:
        return None
    min_notional = filters.get("MIN_NOTIONAL", {}).get("notional", "0")
    if quantity * price < Decimal(str(min_notional)):
        return None
    return quantity


def _quantity_for_risk(
    risk_usdt: float, anchor: Decimal, stop: Decimal, exchange_info: dict[str, Any]
) -> Decimal | None:
    """Size a position so that risk_usdt is lost if price reaches *stop*.

    quantity = risk / |anchor - stop|, floored to LOT_SIZE steps. Anchor is the
    expected fill price (mark for market entries, the resting limit price
    otherwise). Returns None on invalid input or unrepresentable size.
    """
    if risk_usdt <= 0 or anchor <= 0 or stop <= 0 or anchor == stop:
        return None
    filters = {item.get("filterType"): item for item in exchange_info.get("filters", [])}
    lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
    if not isinstance(lot, dict):
        return None
    step = Decimal(str(lot["stepSize"]))
    minimum = Decimal(str(lot["minQty"]))
    gap = abs(anchor - stop)
    desired = Decimal(str(risk_usdt)) / gap
    quantity = (desired / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity < minimum:
        return None
    min_notional = filters.get("MIN_NOTIONAL", {}).get("notional", "0")
    if quantity * anchor < Decimal(str(min_notional)):
        return None
    return quantity


def _partial_quantity(
    quantity: Decimal, pct: float, exchange_info: dict[str, Any]
) -> Decimal | None:
    """Floor the TP1 partial-close size to the LOT_SIZE step size.

    Returns None when partial TP1 is off (pct<=0), not representable on the
    exchange (below minQty) or equal to the whole position (pct=100 keeps the
    legacy full-close TP1 behaviour). None also means the caller must fall
    back to a close-all TP1 order so the position never lacks a take-profit.
    """
    if pct <= 0 or quantity <= 0:
        return None
    filters = {item.get("filterType"): item for item in exchange_info.get("filters", [])}
    lot = filters.get("LOT_SIZE")
    if not isinstance(lot, dict):
        return None
    step = Decimal(str(lot["stepSize"]))
    minimum = Decimal(str(lot["minQty"]))
    share = quantity * Decimal(str(pct)) / Decimal("100")
    partial = (share / step).to_integral_value(rounding=ROUND_DOWN) * step
    if partial <= 0 or partial < minimum or partial >= quantity:
        return None
    return partial


def _price_for_tick(price: Decimal, exchange_info: dict[str, Any]) -> Decimal:
    """Round a trigger price down to the symbol's PRICE_FILTER tick size."""
    filters = {item.get("filterType"): item for item in exchange_info.get("filters", [])}
    price_filter = filters.get("PRICE_FILTER")
    if not isinstance(price_filter, dict):
        return price
    tick_size = Decimal(str(price_filter.get("tickSize", "0")))
    if tick_size <= 0:
        return price
    return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


# 30d daily-trend guard state: symbol -> (local date of fetch, pct change)
_TREND_CACHE: dict[str, tuple[str, float]] = {}


def _trend_pct_from_closes(closes: list[float]) -> float:
    """Percent change from the first to the last daily close in the series."""
    return (closes[-1] / closes[0] - 1.0) * 100.0


def _trend_bucket(pct: float | None, neutral_pct: float) -> str | None:
    """Classify a daily-trend percent into bull/bear; None inside the neutral band."""
    if pct is None or abs(pct) <= neutral_pct:
        return None
    return "bull" if pct > 0 else "bear"


def _daily_trend_pct(
    client: BinanceUSDMTestnetClient, symbol: str, days: int
) -> float | None:
    """Daily-close trend percent for ``symbol``, cached once per local day.

    Fail-open: any fetch problem returns None so trading is never blocked by
    trend data being unavailable (the guard then treats the market as neutral).
    """
    today = time.strftime("%Y%m%d")
    cached = _TREND_CACHE.get(symbol)
    if cached is not None and cached[0] == today:
        return cached[1]
    try:
        closes = client.daily_close_series(symbol, days)
        pct = _trend_pct_from_closes(closes)
    except BinanceAPIError as exc:
        logger.warning("Cannot fetch daily trend for %s: %s", symbol, exc)
        return None
    _TREND_CACHE[symbol] = (today, pct)
    return pct
