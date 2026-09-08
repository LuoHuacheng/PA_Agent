"""Binance USDⓈ-M user-data websocket stream (P0: event push over REST polling).

Why: the REST watchers/poller that keep order and account state fresh are the
main source of Binance requests. The user-data stream turns order/account
changes into push events, so polling only has to run as a low-frequency
reconciliation (event gaps after a disconnect are the caller's job: the
on_connected hook fires on every (re)connect and is the right place to run
a REST reconciliation).

Scope guardrails:
- This module only reads pushed events. Every order/account mutation still
  goes through the signed REST client (synchronous confirmations).
- No Binance host is hard-coded: the listen-key REST calls are injected
  (production wires the Testnet REST client, a future live wiring only swaps
  the injection), and the WS base URL is a constructor argument.

Handlers run on the websocket thread: keep them cheap.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

logger = logging.getLogger("pa_agent.trading.binance_user_data")

#: Default WS gateway for USDⓈ-M user data. Testnet listenKeys are issued by
#: the Testnet REST host; the stream gateway may differ for live trading, so
#: callers can override ws_base.
_DEFAULT_WS_BASE = "wss://fstream.binancefuture.com"

#: listenKey lifetime is 60 minutes without refresh; keepalive at 25 minutes
#: leaves a comfortable margin over network/clock skew.
_KEEPALIVE_INTERVAL_SECONDS = 25 * 60

#: websocket ping cadence (keeps NAT/proxies from idling the socket out).
_PING_INTERVAL_SECONDS = 20.0
_PING_TIMEOUT_SECONDS = 10.0

#: Reconnect backoff ladder (seconds); repeats the last value while stopped.
_RECONNECT_DELAYS_SECONDS = (1.0, 3.0, 10.0, 30.0, 60.0)


class UserDataEventHandlers:
    """Callbacks for user-data events. All optional.

    Handlers receive the decoded payload dict as pushed by Binance.
    """

    def __init__(
        self,
        *,
        on_order_update: Callable[[dict[str, Any]], None] | None = None,
        on_account_update: Callable[[dict[str, Any]], None] | None = None,
        on_other_event: Callable[[str, dict[str, Any]], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        on_listen_key_expired: Callable[[], None] | None = None,
    ) -> None:
        self.on_order_update = on_order_update
        self.on_account_update = on_account_update
        self.on_other_event = on_other_event
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.on_listen_key_expired = on_listen_key_expired

class BinanceUserDataStream:
    """User-data stream with listenKey lifecycle and auto-reconnect.

    Dependency injection keeps this unit-testable without any network:
    create_listen_key / keepalive_listen_key / close_listen_key wrap the
    signed REST calls and may be faked in tests.
    """

    def __init__(
        self,
        *,
        create_listen_key: Callable[[], str],
        keepalive_listen_key: Callable[[str], None],
        close_listen_key: Callable[[str], None] | None = None,
        handlers: UserDataEventHandlers | None = None,
        ws_base: str | None = None,
        keepalive_interval_seconds: float = _KEEPALIVE_INTERVAL_SECONDS,
        reconnect_delays: tuple[float, ...] = _RECONNECT_DELAYS_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not ws_base:
            ws_base = _DEFAULT_WS_BASE
        if not ws_base.startswith(("ws://", "wss://")):
            raise ValueError(f"invalid ws_base: {ws_base!r}")
        self._create = create_listen_key
        self._keepalive = keepalive_listen_key
        self._close = close_listen_key
        self._handlers = handlers or UserDataEventHandlers()
        self._ws_base = ws_base.rstrip("/")
        self._keepalive_interval = float(keepalive_interval_seconds)
        self._delays = tuple(float(d) for d in reconnect_delays)
        self._sleep = sleep

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._listen_key: str | None = None
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._reconnect_index = 0

    def start(self) -> bool:
        """Start the stream thread(s); True when newly started."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            key = self._ensure_listen_key()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_main,
                kwargs={"initial_key": key},
                name="binance-user-data",
                daemon=True,
            )
            self._thread.start()
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop,
                name="binance-user-data-keepalive",
                daemon=True,
            )
            self._keepalive_thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Signal stop and join the worker threads."""
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("ws close() raised during shutdown", exc_info=True)
        for thread in (self._thread, self._keepalive_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
        key = self._listen_key
        if key is not None and self._close is not None:
            try:
                self._close(key)
            except Exception:  # pragma: no cover - best effort
                logger.debug("close_listen_key failed during shutdown", exc_info=True)
        with self._lock:
            self._listen_key = None
        self._thread = None
        self._keepalive_thread = None

    def listen_key(self) -> str | None:
        with self._lock:
            return self._listen_key

    def _ensure_listen_key(self) -> str:
        """Return the current key, creating one when missing."""
        if self._listen_key is None:
            self._listen_key = self._create()
            logger.info("Opened user-data stream: listenKey acquired")
        return self._listen_key

    def _ws_url(self, key: str) -> str:
        return self._ws_base + "/ws/" + key

    def _next_reconnect_delay(self) -> float:
        index = min(self._reconnect_index, len(self._delays) - 1)
        self._reconnect_index += 1
        return self._delays[index]

    def _run_main(self, *, initial_key: str) -> None:
        import websocket  # lazy: dependency only needed while the stream runs

        key = initial_key
        while not self._stop.is_set():
            url = self._ws_url(key)
            app = websocket.WebSocketApp(
                url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            with self._lock:
                self._app = app
            try:
                app.run_forever(
                    ping_interval=_PING_INTERVAL_SECONDS,
                    ping_timeout=_PING_TIMEOUT_SECONDS,
                )
            finally:
                with self._lock:
                    if self._app is app:
                        self._app = None
            if self._stop.is_set():
                break
            delay = self._next_reconnect_delay()
            logger.warning(
                "User-data stream disconnected; reconnecting in %.0fs", delay
            )
            if self._sleep(delay) is False:  # allow tests to fast-forward
                break
            with self._lock:
                key = (
                    self._ensure_listen_key()
                    if self._listen_key is None
                    else self._listen_key
                )

    def _on_open(self, _app: Any) -> None:
        self._reconnect_index = 0
        logger.info("User-data stream connected")
        connected = self._handlers.on_connected
        if connected is not None:
            connected()

    def _on_message(self, _app: Any, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            logger.warning("User-data stream: non-JSON message ignored")
            return
        if not isinstance(payload, dict):
            return
        event = str(payload.get("e") or "")
        if event == "ORDER_TRADE_UPDATE":
            handler = self._handlers.on_order_update
        elif event == "ACCOUNT_UPDATE":
            handler = self._handlers.on_account_update
        else:
            if event == "listenKeyExpired":
                # Terminal control event: never forwarded to on_other_event.
                logger.warning("User-data stream: listenKey expired; key will be re-issued")
                with self._lock:
                    self._listen_key = None
                expired = self._handlers.on_listen_key_expired
                if expired is not None:
                    expired()
                return
            other = self._handlers.on_other_event
            if other is not None:
                other(event, payload)
            return
        if handler is not None:
            try:
                handler(payload)
            except Exception:
                logger.exception("User-data event handler failed for %s", event)

    def _on_error(self, _app: Any, error: Exception) -> None:
        logger.warning("User-data stream error: %s", error)

    def _on_close(self, _app: Any, *_args: object) -> None:
        logger.info("User-data stream closed")
        disconnected = self._handlers.on_disconnected
        if disconnected is not None:
            try:
                disconnected()
            except Exception:
                logger.exception("on_disconnected handler failed")

    def _keepalive_loop(self) -> None:
        while not self._stop.wait(self._keepalive_interval):
            with self._lock:
                key = self._listen_key
            if key is None:
                continue
            try:
                self._keepalive(key)
            except Exception:
                logger.warning(
                    "listenKey keepalive failed; forcing reconnect with a fresh key",
                    exc_info=True,
                )
                with self._lock:
                    self._listen_key = None
                app = self._app
                if app is not None:
                    with contextlib.suppress(Exception):  # pragma: no cover
                        app.close()

class BinanceMarkPriceStream:
    """Combined public mark-price stream for a fixed symbol set.

    One websocket subscribes to <symbol>@markPrice (updates ~3s) for every
    symbol and pushes Decimal prices through on_update. This replaces the
    per-cycle all_mark_prices REST batch while the socket is up; REST fallback
    (snapshot poller / direct mark_price) covers disconnects, so the stream is
    strictly an optimization and never a correctness dependency.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        on_update: Callable[[str, Decimal], None],
        ws_base: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not ws_base:
            ws_base = _DEFAULT_WS_BASE
        if not symbols:
            raise ValueError("mark-price stream needs at least one symbol")
        self._symbols = [str(s).upper() for s in symbols]
        self._on_update = on_update
        self._ws_base = ws_base.rstrip("/")
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._app: Any = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="binance-mark-price", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                app.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _stream_url(self) -> str:
        streams = "/".join(f"{s.lower()}@markPrice" for s in self._symbols)
        return f"{self._ws_base}/stream?streams={streams}"
    def _run(self) -> None:
        import websocket  # lazy, same as user-data stream

        delays = _RECONNECT_DELAYS_SECONDS
        index = 0
        while not self._stop.is_set():
            app = websocket.WebSocketApp(
                self._stream_url(),
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            self._app = app
            try:
                app.run_forever(
                    ping_interval=_PING_INTERVAL_SECONDS,
                    ping_timeout=_PING_TIMEOUT_SECONDS,
                )
            finally:
                self._app = None
            if self._stop.is_set():
                break
            delay = delays[min(index, len(delays) - 1)]
            index += 1
            if self._sleep(delay) is False:
                break

    def _on_open(self, _app: Any) -> None:
        logger.info("Mark-price stream connected (%d symbols)", len(self._symbols))

    def _on_message(self, _app: Any, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        symbol = str(data.get("s") or "")
        raw = data.get("p")
        if not symbol or raw is None:
            return
        try:
            price = Decimal(str(raw))
        except Exception:
            return
        try:
            self._on_update(symbol, price)
        except Exception:
            logger.exception("mark-price update handler failed for %s", symbol)

    def _on_error(self, _app: Any, error: Exception) -> None:
        logger.warning("Mark-price stream error: %s", error)

    def _on_close(self, _app: Any, *_args: object) -> None:
        logger.info("Mark-price stream closed")



