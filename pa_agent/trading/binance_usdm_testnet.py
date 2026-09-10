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
_STATE_LOCK = threading.Lock()
# Per-symbol transition locks: serialise breakeven-stop moves and TP2 swaps
# that run on separate daemon threads for the same symbol (guard vs runner).
_MANAGER_LOCKS: dict[str, threading.Lock] = {}
_MANAGER_LOCKS_GUARD = threading.Lock()

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


def _manager_lock(symbol: str) -> threading.Lock:
    """Return the transition lock guarding this symbol's SL/TP swaps."""
    with _MANAGER_LOCKS_GUARD:
        lock = _MANAGER_LOCKS.get(symbol)
        if lock is None:
            lock = threading.Lock()
            _MANAGER_LOCKS[symbol] = lock
        return lock


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


def execute_market_signal(
    decision: dict[str, Any],
    settings: Settings | None,
    *,
    analysis_symbol: str = "",
    client: BinanceUSDMTestnetClient | None = None,
    trend_30d_pct: float | None = None,
) -> ExecutionResult:
    """Execute one validated market signal (one-shot; no rate-limit retries).

    Binance Testnet shares public egress IPs and frequently answers HTTP 418
    (code -1003, IP banned). The request layer now refuses to send anything
    while a ban is live, and retrying a rate-limited submission only extends
    the penalty, so rate-limit failures are NOT retried - every other failure
    also stays one-shot. Re-entry is safe because the first attempt never
    records the signal on a failed path and the open-position guard blocks
    duplicate entries.
    """
    return _execute_market_signal_once(
        decision,
        settings,
        analysis_symbol=analysis_symbol,
        client=client,
        trend_30d_pct=trend_30d_pct,
    )


