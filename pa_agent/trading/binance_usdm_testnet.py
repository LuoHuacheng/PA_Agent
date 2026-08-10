"""Binance USDⓈ-M Futures Testnet order execution.

This module intentionally supports Testnet only. Credentials are read from the
local gitignored ``settings.json`` file and never written to application logs.
"""

import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pa_agent.config.settings import BinanceUSDMTestnetSettings, Settings
from pa_agent.util.trade_metrics import compute_risk_reward, passes_trader_equation

logger = logging.getLogger(__name__)

_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
_TIMEOUT_SECONDS = 12
_RUNTIME_STATE_PATH = "trade_records/binance_usdm_testnet_state.json"
_STATE_LOCK = threading.Lock()
_ENTRY_CLIENT_PREFIX = "pa-entry-"


class BinanceAPIError(RuntimeError):
    """A rejected or unavailable Binance API request."""


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str
    symbol: str = ""
    quantity: str = ""
    entry_order_id: str = ""


class BinanceUSDMTestnetClient:
    """Small signed REST client for the Testnet U本位 API only."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        opener: Callable[..., Any] = urlopen,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("Binance Testnet API key and secret are required")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._opener = opener
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> dict[str, Any] | list[Any]:
        payload = {k: str(v) for k, v in (params or {}).items() if v is not None}
        if signed:
            payload.setdefault("timestamp", str(self._now_ms()))
            payload.setdefault("recvWindow", "5000")
            query = urlencode(payload)
            payload["signature"] = hmac.new(
                self._api_secret, query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        query = urlencode(payload)
        url = f"{_TESTNET_BASE_URL}{path}" + (f"?{query}" if query else "")
        request = Request(url, method=method, headers={"X-MBX-APIKEY": self._api_key})
        try:
            with self._opener(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BinanceAPIError(f"Binance HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise BinanceAPIError(f"Binance network error: {exc.reason}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BinanceAPIError("Binance returned invalid JSON") from exc
        if isinstance(result, dict) and result.get("code", 0) not in (0, None):
            raise BinanceAPIError(f"Binance error {result['code']}: {result.get('msg', '')}")
        return result

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        response = self._request("GET", "/fapi/v1/exchangeInfo")
        for item in response.get("symbols", []) if isinstance(response, dict) else []:
            if item.get("symbol") == symbol:
                return item
        raise BinanceAPIError(f"Testnet does not list symbol {symbol}")

    def mark_price(self, symbol: str) -> Decimal:
        result = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        try:
            return Decimal(str(result["markPrice"]))
        except (KeyError, ValueError) as exc:
            raise BinanceAPIError("Binance returned no mark price") from exc

    def one_way_mode(self) -> bool:
        result = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return not bool(result.get("dualSidePosition"))

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

    def place_close_algo_order(
        self, *, symbol: str, side: str, order_type: str, stop_price: Decimal, client_algo_id: str
    ) -> None:
        # Binance migrated conditional orders to the Algo Service in December 2025.
        self._request(
            "POST",
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "triggerPrice": _decimal_text(stop_price),
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "priceProtect": "TRUE",
                "clientAlgoId": client_algo_id,
            },
            signed=True,
        )

    def cancel_algo_order(self, *, client_algo_id: str) -> None:
        self._request(
            "DELETE",
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
) -> ExecutionResult:
    """Execute one validated market signal, with mandatory Testnet TP/SL protection."""
    config = settings.binance_usdm_testnet if settings is not None else BinanceUSDMTestnetSettings()
    if not config.enabled:
        return ExecutionResult("skipped", "Binance Testnet automation disabled")
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
    if config.require_trader_equation:
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
        if not active_client.one_way_mode():
            return ExecutionResult(
                "rejected", "Hedge mode unsupported; change Testnet account to one-way mode"
            )
        info = active_client.exchange_info(symbol)
        price = active_client.mark_price(symbol)
        stop = _price_for_tick(stop, info)
        target = _price_for_tick(target, info)
        quantity = _quantity_for_notional(config.max_notional_usdt, price, info)
        if quantity is None:
            return ExecutionResult(
                "rejected", "Configured notional is below symbol minimum or invalid"
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
                signal_id,
            )
        if side == "BUY" and not (stop < price < target):
            return ExecutionResult("rejected", "Long requires stop < mark price < target")
        if side == "SELL" and not (target < price < stop):
            return ExecutionResult("rejected", "Short requires target < mark price < stop")
        active_client.set_leverage(symbol, config.leverage)
        entry = active_client.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            client_id=f"{_ENTRY_CLIENT_PREFIX}{uuid.uuid4().hex[:22]}",
        )
        try:
            _attach_protection(active_client, symbol, side, stop, target)
        except BinanceAPIError:
            # Never leave an unprotected automatically-created position.
            active_client.close_market_position(
                symbol=symbol, side="SELL" if side == "BUY" else "BUY", quantity=quantity
            )
            raise
        _remember_signal(signal_id)
        return ExecutionResult(
            "submitted",
            "Testnet entry and protective orders submitted",
            symbol,
            _decimal_text(quantity),
            str(entry.get("orderId", "")),
        )
    except (BinanceAPIError, ValueError) as exc:
        message = str(exc)
        if "-2015" in message or "HTTP 401" in message:
            message += " (Hint: check configured API key/secret pair and futures permission.)"
        logger.warning("Binance Testnet automatic order rejected: %s", message)
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
    signal_id: str,
) -> ExecutionResult:
    if not config.limit_order_enabled:
        return ExecutionResult("skipped", "Limit order automation disabled", symbol)
    entry_price = _positive_decimal(decision.get("entry_price"))
    if entry_price is None:
        return ExecutionResult("rejected", "Limit order entry price required")
    if side == "BUY":
        if not stop < entry_price:
            return ExecutionResult("rejected", "Long limit requires stop < limit price")
        crosses_mark = entry_price >= mark_price
    else:
        if not entry_price < stop:
            return ExecutionResult("rejected", "Short limit requires limit price < stop")
        crosses_mark = entry_price <= mark_price
    if crosses_mark:
        # A crossed limit would fill immediately. Submit a market entry instead
        # so protection is attached through the same rollback-safe path.
        client.set_leverage(symbol, config.leverage)
        entry = client.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            client_id=f"{_ENTRY_CLIENT_PREFIX}{uuid.uuid4().hex[:22]}",
        )
        try:
            _attach_protection(client, symbol, side, stop, target)
        except BinanceAPIError:
            client.close_market_position(
                symbol=symbol,
                side="SELL" if side == "BUY" else "BUY",
                quantity=quantity,
            )
            raise
        _remember_signal(signal_id)
        return ExecutionResult(
            "submitted",
            "Limit entry crossed mark price; submitted market entry and protective orders",
            symbol,
            _decimal_text(quantity),
            str(entry.get("orderId", "")),
        )
    replacement = _replace_pending_limit(client, symbol)
    if replacement is not None:
        return replacement
    client.set_leverage(symbol, config.leverage)
    entry_client_id = f"{_ENTRY_CLIENT_PREFIX}{uuid.uuid4().hex[:22]}"
    pending_record = {
        "client_id": entry_client_id,
        "signal_id": signal_id,
        "side": side,
        "quantity": _decimal_text(quantity),
        "stop": _decimal_text(stop),
        "target": _decimal_text(target),
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
            "quantity": quantity,
            "signal_id": signal_id,
            "timeout_seconds": config.limit_fill_timeout_minutes * 60,
            "poll_interval": config.limit_poll_interval_seconds,
        },
        daemon=True,
    )
    watcher.start()
    logger.info(
        "Testnet limit entry placed for %s: order=%s entry=%s qty=%s",
        symbol,
        entry_client_id,
        _decimal_text(entry_price),
        _decimal_text(quantity),
    )
    return ExecutionResult(
        "pending",
        "Testnet limit entry placed; awaiting fill",
        symbol,
        _decimal_text(quantity),
        str(order.get("orderId", "")),
    )


def _replace_pending_limit(client: BinanceUSDMTestnetClient, symbol: str) -> ExecutionResult | None:
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
    if not old_client_id or side not in ("BUY", "SELL") or stop is None or target is None:
        _drop_pending(symbol, old_client_id)
        return None
    try:
        status = client.order_status(symbol=symbol, client_id=old_client_id)
    except BinanceAPIError as exc:
        if _is_missing_order_error(exc):
            logger.info("Removing stale Testnet pending entry for %s: %s", symbol, exc)
            _drop_pending(symbol, old_client_id)
            return None
        logger.warning("Cannot inspect previous limit entry for %s: %s", symbol, exc)
        return ExecutionResult("failed", f"Cannot inspect previous limit entry: {exc}", symbol)
    if status == "FILLED":
        # The watcher died (process restart) before attaching protection: repair.
        try:
            _attach_protection(client, symbol, side, stop, target)
        except BinanceAPIError as exc:
            logger.error("Filled limit entry for %s left unprotected: %s", symbol, exc)
            return ExecutionResult(
                "failed", f"Filled limit entry needs manual protection: {exc}", symbol
            )
        if old_signal_id:
            _remember_signal(old_signal_id)
        _drop_pending(symbol, old_client_id)
        return ExecutionResult("skipped", "Previously filled limit entry now protected", symbol)
    if status in ("NEW", "PARTIALLY_FILLED"):
        try:
            client.cancel_order(symbol=symbol, client_id=old_client_id)
        except BinanceAPIError as exc:
            return ExecutionResult("failed", f"Cannot replace pending limit entry: {exc}", symbol)
        logger.info("Replaced stale Testnet limit entry %s for %s", old_client_id, symbol)
    _drop_pending(symbol, old_client_id)
    return None


def _attach_protection(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    side: str,
    stop: Decimal,
    target: Decimal,
) -> None:
    """Attach close-position STOP_MARKET and TAKE_PROFIT_MARKET orders.

    On failure, cancels any orders already placed and re-raises so the caller
    can roll back the position.
    """
    exit_side = "SELL" if side == "BUY" else "BUY"
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
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="TAKE_PROFIT_MARKET",
            stop_price=target,
            client_algo_id=target_algo_id,
        )
        protected_algo_ids.append(target_algo_id)
    except BinanceAPIError:
        # Avoid leaving a close-all trigger that could affect a later position.
        for client_algo_id in protected_algo_ids:
            try:
                client.cancel_algo_order(client_algo_id=client_algo_id)
            except BinanceAPIError:
                logger.exception("Failed to cancel orphaned Testnet protective order")
        raise


def _watch_limit_entry(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    client_id: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    quantity: Decimal,
    signal_id: str,
    timeout_seconds: float,
    poll_interval: float,
) -> None:
    """Poll a resting limit entry; attach TP/SL on fill, cancel on timeout."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            status = client.order_status(symbol=symbol, client_id=client_id)
        except BinanceAPIError as exc:
            logger.warning("Testnet limit fill check failed for %s: %s", symbol, exc)
            if time.monotonic() >= deadline:
                try:
                    client.cancel_order(symbol=symbol, client_id=client_id)
                except BinanceAPIError as cancel_exc:
                    logger.warning(
                        "Cancel timed-out limit entry failed for %s: %s", symbol, cancel_exc
                    )
                _drop_pending(symbol, client_id)
                logger.info(
                    "Testnet limit entry timed out and was canceled: %s %s", symbol, client_id
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            time.sleep(min(poll_interval, remaining))
            continue
        if status == "FILLED":
            try:
                _attach_protection(client, symbol, side, stop, target)
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
                return
            _remember_signal(signal_id)
            _drop_pending(symbol, client_id)
            logger.info("Testnet limit entry filled and protected: %s %s", symbol, client_id)
            return
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            _drop_pending(symbol, client_id)
            logger.info("Testnet limit entry ended (%s): %s %s", status, symbol, client_id)
            return
        if time.monotonic() >= deadline:
            try:
                client.cancel_order(symbol=symbol, client_id=client_id)
            except BinanceAPIError as exc:
                logger.warning("Cancel timed-out limit entry failed for %s: %s", symbol, exc)
            _drop_pending(symbol, client_id)
            logger.info("Testnet limit entry timed out and was canceled: %s %s", symbol, client_id)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(poll_interval, remaining))


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
    path = os.fspath(_RUNTIME_STATE_PATH)
    try:
        with open(path, encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise BinanceAPIError("Cannot read Testnet execution state") from exc
    return state if isinstance(state, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    """Atomically persist the runtime execution state file."""
    path = os.fspath(_RUNTIME_STATE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
        os.replace(temp, path)
    except OSError as exc:
        # Fail closed: an unavailable state must not allow new orders.
        raise BinanceAPIError("Cannot persist Testnet execution state") from exc


def _is_recent_signal(signal_id: str, cooldown_minutes: int) -> bool:
    """Return whether a successfully-submitted plan is still in cooldown."""
    now = time.time()
    with _STATE_LOCK:
        seen = _load_state().get("seen")
    seen_at = seen.get(signal_id) if isinstance(seen, dict) else None
    return isinstance(seen_at, (int, float)) and now - seen_at < cooldown_minutes * 60


def _is_missing_order_error(exc: BinanceAPIError) -> bool:
    """Return whether Binance explicitly reported error code -2013."""
    message = str(exc).lower()
    return "binance error -2013" in message


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
