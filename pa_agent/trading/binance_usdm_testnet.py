"""Binance USDⓈ-M Futures Testnet order execution.

This module intentionally supports Testnet only. Credentials are read only from
``BINANCE_USDM_TESTNET_API_KEY`` and ``BINANCE_USDM_TESTNET_API_SECRET`` so they
never enter ``settings.json`` or application logs.
"""

import hashlib
import hmac
import json
import logging
import math
import os
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
    if str(decision.get("order_type") or "") != "市价单":
        return ExecutionResult(
            "rejected", "Only 市价单 is automated; limit and breakout plans require manual review"
        )

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
            os.environ.get("BINANCE_USDM_TESTNET_API_KEY", ""),
            os.environ.get("BINANCE_USDM_TESTNET_API_SECRET", ""),
        )
        if not active_client.one_way_mode():
            return ExecutionResult(
                "rejected", "Hedge mode unsupported; change Testnet account to one-way mode"
            )
        info = active_client.exchange_info(symbol)
        price = active_client.mark_price(symbol)
        quantity = _quantity_for_notional(config.max_notional_usdt, price, info)
        if quantity is None:
            return ExecutionResult(
                "rejected", "Configured notional is below symbol minimum or invalid"
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
            client_id=f"pa-entry-{uuid.uuid4().hex[:22]}",
        )
        exit_side = "SELL" if side == "BUY" else "BUY"
        protected_algo_ids: list[str] = []
        try:
            stop_algo_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
            active_client.place_close_algo_order(
                symbol=symbol,
                side=exit_side,
                order_type="STOP_MARKET",
                stop_price=stop,
                client_algo_id=stop_algo_id,
            )
            protected_algo_ids.append(stop_algo_id)
            target_algo_id = f"pa-tp-{uuid.uuid4().hex[:24]}"
            active_client.place_close_algo_order(
                symbol=symbol,
                side=exit_side,
                order_type="TAKE_PROFIT_MARKET",
                stop_price=target,
                client_algo_id=target_algo_id,
            )
        except BinanceAPIError:
            # Avoid leaving a close-all trigger that could affect a later position.
            for client_algo_id in protected_algo_ids:
                try:
                    active_client.cancel_algo_order(client_algo_id=client_algo_id)
                except BinanceAPIError:
                    logger.exception("Failed to cancel orphaned Testnet protective order")
            # Never leave an unprotected automatically-created position.
            active_client.close_market_position(symbol=symbol, side=exit_side, quantity=quantity)
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
        logger.warning("Binance Testnet automatic order rejected: %s", exc)
        return ExecutionResult("failed", str(exc), symbol)


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


def _is_recent_signal(signal_id: str, cooldown_minutes: int) -> bool:
    """Return whether a successfully-submitted plan is still in cooldown."""
    path = os.fspath(_RUNTIME_STATE_PATH)
    now = time.time()
    try:
        with open(path, encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise BinanceAPIError("Cannot read Testnet duplicate-signal state") from exc
    seen_at = state.get(signal_id)
    return isinstance(seen_at, (int, float)) and now - seen_at < cooldown_minutes * 60


def _remember_signal(signal_id: str) -> None:
    """Persist only after all entry and protective orders were accepted."""
    path = os.fspath(_RUNTIME_STATE_PATH)
    try:
        with open(path, encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        state = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise BinanceAPIError("Cannot read Testnet duplicate-signal state") from exc
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state[signal_id] = time.time()
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
    except OSError as exc:
        # Fail closed: an unavailable dedupe state must not allow new orders.
        raise BinanceAPIError("Cannot persist Testnet duplicate-signal state") from exc


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


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