def _execute_market_signal_once(
    decision: dict[str, Any],
    settings: Settings | None,
    *,
    analysis_symbol: str = "",
    client: BinanceUSDMTestnetClient | None = None,
    trend_30d_pct: float | None = None,
) -> ExecutionResult:
    """Execute one validated market signal, with mandatory TP/SL protection."""
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
        # 30d daily-trend guard (whitelisted symbols only): a signal against the
        # daily-close trend of the symbol over the last trend_30d_days needs higher
        # confidence and, when allowed, runs at reduced leverage/size.
        leverage = config.leverage
        trend_note = ""
        guard_active = (
            config.counter_trend_min_confidence > 0 or config.counter_trend_size_scale < 1.0
        )
        if guard_active:
            trend_pct = trend_30d_pct
            if trend_pct is None:
                trend_pct = _daily_trend_pct(active_client, symbol, config.trend_30d_days)
            bucket = _trend_bucket(trend_pct, config.trend_30d_neutral_pct)
            counter = (side == "BUY" and bucket == "bear") or (side == "SELL" and bucket == "bull")
            if counter:
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
                    leverage = max(1, round(config.leverage * config.counter_trend_size_scale))
                    trend_note = (
                        f"; counter-trend 30d ({trend_pct:+.1f}%) scaled leverage to {leverage}x"
                    )
        # 保证金恒定：名义价值 = 保证金(margin_usdt) × 杠杆，杠杆变化不影响保证金。
        margin_usdt = float(config.max_notional_usdt)
        risk_usdt = float(config.risk_per_trade_usdt or 0.0)
        if risk_usdt > 0:
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
        active_client.set_leverage(symbol, leverage)
        entry = active_client.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            client_id=_entry_client_id(signal_id),
        )
        target2 = _positive_decimal(decision.get("take_profit_price_2"))
        try:
            sl_algo_id, tp_algo_id, partial_qty = _attach_protection(
                active_client, symbol, side, stop, target,
                quantity=quantity, target2=target2,
                partial_pct=float(config.tp_partial_close_pct or 0.0),
            )
        except BinanceAPIError:
            # Never leave an unprotected automatically-created position.
            active_client.close_market_position(
                symbol=symbol, side="SELL" if side == "BUY" else "BUY", quantity=quantity
            )
            raise
        _remember_signal(signal_id)
        _maybe_guard(
            active_client,
            config,
            symbol,
            side,
            stop,
            target,
            sl_algo_id,
            signal_conf,
            quantity=quantity,
            target2=target2,
            tp_algo_id=tp_algo_id,
            partial_qty=partial_qty,
        )
        return ExecutionResult(
            "submitted",
            f"{active_environment().label_en} entry and protective orders submitted" + trend_note,
            symbol,
            _decimal_text(quantity),
            str(entry.get("orderId", "")),
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
        client.set_leverage(symbol, leverage)
        entry = client.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            client_id=_entry_client_id(signal_id),
        )
        target2 = _positive_decimal(decision.get("take_profit_price_2"))
        try:
            sl_algo_id, tp_algo_id, partial_qty = _attach_protection(
                client, symbol, side, stop, target,
                quantity=quantity, target2=target2,
                partial_pct=float(config.tp_partial_close_pct or 0.0),
            )
        except BinanceAPIError:
            client.close_market_position(
                symbol=symbol,
                side="SELL" if side == "BUY" else "BUY",
                quantity=quantity,
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
            "Limit entry crossed mark price; submitted market entry and protective orders"
            + trend_note,
            symbol,
            _decimal_text(quantity),
            str(entry.get("orderId", "")),
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
    with _STATE_LOCK:
        pending = _load_state().get("pending")
        old = pending.get(symbol) if isinstance(pending, dict) else None
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
    with _STATE_LOCK:
        state = _load_state()
        guards = state.get("guards")
        if not isinstance(guards, dict):
            guards = {}
            state["guards"] = guards
        guards[symbol] = record
        _save_state(state)


def _read_guard(symbol: str) -> dict[str, Any] | None:
    with _STATE_LOCK:
        guards = _load_state().get("guards")
    if not isinstance(guards, dict):
        return None
    record = guards.get(symbol)
    return dict(record) if isinstance(record, dict) else None


def _patch_guard(symbol: str, **patch: Any) -> None:
    with _STATE_LOCK:
        state = _load_state()
        guards = state.get("guards")
        if not isinstance(guards, dict):
            return
        record = guards.get(symbol)
        if not isinstance(record, dict):
            return
        record.update(patch)
        _save_state(state)

def _drop_guard(symbol: str) -> None:
    """Remove the guard/runner record for ``symbol`` (position is gone)."""
    with _STATE_LOCK:
        state = _load_state()
        guards = state.get("guards")
        if isinstance(guards, dict) and symbol in guards:
            del guards[symbol]
            _save_state(state)

# ---------------------------------------------------------------------------
# 止损单补挂校验 (resting-order verification & re-hang)
# ---------------------------------------------------------------------------
# 背景(2026-09-08 事故): TP1 部分止盈/保本移动阶段, 记录指向的 algo 止损单
# 已被撤/已死(精度 bug 挂新失败后旧单早已撤掉), 程序只信注册表不查实单,
# 持仓裸奔数小时. 以下助手在每次"信任 resting 单"前 GET /fapi/v1/algoOrder
# 校验; 单已死则按候选价补挂并回写注册表, 绝不裸奔.

#: Algo 服务中只有 NEW 表示条件单仍在等待触发(实测: NEW=挂单中, CANCELED=已撤).
_ALGO_LIVE_STATUSES = frozenset({"NEW"})


def _algo_status_live(status: str) -> bool:
    """True 仅当 algo 订单仍在 resting (NEW)。"""
    return status in _ALGO_LIVE_STATUSES


def _rehang_stop_candidates(
    *,
    side: str,
    entry: Decimal,
    stop0: Decimal,
    mark: Decimal,
    stage_tp2: bool,
    floor_pct: float,
) -> list[Decimal]:
    """Ordered protective stop prices to try when the recorded stop is gone.

    TP2/trigger stage prefers the breakeven price (entry); the plain phase
    prefers the original static stop (stop0). Candidates whose trigger is
    already satisfied (-2021 immediate-trigger) are dropped. When nothing is
    placeable, a fresh stop at min_stop_distance_pct outside the mark is
    appended last so a breached-but-unprotected position still gets downside
    protection instead of running naked.
    """
    exit_side = "SELL" if side == "BUY" else "BUY"
    candidates: list[Decimal] = []
    if stage_tp2:
        candidates.extend((entry, stop0))
    else:
        candidates.extend((stop0, entry))
    if floor_pct and floor_pct > 0:
        factor = Decimal(str(floor_pct)) / Decimal("100")
        if side == "BUY":
            candidates.append(mark * (Decimal("1") - factor))
        else:
            candidates.append(mark * (Decimal("1") + factor))
    return [
        price
        for price in candidates
        if price is not None
        and price > 0
        and not _stop_would_immediately_trigger(exit_side, price, mark)
    ]


#: 补挂逐候选尝试时, 仅这类业务拒绝视为"该价位挂不了"换下一候选; 网络/限流等
#: 瞬时错误原样上抛, 由调用方限次重试(不许把止损悄悄降级到更差价位).
_REHANG_SKIP_MARKERS = ('"-1111"', '"-2021"', "-1111", "-2021")


def _stop_resting_alive(client: "BinanceUSDMTestnetClient", client_algo_id: str) -> bool:
    """True when the algo stop really rests on the exchange (NEW).

    Terminal/missing states (CANCELED/EXPIRED/triggered/unknown id) return
    False; transient transport/rate-limit errors re-raise for caller retry.
    """
    try:
        payload = client.algo_order_status(client_algo_id=client_algo_id)
        status = payload.get("algoStatus") if isinstance(payload, dict) else None
    except BinanceAPIError as exc:
        if _is_missing_algo_order_error(exc):
            return False
        raise
    return _algo_status_live(str(status or ""))


def _rehang_protective_stop(
    client: "BinanceUSDMTestnetClient",
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    stop0: Decimal,
    stage_tp2: bool,
    floor_pct: float,
) -> tuple[bool, str, str]:
    """Place the best placeable replacement STOP for a missing protective stop.

    Tries every candidate from _rehang_stop_candidates in order; business
    rejections (-1111 precision / -2021 immediate) move to the next candidate,
    transient errors raise. Returns (ok, new_stop_algo_id, note).
    """
    exit_side = "SELL" if side == "BUY" else "BUY"
    mark = current_mark_price(client, symbol)
    for price in _rehang_stop_candidates(
        side=side, entry=entry, stop0=stop0, mark=mark,
        stage_tp2=stage_tp2, floor_pct=floor_pct,
    ):
        candidate_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
        try:
            client.place_close_algo_order(
                symbol=symbol,
                side=exit_side,
                order_type="STOP_MARKET",
                stop_price=price,
                client_algo_id=candidate_id,
            )
        except BinanceAPIError as exc:
            message = str(exc)
            if any(marker in message for marker in _REHANG_SKIP_MARKERS):
                logger.warning(
                    "Re-hang candidate %s rejected for %s (%s); trying next",
                    _decimal_text(price),
                    symbol,
                    exc,
                )
                continue
            raise
        return True, candidate_id, f"re-hung protective stop at {_decimal_text(price)}"
    return (
        False,
        "",
        "no placeable re-hang candidate (mark=" + _decimal_text(mark) + ")",
    )


def _ensure_protective_stop(
    client: "BinanceUSDMTestnetClient",
    *,
    symbol: str,
    record: dict[str, Any],
    entry: Decimal,
    stage_tp2: bool,
    floor_pct: float,
) -> tuple[str, str, str]:
    """Verify the recorded protective stop really rests; re-hang when gone.

    Returns (status, stop_algo_id, note) with status one of:
      alive  - recorded stop verified resting (NEW); nothing to do
      rehung - recorded stop was gone; replacement placed and registry updated
      error  - transient API failure / no placeable candidate (caller retries)
    The caller must hold the per-symbol manager lock.
    """
    side = str(record.get("side") or "")
    current_stop = str(record.get("stop_algo_id") or "")
    if side not in ("BUY", "SELL") or not current_stop:
        return "error", "", "invalid guard record"
    try:
        alive = _stop_resting_alive(client, current_stop)
    except BinanceAPIError as exc:
        return "error", current_stop, f"algo status query failed: {exc}"
    if alive:
        return "alive", current_stop, ""
    stop0 = _positive_decimal(record.get("stop0"))
    if stop0 is None:
        return "error", current_stop, "guard record missing stop0"
    try:
        ok, new_stop, note = _rehang_protective_stop(
            client,
            symbol=symbol,
            side=side,
            entry=entry,
            stop0=stop0,
            stage_tp2=stage_tp2,
            floor_pct=floor_pct,
        )
    except BinanceAPIError as exc:
        return "error", current_stop, f"re-hang placement failed: {exc}"
    if not ok:
        return "error", current_stop, note
    _patch_guard(symbol, stop_algo_id=new_stop)
    return "rehung", new_stop, note


def _swap_stop_to_price(
    client: "BinanceUSDMTestnetClient",
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    live_stop_id: str,
    bridge_qty: Decimal,
) -> tuple[str, str, str]:
    """Replace a resting closePosition STOP with one at entry (breakeven).

    Binance allows only ONE open closePosition order per direction and order
    class (-4130: "An open stop or take profit order with GTE and closePosition
    in the direction is existing"), so a naive place-new-then-cancel-old swap
    is rejected while the old STOP still rests, while a cancel-then-place swap
    leaves the position unprotected between the two calls. This helper bridges
    the move with a reduceOnly+quantity STOP that coexists with a closePosition
    order (same shape the TP1 partial order uses next to the entry STOP):

      1. hang bridge reduceOnly+qty STOP at entry (no -4130, no naked gap)
      2. cancel the old closePosition STOP
      3. hang the canonical closePosition STOP at entry (old gone -> legal)
      4. cancel the bridge

    From step 1 on the position is always protected at entry; any step may
    leave the bridge resting (also at entry), which is strictly better than
    the old stop and never leaves the position naked. A step-1 failure raises
    with nothing changed (old closePosition STOP still protects) so callers can
    keep the original stop and retry later.

    Returns (status, stop_algo_id, note):
      "ok"   - canonical closePosition STOP at entry resting; bridge removed
      "kept" - bridge reduceOnly STOP at entry resting (old closePosition stop
               may also still rest); safe overlap, caller logs and moves on
    Raises BinanceAPIError when nothing changed and the old stop still protects.

    Must be called with the per-symbol manager lock held (callers do).
    """
    if side not in ("BUY", "SELL") or not live_stop_id:
        raise BinanceAPIError("invalid stop swap request")
    if bridge_qty is None or bridge_qty <= 0:
        raise BinanceAPIError(
            "cannot swap stop to entry without a live position amount"
        )
    exit_side = "SELL" if side == "BUY" else "BUY"

    def _hang_cp_stop() -> str:
        stop_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="STOP_MARKET",
            stop_price=entry,
            client_algo_id=stop_id,
        )
        return stop_id

    # 1) bridge: reduceOnly+qty STOP coexists with the old closePosition STOP.
    bridge_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
    try:
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="STOP_MARKET",
            stop_price=entry,
            client_algo_id=bridge_id,
            quantity=bridge_qty,
            close_position=False,
        )
    except BinanceAPIError:
        # Nothing changed: the old closePosition STOP still protects.
        raise
    # 2) old closePosition STOP is now redundant.
    try:
        client.cancel_algo_order(client_algo_id=live_stop_id)
    except BinanceAPIError as exc:
        if not _is_missing_algo_order_error(exc):
            # Cancel failed: the old stop may still rest. Try the canonical
            # closePosition STOP anyway - it only succeeds when the old one is
            # really gone (-4130 otherwise), so no state is lost either way.
            try:
                canonical_id = _hang_cp_stop()
            except BinanceAPIError as cp_exc:
                return (
                    "kept",
                    bridge_id,
                    f"old stop {live_stop_id} cancel failed ({exc}); bridge STOP "
                    f"at entry kept, canonical placement also failed: {cp_exc}",
                )
            # Old stop really gone: drop the bridge.
            try:
                client.cancel_algo_order(client_algo_id=bridge_id)
            except BinanceAPIError as b_exc:
                if not _is_missing_algo_order_error(b_exc):
                    logger.warning(
                        "Stop swap: bridge %s cancel failed for %s (%s); "
                        "canonical stop %s is in place, bridge may still rest",
                        bridge_id, symbol, b_exc, canonical_id,
                    )
            return "ok", canonical_id, ""
    # 3) old stop gone (cancelled or already missing): hang the canonical stop.
    try:
        canonical_id = _hang_cp_stop()
    except BinanceAPIError as exc:
        return (
            "kept",
            bridge_id,
            f"canonical closePosition STOP placement failed ({exc}); bridge "
            f"STOP at entry kept",
        )
    # 4) bridge no longer needed.
    try:
        client.cancel_algo_order(client_algo_id=bridge_id)
    except BinanceAPIError as exc:
        if not _is_missing_algo_order_error(exc):
            logger.warning(
                "Stop swap: bridge %s cancel failed for %s (%s); canonical stop "
                "%s is in place, bridge may still rest",
                bridge_id, symbol, exc, canonical_id,
            )
    return "ok", canonical_id, ""


def _breakeven_guard_loop(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    trigger: str,
    poll_seconds: float,
    *,
    floor_pct: float = 0.0,
) -> None:
    """Poll an open position; once float profit reaches the trigger, replace
    the resting STOP algo order with one at the entry price (breakeven).
    Exits when the position is closed or the stop has been moved.
    """
    consecutive_errors = 0
    while True:
        record = _read_guard(symbol)
        if record is None or record.get("moved"):
            return
        side = str(record.get("side") or "")
        stop0 = _positive_decimal(record.get("stop0"))
        target = _positive_decimal(record.get("target"))
        stop_algo_id = str(record.get("stop_algo_id") or "")
        if side not in ("BUY", "SELL") or stop0 is None or target is None or not stop_algo_id:
            return
        try:
            info = current_position(client, symbol)
            amount = info["amount"]
            entry = info["entry"]
            if amount == 0 or entry is None:
                return  # position closed (TP/SL/manual) - nothing to protect
            if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
                return  # not our position anymore
            mark = current_mark_price(client, symbol)
        except BinanceAPIError as exc:
            if _is_rate_limit_reason(str(exc)):
                # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数。
                _guard_rate_limit_wait(poll_seconds)
                continue
            consecutive_errors += 1
            logger.warning(
                "Breakeven guard poll failed for %s (attempt %d): %s",
                symbol,
                consecutive_errors,
                exc,
            )
            if consecutive_errors >= 5:
                logger.error(
                    "Breakeven guard gave up for %s after repeated API errors; "
                    "original stop stays in place",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if not _guard_trigger_reached(
            mark=mark,
            entry=entry,
            stop0=stop0,
            target=target,
            side=side,
            trigger=trigger,
        ):
            consecutive_errors = 0
            time.sleep(poll_seconds)
            continue
        # 移损临界区: 与 TP runner 互斥, 锁内重读注册表防双撤双挂。
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None or fresh.get("moved"):
                return
            live_stop = str(fresh.get("stop_algo_id") or "")
            if not live_stop:
                return
            exit_side = "SELL" if side == "BUY" else "BUY"
            if _stop_would_immediately_trigger(exit_side, entry, mark):
                # 浮盈已回吐(价格回到入场另一侧): 保本 STOP 会立即触发被拒(-2021),
                # 原止损保留; 回到主循环等价格重新满足移损条件, 避免反复失败刷屏。
                logger.info(
                    "Breakeven guard: mark %s already through entry %s for %s; "
                    "keeping original stop %s",
                    _decimal_text(mark),
                    _decimal_text(entry),
                    symbol,
                    live_stop,
                )
                consecutive_errors = 0
                time.sleep(poll_seconds)
                continue
            # 交易所只许一个同方向 closePosition STOP resting(-4130), 先挂新后撤旧
            # 会被拒绝; _swap_stop_to_price 用 reduceOnly+qty 桥接单保底换单, 全程
            # 持仓有保护, 无裸奔窗口。桥接挂不上(旧单仍在场)走下方校验/重试。
            try:
                swap_status, new_stop_id, swap_note = _swap_stop_to_price(
                    client,
                    symbol=symbol,
                    side=side,
                    entry=entry,
                    live_stop_id=live_stop,
                    bridge_qty=abs(amount),  # 空单 amount 为负: reduceOnly 数量必须取正
                )
            except BinanceAPIError as exc:
                # 挂保本失败并不代表安全: 若记录指向的旧止损其实已死(历史 bug/
                # 手动撤单/精度错误), "原止损 stays in place" 就是裸奔。先校验
                # 再决定限次重试(旧单活着)还是立即补挂(旧单已死)。
                guard_status, guard_stop, guard_note = _ensure_protective_stop(
                    client,
                    symbol=symbol,
                    record=fresh,
                    entry=entry,
                    stage_tp2=True,
                    floor_pct=floor_pct,
                )
                if guard_status == "error":
                    consecutive_errors += 1
                    logger.error(
                        "Breakeven stop placement failed for %s (%s) and "
                        "re-hang verification failed: %s",
                        symbol,
                        stop_algo_id,
                        guard_note,
                    )
                    if consecutive_errors >= 5:
                        logger.error(
                            "Breakeven guard gave up for %s after repeated errors; "
                            "record kept for restart resume",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                if guard_status == "rehung":
                    logger.warning(
                        "Breakeven guard: original stop %s for %s was gone; %s",
                        stop_algo_id,
                        symbol,
                        guard_note,
                    )
                    _patch_guard(symbol, moved=True, stop_algo_id=guard_stop)
                    return
                consecutive_errors += 1
                logger.error(
                    "Breakeven stop placement failed for %s (%s), original stop "
                    "kept resting: %s",
                    symbol,
                    stop_algo_id,
                    exc,
                )
                if consecutive_errors >= 5:
                    logger.error(
                        "Breakeven guard gave up placing breakeven stop for %s after "
                        "repeated errors; original stop stays in place",
                        symbol,
                    )
                    return
                time.sleep(poll_seconds)
                continue
            if swap_status == "kept":
                # 桥接单仍在场(撤旧未遂或正式单被拒): 保本保护已由桥接单落地,
                # 记录指向桥接单即可; 旧 closePosition 单若仍在场由交易所清理。
                logger.warning("Breakeven guard: %s", swap_note)
            _patch_guard(symbol, moved=True, stop_algo_id=new_stop_id)
            logger.info(
                "Breakeven stop moved to entry for %s %s (trigger=%s mark=%s)",
                symbol,
                side,
                trigger,
                _decimal_text(mark),
            )
            return


def _tp_runner_loop(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    poll_seconds: float,
    floor_pct: float = 0.0,
) -> None:
    """Poll a position protected by a partial (reduceOnly) TP1 order.

    The TP1 leg only closes partial_qty, so this thread watches the position
    amount: once it shrinks (TP1 fired) the original SL is cancelled (unless
    the breakeven guard already moved it) and a close-all TAKE_PROFIT at TP2
    is hung so the remainder can run to the far target. When the position is
    fully closed the resting partial TP1 is cancelled and the record removed.
    """
    consecutive_errors = 0
    while True:
        record = _read_guard(symbol)
        if record is None or record.get("partial_done"):
            return
        side = str(record.get("side") or "")
        qty = _positive_decimal(record.get("qty"))
        partial_qty = _positive_decimal(record.get("partial_qty"))
        stop_algo_id = str(record.get("stop_algo_id") or "")
        tp_algo_id = str(record.get("tp_algo_id") or "")
        target2 = _positive_decimal(record.get("target2"))
        if (
            side not in ("BUY", "SELL")
            or qty is None
            or partial_qty is None
            or target2 is None
            or not stop_algo_id
            or not tp_algo_id
        ):
            return  # 旧 guard 记录(无 partial 字段)或残缺记录: 不归 runner 管
        try:
            info = current_position(client, symbol)
            amount = info["amount"]
            entry = info["entry"]
        except BinanceAPIError as exc:
            if _is_rate_limit_reason(str(exc)):
                # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数。
                _guard_rate_limit_wait(poll_seconds)
                continue
            consecutive_errors += 1
            logger.warning(
                "TP runner poll failed for %s (attempt %d): %s",
                symbol,
                consecutive_errors,
                exc,
            )
            if consecutive_errors >= 5:
                logger.error(
                    "TP runner gave up for %s after repeated API errors; "
                    "partial TP1 stays in place",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
            return  # not our position anymore
        if amount == 0:
            # 仓位已清: 撤掉可能残留的 TP1 部分单后移除记录。
            try:
                client.cancel_algo_order(client_algo_id=tp_algo_id)
            except BinanceAPIError as exc:
                if _is_missing_algo_order_error(exc):
                    logger.warning(
                        "TP runner: partial TP1 %s for %s already gone (%s)",
                        tp_algo_id,
                        symbol,
                        exc,
                    )
                else:
                    consecutive_errors += 1
                    logger.error(
                        "TP runner cleanup failed for %s (partial TP1 %s): %s",
                        symbol,
                        tp_algo_id,
                        exc,
                    )
                    if consecutive_errors >= 5:
                        logger.error(
                            "TP runner gave up cleaning %s after repeated errors; "
                            "record kept so a restart resume can retry the cancel",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                    time.sleep(poll_seconds)
                    continue
            _drop_guard(symbol)
            logger.info("TP runner: %s position closed; cleared partial TP1 %s",
                symbol, tp_algo_id,
            )
            return
        if entry is None:
            return  # 无法取得入场价, 保本价格无从谈起
        # positionRisk 返回带符号 positionAmt(空单为负), qty 记的是正数量:
        # 必须比绝对值, 否则空单永远不等, 首轮就误判 TP1 已半平(2026-09-10 事故)。
        if abs(amount) == qty:
            consecutive_errors = 0
            time.sleep(poll_seconds)
            continue
        # TP1 半仓已触发(|amount| < qty): 进入 TP2 阶段。
        # 临界区: 与保本 guard 的移损互斥(per-symbol 锁), 锁内重读注册表, 防双撤双挂。
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None or fresh.get("partial_done"):
                return
            moved = bool(fresh.get("moved"))
            current_stop = str(fresh.get("stop_algo_id") or "")
            current_tp = str(fresh.get("tp_algo_id") or "")
            if not current_stop or not current_tp:
                return
            exit_side = "SELL" if side == "BUY" else "BUY"
            try:
                mark = current_mark_price(client, symbol)
            except BinanceAPIError:
                mark = None  # 预检尽力而为: 拿不到 mark 则保持原行为
            # 先清可能仍 resting 的 TP1 部分单(已成交时 -2011/-2013 视为已清):
            # 手动减仓等场景不能让孤儿部分单对新仓位生效。
            try:
                client.cancel_algo_order(client_algo_id=current_tp)
            except BinanceAPIError as exc:
                if _is_missing_algo_order_error(exc):
                    logger.warning(
                        "TP runner: partial TP1 %s for %s already gone (%s)",
                        current_tp,
                        symbol,
                        exc,
                    )
                else:
                    consecutive_errors += 1
                    logger.error(
                        "TP runner partial TP1 cancel failed for %s (%s): %s",
                        symbol,
                        current_tp,
                        exc,
                    )
                    if consecutive_errors >= 5:
                        logger.error(
                            "TP runner gave up clearing TP1 for %s after repeated "
                            "errors; original stop stays in place",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
            if moved or (mark is not None and _stop_would_immediately_trigger(
                exit_side, entry, mark
            )):
                # 保本不再重挂(guard 已移 / mark 已回吐穿 entry)。进 TP2 前先
                # 校验记录指向的止损单真实 resting: 2026-09-08 事故中记录仍指向
                # 早已撤掉的单, 程序却"保留原止损"裸奔数小时 → 已死必须补挂。
                if not moved:
                    logger.info(
                        "TP runner: mark %s already through entry %s for %s; "
                        "verifying protective stop %s",
                        _decimal_text(mark),
                        _decimal_text(entry),
                        symbol,
                        current_stop,
                    )
                    moved = True
                    _patch_guard(symbol, moved=True)
                runner_status, runner_stop, runner_note = _ensure_protective_stop(
                    client,
                    symbol=symbol,
                    record=fresh,
                    entry=entry,
                    stage_tp2=True,
                    floor_pct=floor_pct,
                )
                if runner_status == "error":
                    consecutive_errors += 1
                    logger.error(
                        "TP runner: protective-stop verification failed for %s "
                        "(%s): %s",
                        symbol,
                        current_stop,
                        runner_note,
                    )
                    if consecutive_errors >= 5:
                        logger.error(
                            "TP runner gave up verifying stop for %s after repeated "
                            "errors; record kept for restart resume",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                if runner_status == "rehung":
                    logger.warning(
                        "TP runner: recorded stop %s for %s was gone; %s",
                        current_stop,
                        symbol,
                        runner_note,
                    )
                new_stop_id = runner_stop
            else:
                # C1: 同方向只许一个 closePosition STOP resting(-4130), 保本移动
                # 不能先挂新后撤旧。挂新前先校验旧单真实 resting: 旧单已死时直接
                # 补挂(entry 优先); 活着则经 reduceOnly+qty 桥接单换至 entry, 全程
                # 持仓有保护, 无裸奔窗口。
                runner_status, runner_stop, runner_note = _ensure_protective_stop(
                    client,
                    symbol=symbol,
                    record=fresh,
                    entry=entry,
                    stage_tp2=True,
                    floor_pct=floor_pct,
                )
                if runner_status == "error":
                    consecutive_errors += 1
                    logger.error(
                        "TP runner: protective-stop verification failed for %s "
                        "(%s): %s",
                        symbol,
                        current_stop,
                        runner_note,
                    )
                    if consecutive_errors >= 5:
                        logger.error(
                            "TP runner gave up verifying stop for %s after repeated "
                            "errors; record kept for restart resume",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                if runner_status == "rehung":
                    logger.warning(
                        "TP runner: recorded stop %s for %s was gone; %s",
                        current_stop,
                        symbol,
                        runner_note,
                    )
                    new_stop_id = runner_stop
                    moved = True  # 替代单已挂(entry 优先), 无需再撤旧
                else:
                    try:
                        swap_status, new_stop_id, swap_note = _swap_stop_to_price(
                            client,
                            symbol=symbol,
                            side=side,
                            entry=entry,
                            live_stop_id=current_stop,
                            bridge_qty=abs(amount),
                        )
                    except BinanceAPIError as exc:
                        consecutive_errors += 1
                        logger.error(
                            "TP runner breakeven stop placement failed for %s "
                            "(kept original stop): %s",
                            symbol,
                            exc,
                        )
                        if consecutive_errors >= 5:
                            logger.error(
                                "TP runner gave up placing breakeven stop for %s after "
                                "repeated errors; original stop stays in place",
                                symbol,
                            )
                            return
                        time.sleep(poll_seconds)
                        continue
                    if swap_status == "kept":
                        # 桥接单仍在场(撤旧未遂或正式单被拒): 保本保护已由桥接单落地。
                        logger.warning("TP runner: %s", swap_note)
                    moved = True  # 撤旧已由桥接流程处理
            # 保本单已落地(或 guard 已完成): 立即回写注册表, 记录始终指向真实存在的单。
            _patch_guard(symbol, moved=True, stop_algo_id=new_stop_id)
            new_tp_id = f"pa-tp-{uuid.uuid4().hex[:24]}"
            try:
                client.place_close_algo_order(
                    symbol=symbol,
                    side=exit_side,
                    order_type="TAKE_PROFIT_MARKET",
                    stop_price=target2,
                    client_algo_id=new_tp_id,
                )
            except BinanceAPIError as exc:
                consecutive_errors += 1
                logger.error(
                    "TP runner TP2 placement failed for %s: %s",
                    symbol,
                    exc,
                )
                if consecutive_errors >= 5:
                    logger.error(
                        "TP runner gave up placing TP2 for %s after repeated errors; "
                        "record kept for restart resume",
                        symbol,
                    )
                    return
                time.sleep(poll_seconds)
                continue
            _patch_guard(
                symbol,
                moved=True,
                stop_algo_id=new_stop_id,
                tp_algo_id=new_tp_id,
                partial_done=True,
            )
            logger.info(
                "TP runner: %s %s half closed; breakeven stop + TP2=%s in place",
                symbol,
                side,
                _decimal_text(target2),
            )
            return


def _timestop_deadline_hit(
    record: dict[str, Any] | None, stop_minutes: float, *, now: float | None = None
) -> bool:
    """True when the recorded position has been open past the time-stop."""
    if not isinstance(record, dict) or stop_minutes <= 0:
        return False
    ts = record.get("ts")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False
    return (now if now is not None else time.time()) - float(ts) >= stop_minutes * 60


def _timestop_loop(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    stop_minutes: float,
    poll_seconds: float,
) -> None:
    """Close the remaining position once it aged past stop_minutes.

    Runs alongside the breakeven guard and TP1 runner (all read the same
    registry record under the per-symbol lock). After closing, a resting
    partial TP1 order is cancelled and the record dropped unless an unfinished
    runner still owns that lifecycle (it then observes the flat position on
    its next poll and cleans up itself).
    """
    errors = 0
    while True:
        record = _read_guard(symbol)
        if record is None or not _timestop_deadline_hit(record, stop_minutes):
            time.sleep(poll_seconds)
            continue
        side = str(record.get("side") or "")
        if side not in ("BUY", "SELL"):
            return
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None or str(fresh.get("side") or "") != side:
                return
            if not _timestop_deadline_hit(fresh, stop_minutes):
                continue  # record replaced by a newer position: re-arm on its deadline
            try:
                info = current_position(client, symbol)
                amount = info["amount"]
            except BinanceAPIError as exc:
                if _is_rate_limit_reason(str(exc)):
                    # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数。
                    _guard_rate_limit_wait(poll_seconds)
                    continue
                errors += 1
                logger.warning(
                    "Time-stop poll failed for %s (attempt %d): %s",
                    symbol, errors, exc,
                )
                if errors >= 5:
                    logger.error(
                        "Time-stop gave up for %s after repeated API errors; "
                        "static stop/TP stays in place",
                        symbol,
                    )
                    return
                time.sleep(poll_seconds)
                continue
            exit_side = "SELL" if side == "BUY" else "BUY"
            partial_pending = (
                _positive_decimal(fresh.get("partial_qty")) is not None
                and not bool(fresh.get("partial_done"))
            )
            if amount != 0:
                if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
                    return  # not our position anymore
                try:
                    client.close_market_position(
                        symbol=symbol,
                        side=exit_side,
                        quantity=abs(amount),
                    )
                except BinanceAPIError as exc:
                    errors += 1
                    logger.error(
                        "Time-stop close failed for %s (%s): %s",
                        symbol, _decimal_text(abs(amount)), exc,
                    )
                    if errors >= 5:
                        logger.error(
                            "Time-stop gave up closing %s after repeated errors; "
                            "static stop/TP stays in place",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                logger.info(
                    "Time-stop reached for %s %s: closed remaining %s",
                    symbol, side, _decimal_text(abs(amount)),
                )
            if partial_pending:
                # runner 仍存活, 由它撤残留 TP1 并清理记录 (下一轮见 amount=0).
                return
            tp_algo_id = str(fresh.get("tp_algo_id") or "")
            if tp_algo_id:
                try:
                    client.cancel_algo_order(client_algo_id=tp_algo_id)
                except BinanceAPIError as exc:
                    if not _is_missing_algo_order_error(exc):
                        logger.error(
                            "Time-stop residual TP1 cancel failed for %s (%s): %s",
                            symbol, tp_algo_id, exc,
                        )
                        return
            _drop_guard(symbol)
            return


def _maybe_guard(
    client: BinanceUSDMTestnetClient,
    config: BinanceUSDMTestnetSettings,
    symbol: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    stop_algo_id: str,
    conf: float | None,
    *,
    quantity: Decimal | None = None,
    target2: Decimal | None = None,
    tp_algo_id: str | None = None,
    partial_qty: Decimal | None = None,
) -> None:
    """Register the open position and arm its managers when enabled.

    Two optional managers share one per-symbol registry record: the breakeven
    guard (moves the SL to entry once float profit hits its trigger) and the
    TP1 partial runner (moves the remainder to TP2 after the reduceOnly half
    closed). Either manager only starts when its feature applies.

    ``partial_qty`` comes from _attach_protection (single source of truth):
    the registration never re-queries LOT_SIZE so the manager state always
    matches the TP1 order that was actually placed.
    """
    pct = float(config.tp_partial_close_pct or 0.0)
    if pct <= 0:
        partial_qty = None  # 功能关闭时忽略调用方传入的部分计划
    guard_on = _guard_enabled(config, conf)
    partial_on = (
        partial_qty is not None
        and quantity is not None
        and target2 is not None
        and tp_algo_id is not None
    )
    if not guard_on and not partial_on:
        return
    record: dict[str, Any] = {
        "stop_algo_id": stop_algo_id,
        "stop0": _decimal_text(stop),
        "target": _decimal_text(target),
        "side": side,
        "conf": conf,
        "ts": time.time(),
        "moved": False,
    }
    if partial_on:
        record.update(
            {
                "tp_algo_id": str(tp_algo_id),
                "target2": _decimal_text(target2),
                "qty": _decimal_text(quantity),
                "partial_qty": _decimal_text(partial_qty),
                "partial_done": False,
            }
        )
    _register_guard(symbol, record)
    if guard_on:
        watcher = threading.Thread(
            target=_breakeven_guard_loop,
            kwargs={
                "client": client,
                "symbol": symbol,
                "trigger": str(config.breakeven_stop_trigger),
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        watcher.start()
        logger.info(
            "Breakeven guard started for %s %s (conf=%s)",
            symbol,
            side,
            conf if conf is not None else "-",
        )
    if partial_on:
        runner = threading.Thread(
            target=_tp_runner_loop,
            kwargs={
                "client": client,
                "symbol": symbol,
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        runner.start()
        logger.info(
            "TP partial runner started for %s %s (TP1 close %s%%, runner to TP2=%s)",
            symbol,
            side,
            str(config.tp_partial_close_pct).rstrip("0").rstrip("."),
            _decimal_text(target2),
        )
    # 每个已注册记录都配一个看护线程: breakeven guard 与 TP runner 只在自己的
    # 触发点核验止损, 触发前 resting STOP 若在交易所端消失, 持仓就没有兜底
    # (09-07 事故)。看护线程同时负责 runner 结束后的终态补挂。
    watchdog = threading.Thread(
        target=_stop_watchdog_loop,
        kwargs={
            "client": client,
            "symbol": symbol,
            "poll_seconds": float(config.breakeven_poll_seconds),
            "floor_pct": float(config.min_stop_distance_pct),
        },
        daemon=True,
    )
    watchdog.start()
    logger.info("Stop watchdog started for %s", symbol)
    time_stop_minutes = int(config.time_stop_minutes or 0)
    if time_stop_minutes > 0:
        ts_thread = threading.Thread(
            target=_timestop_loop,
            kwargs={
                "client": client,
                "symbol": symbol,
                "stop_minutes": float(time_stop_minutes),
                "poll_seconds": float(config.breakeven_poll_seconds),
            },
            daemon=True,
        )
        ts_thread.start()
        logger.info(
            "Time-stop manager started for %s %s (limit %d minutes)",
            symbol,
            side,
            time_stop_minutes,
        )
def resume_breakeven_guards(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm breakeven guard loops for registered open positions after restart.

    Returns the number of guards resumed. Guards that already moved their stop
    (or whose position is gone) exit immediately on their first poll.
    """
    configure_binance_environment(settings)
    config = binance_env.active_cfg(settings)
    if not config.enabled or config.dry_run or config.emergency_stop:
        return 0
    if str(config.breakeven_stop_trigger) == "off":
        return 0
    with _STATE_LOCK:
        guards = _load_state().get("guards")
        records = (
            {s: dict(r) for s, r in guards.items() if isinstance(r, dict)}
            if isinstance(guards, dict)
            else {}
        )
    if not records:
        return 0
    try:
        active = client or BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    except ValueError as exc:
        logger.error("Cannot resume breakeven guards: %s", exc)
        return 0
    resumed = 0
    for symbol, record in records.items():
        if record.get("moved"):
            continue
        conf = record.get("conf")
        if not _guard_enabled(config, conf):
            continue
        watcher = threading.Thread(
            target=_breakeven_guard_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "trigger": str(config.breakeven_stop_trigger),
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        watcher.start()
        resumed += 1
    return resumed


def resume_tp_runners(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm TP1 partial runners for unfinished records after a restart.

    Runner threads do not survive a process restart. Records with
    ``partial_done`` are skipped; a runner re-polled for the others exits
    quickly when the position is gone (cleaning the residual TP1) or swaps
    the remainder to TP2 when the partial leg fired while we were down.
    Legacy breakeven-only records (no partial fields) are left untouched for
    the guard resume path.

    Returns the number of runners resumed.
    """
    configure_binance_environment(settings)
    config = binance_env.active_cfg(settings)
    if not config.enabled or config.dry_run or config.emergency_stop:
        return 0
    if float(config.tp_partial_close_pct or 0.0) <= 0:
        return 0
    with _STATE_LOCK:
        guards = _load_state().get("guards")
        records = (
            {s: dict(r) for s, r in guards.items() if isinstance(r, dict)}
            if isinstance(guards, dict)
            else {}
        )
    if not records:
        return 0
    try:
        active = client or BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    except ValueError as exc:
        logger.error("Cannot resume TP partial runners: %s", exc)
        return 0
    resumed = 0
    for symbol, record in records.items():
        if record.get("partial_done"):
            continue
        tp_algo_id = str(record.get("tp_algo_id") or "")
        stop_algo_id = str(record.get("stop_algo_id") or "")
        if (
            _positive_decimal(record.get("qty")) is None
            or _positive_decimal(record.get("partial_qty")) is None
            or _positive_decimal(record.get("target2")) is None
            or not tp_algo_id
            or not stop_algo_id
        ):
            continue  # 旧 guard 记录/残缺记录: 留给 guard resume 处理
        runner = threading.Thread(
            target=_tp_runner_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        runner.start()
        resumed += 1
        logger.info("Resumed TP partial runner for %s", symbol)
    return resumed
def resume_time_stops(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm time-stop managers for registered records after a restart.

    Any record with a side and ts gets a manager (records at or past their
    deadline are closed on the first poll). Returns the resumed count.
    """
    configure_binance_environment(settings)
    config = binance_env.active_cfg(settings)
    if not config.enabled or config.dry_run or config.emergency_stop:
        return 0
    minutes = int(config.time_stop_minutes or 0)
    if minutes <= 0:
        return 0
    with _STATE_LOCK:
        guards = _load_state().get("guards")
        records = (
            {s: dict(r) for s, r in guards.items() if isinstance(r, dict)}
            if isinstance(guards, dict)
            else {}
        )
    if not records:
        return 0
    try:
        active = client or BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    except ValueError as exc:
        logger.error("Cannot resume time-stop managers: %s", exc)
        return 0
    resumed = 0
    for symbol, record in records.items():
        ts = record.get("ts")
        side = str(record.get("side") or "")
        if not isinstance(ts, (int, float)) or ts <= 0 or side not in ("BUY", "SELL"):
            continue
        thread = threading.Thread(
            target=_timestop_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "stop_minutes": float(minutes),
                "poll_seconds": float(config.breakeven_poll_seconds),
            },
            daemon=True,
        )
        thread.start()
        resumed += 1
        logger.info("Resumed time-stop manager for %s", symbol)
    return resumed


#: 未进入保本/TP2 阶段的持仓: breakeven guard 与 TP runner 只在各自触发点才
#: 核验止损, 触发前 resting STOP 若已在交易所端消失, 持仓整段无保护(2026-09-07
#: ZEC 事故)。本看护按此轮数间隔复核存活(轮询 10s 时约 60s 一次), 兼顾 Testnet
#: 共享 IP 限流。
_UNMOVED_STOP_VERIFY_TICKS = 6


def _stop_watchdog_loop(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    poll_seconds: float,
    floor_pct: float = 0.0,
) -> None:
    """Keep the recorded algo stop really resting on the exchange.

    Covers every stage of the position's life: records whose managers already
    finished (moved / partial_done / TP1-fired remainder) are verified every
    poll, while an unmoved record (breakeven and TP1 not reached yet) is
    verified every _UNMOVED_STOP_VERIFY_TICKS polls - before 2026-09-07
    nothing checked that leg at all, so a stop that died server-side left the
    position naked until the next restart. A missing stop is re-hung; when the
    position is flat the residual TP order is cancelled and the record dropped.
    """
    consecutive_errors = 0
    unmoved_ticks = 0
    while True:
        record = _read_guard(symbol)
        if record is None:
            return
        side = str(record.get("side") or "")
        qty = _positive_decimal(record.get("qty"))
        partial_qty = _positive_decimal(record.get("partial_qty"))
        if side not in ("BUY", "SELL"):
            return  # 残缺记录: 不归看护管
        legacy = qty is None or partial_qty is None
        try:
            info = current_position(client, symbol)
            amount = info["amount"]
            entry = info["entry"]
        except BinanceAPIError as exc:
            if _is_rate_limit_reason(str(exc)):
                # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数.
                _guard_rate_limit_wait(poll_seconds)
                continue
            consecutive_errors += 1
            logger.warning(
                "Stop watchdog poll failed for %s (attempt %d): %s",
                symbol,
                consecutive_errors,
                exc,
            )
            if consecutive_errors >= 5:
                logger.error(
                    "Stop watchdog gave up polling %s after repeated API errors; "
                    "record kept for restart resume",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
            return  # not our position anymore
        if amount == 0:
            # 仓位已平: 撤残留部分单/TP2 单后移除记录(撤单幂等, -2011 视为已清).
            tp_algo_id = str(record.get("tp_algo_id") or "")
            if tp_algo_id:
                try:
                    client.cancel_algo_order(client_algo_id=tp_algo_id)
                except BinanceAPIError as exc:
                    if not _is_missing_algo_order_error(exc):
                        consecutive_errors += 1
                        logger.error(
                            "Stop watchdog residual TP cancel failed for %s (%s): %s",
                            symbol,
                            tp_algo_id,
                            exc,
                        )
                        if consecutive_errors >= 5:
                            return
                        time.sleep(poll_seconds)
                        continue
            _drop_guard(symbol)
            logger.info("Stop watchdog: %s position closed; record dropped", symbol)
            return
        terminal = (
            bool(record.get("moved"))
            or bool(record.get("partial_done"))
            or (not legacy and abs(amount) < qty)
        )
        if not terminal:
            # 未到保本/TP1 触发点: guard 与 runner 只在自己的触发点核验止损, 触发
            # 前若 resting STOP 已在交易所端消失, 持仓整段无保护(09-07 事故)。
            # 慢频复核, 消失即按原止损价补挂。
            unmoved_ticks += 1
            if unmoved_ticks >= _UNMOVED_STOP_VERIFY_TICKS:
                unmoved_ticks = 0
                if entry is not None and _positive_decimal(record.get("stop0")) is not None:
                    with _manager_lock(symbol):
                        fresh = _read_guard(symbol)
                        if fresh is None:
                            return
                        current_stop = str(fresh.get("stop_algo_id") or "")
                        if not current_stop:
                            return
                        status, _rehung_id, note = _ensure_protective_stop(
                            client,
                            symbol=symbol,
                            record=fresh,
                            entry=entry,
                            stage_tp2=False,
                            floor_pct=floor_pct,
                        )
                    if status == "error":
                        consecutive_errors += 1
                        logger.error(
                            "Stop watchdog pre-move verify/re-hang failed for %s (%s): %s",
                            symbol,
                            current_stop,
                            note,
                        )
                        if consecutive_errors >= 5:
                            logger.error(
                                "Stop watchdog gave up polling %s after repeated API "
                                "errors before the stop move; record kept for restart resume",
                                symbol,
                            )
                            return
                    else:
                        consecutive_errors = 0
                        if status == "rehung":
                            logger.warning(
                                "Stop watchdog: %s pre-move recorded stop %s was gone; %s",
                                symbol,
                                current_stop,
                                note,
                            )
            time.sleep(poll_seconds)
            continue
        if entry is None:
            return  # 拿不到入场价, 保本价无从谈起
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None:
                return
            current_stop = str(fresh.get("stop_algo_id") or "")
            if not current_stop:
                return
            status, _rehung_id, note = _ensure_protective_stop(
                client,
                symbol=symbol,
                record=fresh,
                entry=entry,
                stage_tp2=True,
                floor_pct=floor_pct,
            )
        if status == "error":
            consecutive_errors += 1
            logger.error(
                "Stop watchdog verify/re-hang failed for %s (%s): %s",
                symbol,
                current_stop,
                note,
            )
            if consecutive_errors >= 5:
                logger.error(
                    "Stop watchdog gave up for %s after repeated errors; "
                    "record kept for restart resume",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if status == "rehung":
            logger.warning(
                "Stop watchdog: %s recorded stop %s was gone; %s",
                symbol,
                current_stop,
                note,
            )
        consecutive_errors = 0
        time.sleep(poll_seconds)


def resume_stop_watchdogs(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm protective-stop watchdogs after a restart.

    Runner/guard threads do not survive a restart. Records whose managers had
    already finished (partial_done / moved) or that sit mid-TP2 previously had
    NO watcher at all: a stop that died server-side (cancel race / precision
    bug / manual removal) left the position naked until the next restart.
    Unmoved records (breakeven / TP1 not reached) are armed too, because their
    managers only look at the stop once their own trigger fires.
    Returns the number of watchdogs resumed.
    """
    configure_binance_environment(settings)
    config = binance_env.active_cfg(settings)
    if not config.enabled or config.dry_run or config.emergency_stop:
        return 0
    with _STATE_LOCK:
        guards = _load_state().get("guards")
        records = (
            {s: dict(r) for s, r in guards.items() if isinstance(r, dict)}
            if isinstance(guards, dict)
            else {}
        )
    if not records:
        return 0
    try:
        active = client or BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    except ValueError as exc:
        logger.error("Cannot resume stop watchdogs: %s", exc)
        return 0
    resumed = 0
    for symbol, record in records.items():
        side = str(record.get("side") or "")
        if side not in ("BUY", "SELL"):
            continue
        thread = threading.Thread(
            target=_stop_watchdog_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        thread.start()
        resumed += 1
        logger.info("Resumed stop watchdog for %s", symbol)
    return resumed


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
        raw_pending = _load_state().get("pending")
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
    """Load the runtime execution state file (empty dict when absent)."""
    path = os.fspath(
        os.path.join(os.path.dirname(_RUNTIME_STATE_PATH), active_environment().state_file)
    )
    try:
        with open(path, encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise BinanceAPIError(f"Cannot read {active_environment().label_en} execution state") from exc
    return state if isinstance(state, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    """Atomically persist the runtime execution state file."""
    path = os.fspath(
        os.path.join(os.path.dirname(_RUNTIME_STATE_PATH), active_environment().state_file)
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
        os.replace(temp, path)
    except OSError as exc:
        # Fail closed: an unavailable state must not allow new orders.
        raise BinanceAPIError(f"Cannot persist {active_environment().label_en} execution state") from exc


def _is_recent_signal(signal_id: str, cooldown_minutes: int) -> bool:
    """Return whether a successfully-submitted plan is still in cooldown."""
    now = time.time()
    with _STATE_LOCK:
        seen = _load_state().get("seen")
    seen_at = seen.get(signal_id) if isinstance(seen, dict) else None
    return isinstance(seen_at, (int, float)) and now - seen_at < cooldown_minutes * 60


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
    with _STATE_LOCK:
        state = _load_state()
        seen = state.get("seen")
        if not isinstance(seen, dict):
            seen = {}
            state["seen"] = seen
        seen[signal_id] = time.time()
        _save_state(state)


def _persist_pending(symbol: str, entry: dict[str, Any]) -> None:
    """Record a resting limit entry that is awaiting fill."""
    with _STATE_LOCK:
        state = _load_state()
        pending = state.get("pending")
        if not isinstance(pending, dict):
            pending = {}
            state["pending"] = pending
        pending[symbol] = entry
        _save_state(state)


def _drop_pending(symbol: str, client_id: str | None = None) -> None:
    """Remove the pending record for ``symbol`` unless it belongs to another order."""
    with _STATE_LOCK:
        state = _load_state()
        pending = state.get("pending")
        if not isinstance(pending, dict):
            return
        record = pending.get(symbol)
        if record is None:
            return
        if client_id is not None and record.get("client_id") != client_id:
            return
        del pending[symbol]
        _save_state(state)


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
        with _STATE_LOCK:
            state = _load_state()
            entries = state.get("last_canceled_entries")
            if not isinstance(entries, dict):
                entries = {}
                state["last_canceled_entries"] = entries
            entries[symbol] = {
                "entry": _decimal_text(price),
                "reason": reason,
                "ts": time.time(),
            }
            _save_state(state)
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
    with _STATE_LOCK:
        entries = _load_state().get("last_canceled_entries")
    record = entries.get(symbol) if isinstance(entries, dict) else None
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
    with _STATE_LOCK:
        pending = _load_state().get("pending")
    record = pending.get(symbol) if isinstance(pending, dict) else None
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
