"""Tests for isolated Binance USDⓈ-M Testnet execution."""

from __future__ import annotations

import io
import json
import time
from decimal import Decimal
from http.client import RemoteDisconnected
from urllib.error import HTTPError

import pytest

from pa_agent.config.settings import BinanceUSDMTestnetSettings, Settings
from pa_agent.trading import binance_usdm_testnet
from pa_agent.trading.binance_usdm_testnet import BinanceAPIError, execute_market_signal


@pytest.fixture(autouse=True)
def _isolate_dedupe_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        binance_usdm_testnet,
        "_RUNTIME_STATE_PATH",
        str(tmp_path / "binance_usdm_testnet_state.json"),
    )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.statuses: dict[str, list[str]] = {}
        self.limit_orders: dict[str, dict] = {}

    def one_way_mode(self) -> bool:
        return True

    def exchange_info(self, symbol: str) -> dict:
        self.calls.append(("exchange_info", symbol))
        return {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]
        }

    def mark_price(self, symbol: str) -> Decimal:
        self.calls.append(("mark_price", symbol))
        return Decimal("100")

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls.append(("set_leverage", symbol, leverage))

    def place_market_order(self, **kwargs: object) -> dict:
        self.calls.append(("entry", kwargs))
        return {"orderId": 123}

    def place_limit_order(self, **kwargs: object) -> dict:
        self.calls.append(("limit_entry", kwargs))
        self.limit_orders[str(kwargs["client_id"])] = {"status": "NEW"}
        return {"orderId": 456}

    def order_status(self, *, symbol: str, client_id: str) -> str:
        self.calls.append(("order_status", client_id))
        queue = self.statuses.get(client_id)
        if queue:
            return queue.pop(0)
        order = self.limit_orders.get(client_id)
        if order is None:
            raise BinanceAPIError("order not found")
        return str(order["status"])

    def cancel_order(self, *, symbol: str, client_id: str) -> None:
        self.calls.append(("cancel_limit", client_id))
        order = self.limit_orders.get(client_id)
        if order is not None:
            order["status"] = "CANCELED"

    def place_close_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("protection", kwargs))

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))

    def close_market_position(self, **kwargs: object) -> None:
        self.calls.append(("rollback", kwargs))

    def net_position(self, symbol: str) -> Decimal:
        self.calls.append(("net_position", symbol))
        return Decimal("0")

    def daily_close_series(self, symbol: str, days: int) -> list[float]:
        # Not configured in the fake: the 30d-trend guard fails open.
        raise BinanceAPIError("daily klines not configured in fake")

    def position_info(self, symbol: str) -> dict:
        # Flat by default: breakeven guard threads exit on their first poll.
        self.calls.append(("position_info", symbol))
        return {"amount": Decimal("0"), "entry": None}


class OpenPositionClient(FakeClient):
    """Account that already holds an open position for the target symbol."""

    def __init__(self, amount: str = "0.001") -> None:
        super().__init__()
        self._amount = amount

    def net_position(self, symbol: str) -> Decimal:
        self.calls.append(("net_position", symbol))
        return Decimal(self._amount)


def _state() -> dict:
    return json.load(open(binance_usdm_testnet._RUNTIME_STATE_PATH, encoding="utf-8"))


def _pending_state() -> dict:
    pending = _state().get("pending") or {}
    return pending if isinstance(pending, dict) else {}


class FailSecondProtectionClient(FakeClient):
    def place_close_algo_order(self, **kwargs: object) -> None:
        super().place_close_algo_order(**kwargs)
        if len([call for call in self.calls if call[0] == "protection"]) == 2:
            raise BinanceAPIError("take-profit rejected")


class AuthRejectedClient(FakeClient):
    def one_way_mode(self) -> bool:
        raise BinanceAPIError(
            'Binance HTTP 401: {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action"}'
        )


class HedgeModeClient(FakeClient):
    """Account in hedge mode; auto-switch succeeds on the first call."""

    def __init__(self) -> None:
        super().__init__()
        self.switch_calls = 0

    def one_way_mode(self) -> bool:
        return self.switch_calls > 0

    def set_one_way_mode(self) -> None:
        self.switch_calls += 1


class HedgeSwitchFailClient(HedgeModeClient):
    """Account in hedge mode; auto-switch is rejected by Binance."""

    def set_one_way_mode(self) -> None:
        raise BinanceAPIError("position not empty")


def _settings(*, enabled: bool = True, dry_run: bool = False) -> Settings:
    settings = Settings()
    settings.binance_usdm_testnet.enabled = enabled
    settings.binance_usdm_testnet.dry_run = dry_run
    settings.binance_usdm_testnet.emergency_stop = False
    settings.binance_usdm_testnet.symbol = "BTCUSDT"
    settings.binance_usdm_testnet.symbol_whitelist = ["BTCUSDT"]
    settings.binance_usdm_testnet.max_notional_usdt = 20
    return settings


def _long_decision() -> dict:
    return {
        "order_type": "市价单",
        "order_direction": "做多",
        "entry_price": 100,
        "stop_loss_price": 90,
        "take_profit_price": 120,
        "estimated_win_rate": 70,
    }


def test_disabled_never_calls_client() -> None:
    result = execute_market_signal(_long_decision(), _settings(enabled=False), client=FakeClient())
    assert result.status == "skipped"


def test_dry_run_never_calls_client() -> None:
    client = FakeClient()
    result = execute_market_signal(_long_decision(), _settings(dry_run=True), client=client)
    assert result.status == "dry_run"
    assert not client.calls

class RateLimitClient(FakeClient):
    """Fails with Binance HTTP 418 (rate-limit ban) for the first N calls."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self._failures = failures

    def one_way_mode(self) -> bool:
        if self._failures > 0:
            self._failures -= 1
            raise BinanceAPIError(
                "Binance HTTP 418: {\"code\":-1003,\"msg\":\"Way too many requests; IP(1.2.3.4) banned until 1\"}"
            )
        return True


def _retry_settings() -> Settings:
    settings = _settings()
    settings.binance_usdm_testnet.execution_retry_max_attempts = 3
    settings.binance_usdm_testnet.execution_retry_backoff_seconds = 5
    return settings


def test_rate_limit_failure_retries_with_backoff_then_submits(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = RateLimitClient(failures=2)
    result = execute_market_signal(
        _long_decision(), _retry_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "submitted", result.reason
    assert sleeps == [5.0, 10.0]


def test_rate_limit_exhaustion_reports_failed_with_retry_note(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = RateLimitClient(failures=99)
    result = execute_market_signal(
        _long_decision(), _retry_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "failed"
    assert "retries exhausted" in result.reason
    assert sleeps == [5.0, 10.0]


def test_non_rate_limit_failure_is_one_shot(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = AuthRejectedClient()
    result = execute_market_signal(
        _long_decision(), _retry_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "failed"
    assert sleeps == []


def test_hedge_mode_auto_switches_to_one_way() -> None:
    client = HedgeModeClient()
    result = execute_market_signal(_long_decision(), _settings(), client=client)
    assert client.switch_calls == 1
    assert result.status in ("submitted", "pending")


def test_hedge_mode_switch_failure_rejects_signal() -> None:
    client = HedgeSwitchFailClient()
    result = execute_market_signal(_long_decision(), _settings(), client=client)
    assert result.status == "rejected"
    assert "Hedge mode" in result.reason


def test_execution_constructs_client_from_settings_credentials(monkeypatch) -> None:
    """Automatic execution uses local settings credentials, never environment variables."""
    created_with: list[tuple[str, str]] = []
    fake_client = FakeClient()

    def construct_client(api_key: str, api_secret: str) -> FakeClient:
        created_with.append((api_key, api_secret))
        return fake_client

    monkeypatch.setattr(binance_usdm_testnet, "BinanceUSDMTestnetClient", construct_client)
    settings = _settings()
    settings.binance_usdm_testnet.api_key = "settings-key"
    settings.binance_usdm_testnet.api_secret = "settings-secret"

    result = execute_market_signal(_long_decision(), settings, analysis_symbol="BTCUSDT")

    assert result.status == "submitted"
    assert created_with == [("settings-key", "settings-secret")]


def test_price_for_tick_rounds_down_to_exchange_precision() -> None:
    exchange_info = {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}

    result = binance_usdm_testnet._price_for_tick(Decimal("1957.686"), exchange_info)

    assert result == Decimal("1957.68")


def test_request_wraps_remote_disconnect_as_binance_api_error() -> None:
    def disconnected_opener(*_args: object, **_kwargs: object) -> None:
        raise RemoteDisconnected("remote closed connection")

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=disconnected_opener
    )

    with pytest.raises(BinanceAPIError, match="Binance network error"):
        client.order_status(symbol="BTCUSDT", client_id="pa-entry-test")


class _OkResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _OkResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


def _http_error(code: int, payload: dict) -> HTTPError:
    return HTTPError("https://testnet.binancefuture.com", code, "err", None, io.BytesIO(json.dumps(payload).encode()))


def test_request_retries_transient_network_error_then_succeeds() -> None:
    calls: list[int] = []

    def flaky_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        calls.append(1)
        if len(calls) <= 2:
            raise RemoteDisconnected("tls ripped mid-flight")
        return _OkResponse({"serverTime": 1})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=flaky_opener
    )

    assert client._request("GET", "/fapi/v1/time") == {"serverTime": 1}
    assert len(calls) == 3


def test_request_retries_order_post_with_idempotency_key() -> None:
    calls: list[int] = []

    def flaky_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        calls.append(1)
        if len(calls) <= 1:
            raise RemoteDisconnected("tls ripped")
        return _OkResponse({"orderId": 42})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=flaky_opener
    )

    result = client._request(
        "POST",
        "/fapi/v1/order",
        {"symbol": "BTCUSDT", "newClientOrderId": "pa-entry-x"},
        signed=True,
    )
    assert result == {"orderId": 42}
    assert len(calls) == 2


def test_request_retries_clock_skew_1021() -> None:
    calls: list[int] = []

    def skew_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(400, {"code": -1021, "msg": "Timestamp outside recvWindow."})
        return _OkResponse({"order": {"orderId": 7}})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=skew_opener
    )

    result = client._request(
        "GET", "/fapi/v1/order", {"symbol": "BTCUSDT", "clientOrderId": "pa-entry-x"}, signed=True
    )
    assert result == {"order": {"orderId": 7}}
    assert len(calls) == 2


def test_request_does_not_retry_business_error() -> None:
    calls: list[int] = []

    def err_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        calls.append(1)
        raise _http_error(400, {"code": -2013, "msg": "Order does not exist."})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=err_opener
    )

    with pytest.raises(BinanceAPIError, match="-2013"):
        client._request("GET", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)
    assert len(calls) == 1


def test_request_does_not_retry_post_without_idempotency_key() -> None:
    calls: list[int] = []

    def net_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        calls.append(1)
        raise RemoteDisconnected("tls ripped")

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=net_opener
    )

    with pytest.raises(BinanceAPIError, match="network error"):
        client._request(
            "POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 1}, signed=True
        )
    assert len(calls) == 1


def test_algo_success_body_code_200_is_not_an_error() -> None:
    """Algo API 成功响应形如 {"code":200,"msg":"success"}: 不得被当成错误。"""

    def ok_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        return _OkResponse({"code": 200, "msg": "success"})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=ok_opener
    )
    # 撤单成功必须无异常返回, 否则保本移动会误判失败、丢止损不补挂。
    client.cancel_algo_order(client_algo_id="pa-sl-test0001")


def test_entry_client_id_deterministic_and_bounded() -> None:
    first = binance_usdm_testnet._entry_client_id("sig-abc")
    assert first == binance_usdm_testnet._entry_client_id("sig-abc")
    assert first.startswith("pa-entry-")
    assert len(first) <= 36
    assert first != binance_usdm_testnet._entry_client_id("sig-abd")


def test_daily_pnl_aggregate_groups_and_sums_per_day() -> None:
    # 1788546600000 / 1788634200000 ms 落在 UTC+8 的 2026-09-05 / 09-06
    rows = [
        {"time": 1788546600000, "incomeType": "REALIZED_PNL", "income": "12.5"},
        {"time": 1788546600001, "incomeType": "COMMISSION", "income": "-0.5"},
        {"time": 1788634200000, "incomeType": "REALIZED_PNL", "income": "8.0"},
        {"time": 1788634200001, "incomeType": "FUNDING_FEE", "income": "-0.2"},
    ]
    summary = binance_usdm_testnet._daily_pnl_aggregate(rows, tz_hours=8)
    assert [item["date"] for item in summary] == ["2026-09-05", "2026-09-06"]
    assert summary[0]["net"] == 12.5 - 0.5
    assert summary[1]["realized_pnl"] == 8.0
    assert summary[1]["net"] == 8.0 - 0.2


def test_daily_pnl_aggregate_ignores_unknown_types_and_sorts_days() -> None:
    rows = [
        {"time": 1788634200000, "incomeType": "TRANSFER", "income": "999"},
        {"time": 1788634200001, "incomeType": "REALIZED_PNL", "income": "1.0"},
    ]
    summary = binance_usdm_testnet._daily_pnl_aggregate(rows, tz_hours=8)
    assert summary[0]["net"] == 1.0
    assert summary[0]["realized_pnl"] == 1.0


def test_trader_equation_risk_is_entry_to_stop() -> None:
    """Risk in the §10.3 equation is entry→SL distance, not stop↔target span.

    entry=100, sl=90, tp=120 → risk=10, reward=20. At 55% win rate the trade
    passes (0.55×20 > 0.45×10); treating the stop↔target span (30) as risk
    would wrongly reject it (0.55×20 < 0.45×30).
    """
    decision = _long_decision()
    decision["estimated_win_rate"] = 55
    result = execute_market_signal(
        decision, _settings(), analysis_symbol="BTCUSDT", client=FakeClient()
    )
    assert result.status == "submitted", result.reason


def test_missing_entry_price_rejected_cleanly() -> None:
    """A decision without entry_price must reject, never raise TypeError."""
    decision = _long_decision()
    decision.pop("entry_price")
    result = execute_market_signal(
        decision, _settings(), analysis_symbol="BTCUSDT", client=FakeClient()
    )
    assert result.status == "rejected"
    assert "Trader's equation" in result.reason


def test_market_signal_submits_entry_and_both_protections() -> None:
    client = FakeClient()
    result = execute_market_signal(
        _long_decision(), _settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "submitted"
    assert result.entry_order_id == "123"
    assert [call[0] for call in client.calls] == [
        "net_position",
        "exchange_info",
        "mark_price",
        "set_leverage",
        "entry",
        "protection",
        "protection",
    ]
    protections = [call[1] for call in client.calls if call[0] == "protection"]
    assert {order["order_type"] for order in protections} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


def test_margin_constant_across_leverage() -> None:
    """保证金恒定：杠杆翻倍时名义价值翻倍，但 quantity 保持保证金/价格不变。"""

    def entry_qty(leverage: int, entry_price: float) -> Decimal:
        settings = _settings()
        settings.binance_usdm_testnet.max_notional_usdt = 100  # margin USDT
        settings.binance_usdm_testnet.leverage = leverage
        client = FakeClient()
        decision = _long_decision() | {"entry_price": entry_price}
        execute_market_signal(
            decision, settings, analysis_symbol="BTCUSDT", client=client
        )
        entry = [call[1] for call in client.calls if call[0] == "entry"][0]
        return Decimal(str(entry["quantity"]))

    # mark price 固定 100 → q = margin*leverage/price；不同 entry 避免冷却去重
    q1 = entry_qty(1, 101)
    q20 = entry_qty(20, 102)
    # margin 100U: q1 = 100*1/100 = 1; q20 = 100*20/100 = 20
    assert q1 == Decimal("1")
    assert q20 == Decimal("20")
    # 保证金 = 名义/杠杆 恒定：100*20/20 == 100*1/1
    assert q20 / 20 == q1



def test_risk_per_trade_setting_bounds() -> None:
    """P2-1: 单笔风险金 0=关闭默认, 0<x<=1000 有效, 越界拒绝."""
    from pydantic import ValidationError

    assert BinanceUSDMTestnetSettings().risk_per_trade_usdt == 0.0
    assert BinanceUSDMTestnetSettings(risk_per_trade_usdt=2).risk_per_trade_usdt == 2.0
    for bad in (-1, 1001):
        with pytest.raises(ValidationError):
            BinanceUSDMTestnetSettings(risk_per_trade_usdt=bad)


def test_quantity_for_risk_math() -> None:
    """P2-1: qty = risk/|anchor-stop| 按 LOT_SIZE 向下取整, 边界返回 None."""
    q = binance_usdm_testnet._quantity_for_risk
    info = {"filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]}
    assert q(2.0, Decimal("100"), Decimal("99"), info) == Decimal("2")
    assert q(2.0, Decimal("100"), Decimal("97"), info) == Decimal("0.666")
    assert q(2.0, Decimal("100"), Decimal("100"), info) is None  # gap=0
    assert q(0.0005, Decimal("100"), Decimal("99"), info) is None  # below minQty
    assert q(2.0, Decimal("100"), Decimal("99"), {"filters": []}) is None
    assert q(2.0, Decimal("0"), Decimal("99"), info) is None  # anchor<=0
    # 做空方向: stop 高于 anchor
    assert q(2.0, Decimal("100"), Decimal("101"), info) == Decimal("2")


def test_market_risk_equal_sizing() -> None:
    """P2-1: 市价单按 risk/|mark-stop| 定仓 (mark=100, stop=99 -> qty=2)."""
    settings = _settings()
    settings.binance_usdm_testnet.max_notional_usdt = 1000
    settings.binance_usdm_testnet.risk_per_trade_usdt = 2.0
    client = FakeClient()
    decision = _long_decision() | {"stop_loss_price": 99, "entry_price": 101}
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "submitted", result.reason
    entry = next(call[1] for call in client.calls if call[0] == "entry")
    assert Decimal(str(entry["quantity"])) == Decimal("2")


def test_market_risk_sizing_over_margin_cap_rejects() -> None:
    """P2-1: risk 所需名义 > 保证金*杠杆 时拒单且不入场."""
    settings = _settings()
    settings.binance_usdm_testnet.max_notional_usdt = 100  # 杠杆1 -> 名义上限100
    settings.binance_usdm_testnet.risk_per_trade_usdt = 30.0
    client = FakeClient()
    decision = _long_decision() | {"stop_loss_price": 99}
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "margin cap" in result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_limit_risk_sizing_anchors_at_entry_price() -> None:
    """P2-1: resting 限价单按 entry 止损距离定仓."""
    settings = _settings()
    settings.binance_usdm_testnet.max_notional_usdt = 1000
    settings.binance_usdm_testnet.risk_per_trade_usdt = 2.0
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单", "entry_price": 95, "stop_loss_price": 94.5,
    }
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "pending", result.reason
    entries = [call[1] for call in client.calls if call[0] == "limit_entry"]
    assert len(entries) == 1
    assert Decimal(str(entries[0]["quantity"])) == Decimal("4")


def test_rejects_invalid_long_protection_prices_before_entry() -> None:
    client = FakeClient()
    decision = _long_decision() | {"stop_loss_price": 110}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    # Invalid geometry (sl above entry) is now caught by the trader's-equation
    # gate (compute_risk_reward returns None) before any client call.
    assert "Long requires" in result.reason or "Trader's equation" in result.reason, result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_breakout_plan_still_requires_manual_review() -> None:
    client = FakeClient()
    decision = _long_decision() | {"order_type": "突破单"}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "manual review" in result.reason
    assert not client.calls


def test_auth_401_failure_includes_actionable_hint() -> None:
    client = AuthRejectedClient()
    result = execute_market_signal(
        _long_decision(), _settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "failed"
    assert "futures permission" in result.reason
    assert "configured API key/secret pair" in result.reason


def test_limit_signal_places_resting_entry_and_tracks_pending() -> None:
    client = FakeClient()
    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "pending", result.reason
    entries = [call[1] for call in client.calls if call[0] == "limit_entry"]
    assert len(entries) == 1
    assert entries[0]["price"] == Decimal("95")
    assert entries[0]["side"] == "BUY"
    # A resting limit must not attach TP/SL before it fills.
    assert "protection" not in [call[0] for call in client.calls]
    pending = _pending_state()
    assert "BTCUSDT" in pending
    assert pending["BTCUSDT"]["client_id"] == entries[0]["client_id"]


def test_limit_signal_rounds_entry_price_to_exchange_tick_size() -> None:
    client = FakeClient()
    decision = _long_decision() | {"order_type": "限价单", "entry_price": "95.123"}

    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)

    assert result.status == "pending", result.reason
    entries = [call[1] for call in client.calls if call[0] == "limit_entry"]
    assert entries[0]["price"] == Decimal("95.1")


def test_limit_entry_above_mark_submits_market_entry_with_protection() -> None:
    """A crossed long limit must execute immediately without losing protection."""
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 120,
        "take_profit_price": 150,
    }
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "submitted", result.reason
    assert "crossed mark price" in result.reason
    assert [call[0] for call in client.calls] == [
        "net_position",
        "exchange_info",
        "mark_price",
        "set_leverage",
        "entry",
        "protection",
        "protection",
    ]
    assert "limit_entry" not in [call[0] for call in client.calls]


def test_limit_automation_can_be_disabled() -> None:
    client = FakeClient()
    settings = _settings()
    settings.binance_usdm_testnet.limit_order_enabled = False
    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}

    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)

    assert result.status == "skipped"
    assert "Limit order automation disabled" in result.reason
    assert client.calls == []


def test_limit_entry_fill_watcher_attaches_protection() -> None:
    client = FakeClient()
    settings = _settings()
    settings.binance_usdm_testnet.limit_poll_interval_seconds = 1
    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "pending"
    client_id = client.calls[[call[0] for call in client.calls].index("limit_entry")][1][
        "client_id"
    ]
    client.statuses[client_id] = ["FILLED"]
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if "protection" in [call[0] for call in client.calls]:
            break
        time.sleep(0.05)
    protections = [call[1] for call in client.calls if call[0] == "protection"]
    assert {order["order_type"] for order in protections} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    # Fill is recorded for cooldown and the pending record is cleared.
    seen = _state().get("seen") or {}
    assert seen, "signal should be remembered after fill + protection"
    assert not _pending_state()


def test_new_limit_signal_replaces_pending_entry() -> None:
    client = FakeClient()
    settings = _settings()
    first = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(first, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "pending"
    old_id = _pending_state()["BTCUSDT"]["client_id"]
    second = _long_decision() | {"order_type": "限价单", "entry_price": 92}
    result = execute_market_signal(second, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "pending", result.reason
    assert ("cancel_limit", old_id) in client.calls
    new_id = _pending_state()["BTCUSDT"]["client_id"]
    assert new_id != old_id


def test_protection_failure_cancels_first_order_and_rolls_back_entry() -> None:
    client = FailSecondProtectionClient()
    result = execute_market_signal(
        _long_decision(), _settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "failed"
    assert [call[0] for call in client.calls][-2:] == ["cancel_protection", "rollback"]


def test_limit_entry_status_failures_still_timeout_and_cancel(monkeypatch) -> None:
    class AlwaysFailStatusClient(FakeClient):
        def order_status(self, *, symbol: str, client_id: str) -> str:
            self.calls.append(("order_status", client_id))
            raise BinanceAPIError("temporary status failure")

    client = AlwaysFailStatusClient()
    binance_usdm_testnet._persist_pending(
        "BTCUSDT",
        {"client_id": "pa-entry-timeout", "signal_id": "timeout-signal"},
    )
    clock = iter((0.0, 2.0))
    sleep_calls = 0

    def fake_monotonic() -> float:
        return next(clock, 2.0)

    def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise AssertionError("status failures bypassed the timeout deadline")

    monkeypatch.setattr(binance_usdm_testnet.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", fake_sleep)

    binance_usdm_testnet._watch_limit_entry(
        client=client,
        symbol="BTCUSDT",
        client_id="pa-entry-timeout",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        quantity=Decimal("0.2"),
        signal_id="timeout-signal",
        timeout_seconds=1.0,
        poll_interval=0.1,
    )

    assert [call[0] for call in client.calls].count("cancel_limit") == 1
    assert not _pending_state()


def test_limit_entry_state_failure_does_not_place_untracked_order(monkeypatch) -> None:
    client = FakeClient()

    def fail_persist(symbol: str, entry: dict) -> None:
        raise BinanceAPIError("state unavailable")

    monkeypatch.setattr(binance_usdm_testnet, "_persist_pending", fail_persist)
    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)

    assert result.status == "failed"
    assert "state unavailable" in result.reason
    assert "limit_entry" not in [call[0] for call in client.calls]


def test_missing_previous_order_clears_stale_pending_and_retries(monkeypatch) -> None:
    class MissingOrderClient(FakeClient):
        def order_status(self, *, symbol: str, client_id: str) -> str:
            self.calls.append(("order_status", client_id))
            if client_id == "pa-entry-crashed":
                raise BinanceAPIError("Binance error -2013: Order does not exist")
            return super().order_status(symbol=symbol, client_id=client_id)

    client = MissingOrderClient()
    binance_usdm_testnet._persist_pending(
        "BTCUSDT",
        {
            "client_id": "pa-entry-crashed",
            "signal_id": "crashed-signal",
            "side": "BUY",
            "quantity": "0.2",
            "stop": "90",
            "target": "120",
        },
    )

    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)

    assert result.status == "pending", result.reason
    assert len([call for call in client.calls if call[0] == "limit_entry"]) == 1
    assert _pending_state()["BTCUSDT"]["client_id"] != "pa-entry-crashed"


def test_only_binance_2013_marks_missing_order() -> None:
    assert binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError("Binance error -2013: Order does not exist")
    )
    assert binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError('Binance HTTP 400: {"code":-2013,"msg":"Order does not exist."}')
    )
    assert binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError('Binance HTTP 400: {"code": -2013, "msg": "Order does not exist."}')
    )
    assert not binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError("upstream proxy: order does not exist")
    )
    assert not binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError("Binance error -2014: Order does not exist")
    )


def test_stale_pending_with_http_400_2013_clears_and_proceeds() -> None:
    """Regression: order queries surface -2013 as HTTP 400; stale-pending
    cleanup must recognize it as 'order gone' and return None (caller proceeds
    with a fresh entry) instead of failing the whole signal."""
    client = FakeClient()
    old_client_id = "pa-entry-deadbeefdeadbeefdeadbeef"
    with binance_usdm_testnet._STATE_LOCK:
        state = binance_usdm_testnet._load_state()
        state.setdefault("pending", {})["SOLUSDT"] = {
            "client_id": old_client_id,
            "signal_id": "old-signal",
            "side": "SELL",
            "quantity": "1.0",
            "stop": "104.1",
            "target": "102.8",
            "placed_at": time.time() - 3600,
        }
        binance_usdm_testnet._save_state(state)

    # order_status for the stale id raises the real HTTP-400 shape of -2013.
    def missing_order_status(*, symbol: str, client_id: str) -> str:
        raise BinanceAPIError('Binance HTTP 400: {"code":-2013,"msg":"Order does not exist."}')

    client.order_status = missing_order_status  # type: ignore[method-assign]

    result = binance_usdm_testnet._replace_pending_limit(client, "SOLUSDT")

    assert result is None, "stale pending must not block a fresh entry"
    with binance_usdm_testnet._STATE_LOCK:
        pending = (binance_usdm_testnet._load_state().get("pending") or {}).get("SOLUSDT")
    assert pending is None or pending.get("client_id") != old_client_id


# ── P0-1: 防双开 — 账户已有持仓时拒绝新入场 ────────────────────────────

def test_net_position_parses_open_amount() -> None:
    """GET /fapi/v2/positionRisk returns the signed position amount."""

    class _ListResp:
        def __init__(self, payload: list) -> None:
            self._raw = json.dumps(payload).encode("utf-8")

        def __enter__(self) -> _ListResp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return self._raw

    calls: list[str] = []

    def opener(*args: object, **_kwargs: object) -> _ListResp:
        request = args[0]
        calls.append(str(getattr(request, "full_url", request)))
        return _ListResp([{"symbol": "BTCUSDT", "positionAmt": "-0.2479"}])

    client = binance_usdm_testnet.BinanceUSDMTestnetClient("k", "s", opener=opener)

    assert client.net_position("BTCUSDT") == Decimal("-0.2479")
    assert any("positionRisk" in call for call in calls)


def test_net_position_empty_response_means_flat() -> None:
    """No open position rows → flat (0)."""

    class _EmptyResp:
        def __enter__(self) -> _EmptyResp:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return b"[]"

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "k", "s", opener=lambda *_a, **_k: _EmptyResp()
    )

    assert client.net_position("BTCUSDT") == Decimal("0")


def test_open_position_rejects_market_signal_before_any_entry() -> None:
    """Regression: bot must never stack a new entry on an existing position."""
    client = OpenPositionClient("0.001")
    result = execute_market_signal(
        _long_decision(), _settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "rejected"
    assert "Position already open" in result.reason
    assert "entry" not in [call[0] for call in client.calls]
    assert "protection" not in [call[0] for call in client.calls]
    assert [call[0] for call in client.calls] == ["net_position"]


def test_open_position_rejects_limit_signal_too() -> None:
    """Both market and resting-limit entries must respect an open position."""
    client = OpenPositionClient("-1.5")
    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Position already open" in result.reason
    assert "limit_entry" not in [call[0] for call in client.calls]
    with binance_usdm_testnet._STATE_LOCK:
        state = binance_usdm_testnet._load_state()
    assert not (state.get("pending") or {})


# ── P0-2: 止损距入场过近时拒绝下单 ─────────────────────────────────────

def test_stop_too_close_to_market_price_rejects_entry() -> None:
    client = FakeClient()
    # mark = 100; stop 99.95 → 0.05% gap < 0.2% minimum
    decision = _long_decision() | {"stop_loss_price": 99.95}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Stop loss too close" in result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_stop_at_minimum_distance_proceeds_market_entry() -> None:
    settings = _settings()
    settings.binance_usdm_testnet.min_stop_distance_pct = 0.2
    client = FakeClient()
    # gap 0.3% >= 0.2% minimum → allowed
    decision = _long_decision() | {"stop_loss_price": 99.7}
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "submitted", result.reason
    assert "entry" in [call[0] for call in client.calls]


def test_stop_too_close_rejects_resting_limit_entry() -> None:
    client = FakeClient()
    # resting long limit at 95, stop 94.9 → 0.105% gap < 0.2%
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 95,
        "stop_loss_price": 94.9,
        "take_profit_price": 120,
    }
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Stop loss too close" in result.reason
    assert "limit_entry" not in [call[0] for call in client.calls]
    with binance_usdm_testnet._STATE_LOCK:
        state = binance_usdm_testnet._load_state()
    assert not (state.get("pending") or {})


def test_crossed_limit_with_close_stop_rejected_before_market_fallback() -> None:
    """A limit that would cross (fill immediately) must still be gated on stop distance."""
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 120,
        "stop_loss_price": 99.95,  # vs mark 100 → 0.05% gap
        "take_profit_price": 150,
    }
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Stop loss too close" in result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_crossed_limit_with_safe_stop_keeps_market_fallback() -> None:
    settings = _settings()
    settings.binance_usdm_testnet.min_stop_distance_pct = 0.2
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 120,
        "stop_loss_price": 99.7,  # vs mark 100 → 0.3% gap >= minimum
        "take_profit_price": 150,
    }
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "submitted", result.reason
    assert "crossed mark price" in result.reason
    assert "entry" in [call[0] for call in client.calls]


def test_stop_distance_minimum_is_configurable() -> None:
    settings = _settings()
    settings.binance_usdm_testnet.min_stop_distance_pct = 1.0
    client = FakeClient()
    decision = _long_decision() | {"stop_loss_price": 99.5}  # 0.5% gap
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Stop loss too close" in result.reason
# ── P0-3: 限价部分成交后绝不裸仓 ───────────────────────────────────────

def _persist_pending_record(symbol: str, client_id: str, signal_id: str) -> None:
    binance_usdm_testnet._persist_pending(
        symbol,
        {
            "client_id": client_id,
            "signal_id": signal_id,
            "side": "BUY",
            "quantity": "167.79",
            "stop": "90",
            "target": "120",
            "placed_at": time.time() - 3600,
        },
    )


class PartialOpenClient(FakeClient):
    """Account holding the partially-filled remainder of a limit entry."""

    def __init__(self, amount: str = "91.05") -> None:
        super().__init__()
        self._amount = amount

    def net_position(self, symbol: str) -> Decimal:
        self.calls.append(("net_position", symbol))
        return Decimal(self._amount)


class FailTpPartialClient(PartialOpenClient):
    """Partial-fill account whose TAKE_PROFIT protection leg is rejected."""

    def place_close_algo_order(self, **kwargs: object) -> None:
        super().place_close_algo_order(**kwargs)
        if len([call for call in self.calls if call[0] == "protection"]) == 2:
            raise BinanceAPIError("take-profit rejected")


def _run_watcher_on_partial(
    client: FakeClient,
    monkeypatch,
    *,
    statuses: list[str],
    jump_past_deadline: bool,
) -> None:
    client.statuses["pa-entry-partial"] = list(statuses)
    if jump_past_deadline:
        clock = iter((0.0, 2.0))

        def fake_monotonic() -> float:
            return next(clock, 2.0)

        monkeypatch.setattr(binance_usdm_testnet.time, "monotonic", fake_monotonic)
    binance_usdm_testnet._watch_limit_entry(
        client=client,
        symbol="BTCUSDT",
        client_id="pa-entry-partial",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        quantity=Decimal("167.79"),
        signal_id="partial-signal",
        timeout_seconds=1.0,
        poll_interval=0.1,
    )


def test_watcher_timeout_after_partial_fill_attaches_protection(monkeypatch) -> None:
    """Regression (LINKUSDT incident): a timed-out partially-filled entry must
    cancel the resting remainder and protect the partial position instead of
    leaving it naked."""
    client = PartialOpenClient()
    _persist_pending_record("BTCUSDT", "pa-entry-partial", "partial-signal")
    _run_watcher_on_partial(client, monkeypatch, statuses=["PARTIALLY_FILLED"], jump_past_deadline=True)

    assert ("cancel_limit", "pa-entry-partial") in client.calls
    protections = [call[1] for call in client.calls if call[0] == "protection"]
    assert {order["order_type"] for order in protections} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    seen = _state().get("seen") or {}
    assert seen.get("partial-signal"), "protected partial fill must count as a signal"
    assert not _pending_state()


def test_watcher_timeout_partial_fill_protection_failure_rolls_back(monkeypatch) -> None:
    """TP leg rejected on a partial fill: roll the position back, never naked."""
    client = FailTpPartialClient()
    _persist_pending_record("BTCUSDT", "pa-entry-partial", "partial-signal")
    _run_watcher_on_partial(client, monkeypatch, statuses=["PARTIALLY_FILLED"], jump_past_deadline=True)

    rollbacks = [call[1] for call in client.calls if call[0] == "rollback"]
    assert rollbacks, "protection failure must close the partial position"
    assert rollbacks[0]["quantity"] == Decimal("91.05")
    assert not _pending_state()
    seen = _state().get("seen") or {}
    assert "partial-signal" not in seen


def test_watcher_terminal_cancel_with_partial_fill_still_protects() -> None:
    """Order cancelled elsewhere (UI/manual) after partial fill -> protect."""
    client = PartialOpenClient()
    _persist_pending_record("BTCUSDT", "pa-entry-partial", "partial-signal")
    _run_watcher_on_partial(client, None, statuses=["CANCELED"], jump_past_deadline=False)

    protections = [call[1] for call in client.calls if call[0] == "protection"]
    assert {order["order_type"] for order in protections} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert "cancel_limit" not in [call[0] for call in client.calls]
    assert not _pending_state()


def test_partial_stale_pending_protected_before_duplicate_rejection() -> None:
    """New signal must not discard a previous partial fill without protection."""
    client = PartialOpenClient("91.05")
    _persist_pending_record("BTCUSDT", "pa-entry-partial", "partial-signal")
    client.statuses["pa-entry-partial"] = ["PARTIALLY_FILLED"]
    decision = _long_decision() | {"order_type": "限价单", "entry_price": 95}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Position already open" in result.reason
    protections = [call for call in client.calls if call[0] == "protection"]
    assert len(protections) == 2
    assert "limit_entry" not in [call[0] for call in client.calls]
    assert not _pending_state()
    seen = _state().get("seen") or {}
    assert seen.get("partial-signal")


def test_resume_pending_limit_watchers_after_restart() -> None:
    """Restart must re-arm fill watchers for resting limit entries."""
    started: list[dict] = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon=True) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            started.append(self.kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(binance_usdm_testnet.threading, "Thread", FakeThread)
    settings = _settings()
    settings.binance_usdm_testnet.limit_fill_timeout_minutes = 60
    with binance_usdm_testnet._STATE_LOCK:
        state = binance_usdm_testnet._load_state()
        state.setdefault("pending", {})["BTCUSDT"] = {
            "client_id": "pa-entry-fresh",
            "signal_id": "fresh-signal",
            "side": "BUY",
            "quantity": "0.2",
            "stop": "90",
            "target": "120",
            "placed_at": time.time() - 600,
        }
        state["pending"]["ETHUSDT"] = {
            "client_id": "pa-entry-expired",
            "signal_id": "expired-signal",
            "side": "BUY",
            "quantity": "0.2",
            "stop": "90",
            "target": "120",
            "placed_at": time.time() - 7200,
        }
        binance_usdm_testnet._save_state(state)

    count = binance_usdm_testnet.resume_pending_limit_watchers(
        settings, client=PartialOpenClient()
    )
    assert count == 2
    by_symbol = {record["symbol"]: record for record in started}
    assert by_symbol["BTCUSDT"]["client_id"] == "pa-entry-fresh"
    assert by_symbol["BTCUSDT"]["timeout_seconds"] == pytest.approx(3000.0, abs=2.0)
    assert by_symbol["ETHUSDT"]["client_id"] == "pa-entry-expired"
    assert by_symbol["ETHUSDT"]["timeout_seconds"] == 0.0

    # Disabled automation must not spawn watchers.
    settings.binance_usdm_testnet.enabled = False
    started.clear()
    assert binance_usdm_testnet.resume_pending_limit_watchers(
        settings, client=PartialOpenClient()
    ) == 0
    assert started == []
    monkeypatch.undo()

# ---- 保本移动止损 (breakeven guard) ------------------------------------

class MarkSeqClient(FakeClient):
    """Open long position at 100 with a mark-price sequence."""

    def __init__(self, marks: list[float]) -> None:
        super().__init__()
        self._marks = [Decimal(str(m)) for m in marks]
        self.pos = {"amount": Decimal("100"), "entry": Decimal("100")}

    def position_info(self, symbol: str) -> dict:
        self.calls.append(("position_info", symbol))
        return dict(self.pos)

    def mark_price(self, symbol: str) -> Decimal:
        self.calls.append(("mark_price", symbol))
        return self._marks.pop(0) if self._marks else Decimal("100")


def _register_test_guard() -> None:
    binance_usdm_testnet._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-old0001", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 60, "ts": time.time(), "moved": False},
    )


def test_guard_trigger_1r_moves_stop_to_entry(monkeypatch) -> None:
    """浮盈达 1R 后: 撤销旧 STOP 并在入场价重挂, 注册表标记 moved。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = MarkSeqClient([95, 105, 111])  # 95/105 未达 1R, 111 达 1.1R
    _register_test_guard()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0
    )
    cancels = [c[1] for c in client.calls if c[0] == "cancel_protection"]
    assert cancels, "must cancel the original stop"
    assert cancels[0]["client_algo_id"] == "pa-sl-old0001"
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert places, "must place a replacement stop"
    assert places[-1]["stop_price"] == Decimal("100")
    assert places[-1]["order_type"] == "STOP_MARKET"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True
    assert record["stop_algo_id"] != "pa-sl-old0001"


def test_guard_tp_trigger_needs_target_touch(monkeypatch) -> None:
    """tp 触发: 浮盈 0.5R 不动, 触 TP1 才移。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = MarkSeqClient([105, 121])
    _register_test_guard()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="tp", poll_seconds=1.0
    )
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True
    assert len(sleeps) == 1  # first poll not armed, second armed


def test_guard_trigger_reached_math() -> None:
    trig = binance_usdm_testnet._guard_trigger_reached
    long = dict(entry=Decimal("100"), stop0=Decimal("90"),
                target=Decimal("120"), side="BUY")
    assert trig(mark=Decimal("111"), trigger="1r", **long)
    assert not trig(mark=Decimal("105"), trigger="1r", **long)
    assert not trig(mark=Decimal("105"), trigger="tp", **long)
    assert trig(mark=Decimal("121"), trigger="tp", **long)
    assert trig(mark=Decimal("121"), trigger="1r_or_tp", **long)
    short = dict(entry=Decimal("100"), stop0=Decimal("110"),
                 target=Decimal("80"), side="SELL")
    assert trig(mark=Decimal("89"), trigger="1r", **short)
    assert not trig(mark=Decimal("91"), trigger="1r", **short)


def test_guard_trigger_fractional_r_math() -> None:
    trig = binance_usdm_testnet._guard_trigger_reached
    long = dict(entry=Decimal("100"), stop0=Decimal("90"),
                target=Decimal("120"), side="BUY")
    # risk = 10; 0.5r fires at float profit >= 5 (mark 105+).
    assert trig(mark=Decimal("105"), trigger="0.5r", **long)
    assert not trig(mark=Decimal("104"), trigger="0.5r", **long)
    # 0.25r fires at >= 2.5 (mark 103+).
    assert trig(mark=Decimal("103"), trigger="0.25r", **long)
    assert not trig(mark=Decimal("102"), trigger="0.25r", **long)
    # 1r keeps legacy semantics via the same numeric path.
    assert trig(mark=Decimal("111"), trigger="1r", **long)
    assert not trig(mark=Decimal("105"), trigger="1r", **long)
    short = dict(entry=Decimal("100"), stop0=Decimal("110"),
                 target=Decimal("80"), side="SELL")
    assert trig(mark=Decimal("95"), trigger="0.5r", **short)
    assert not trig(mark=Decimal("96"), trigger="0.5r", **short)
    # Adverse side never fires.
    assert not trig(mark=Decimal("95"), trigger="0.5r", **long)


def test_tp_partial_close_pct_setting_bounds() -> None:
    """TP1 部分止盈比例: 0=关闭(默认), 0< x <=100 有效, 越界拒绝。"""
    from pydantic import ValidationError

    assert BinanceUSDMTestnetSettings().tp_partial_close_pct == 0.0
    assert BinanceUSDMTestnetSettings(tp_partial_close_pct=0).tp_partial_close_pct == 0.0
    assert BinanceUSDMTestnetSettings(tp_partial_close_pct=50).tp_partial_close_pct == 50.0
    assert BinanceUSDMTestnetSettings(tp_partial_close_pct=100).tp_partial_close_pct == 100.0
    for bad in (-1, 101):
        with pytest.raises(ValidationError):
            BinanceUSDMTestnetSettings(tp_partial_close_pct=bad)


def test_min_stop_distance_default_bounds() -> None:
    """P1-1: min_stop 默认 0.45% (原 0.2%), 越界拒绝。"""
    from pydantic import ValidationError

    assert BinanceUSDMTestnetSettings().min_stop_distance_pct == 0.45
    assert BinanceUSDMTestnetSettings(min_stop_distance_pct=0.0).min_stop_distance_pct == 0.0
    assert BinanceUSDMTestnetSettings(min_stop_distance_pct=1.0).min_stop_distance_pct == 1.0
    for bad in (-0.1, 10.1):
        with pytest.raises(ValidationError):
            BinanceUSDMTestnetSettings(min_stop_distance_pct=bad)


def test_breakeven_trigger_setting_accepts_fractional_r() -> None:
    from pydantic import ValidationError

    assert BinanceUSDMTestnetSettings().breakeven_stop_trigger == "1r"
    assert BinanceUSDMTestnetSettings(breakeven_stop_trigger="0.5r").breakeven_stop_trigger == "0.5r"
    assert BinanceUSDMTestnetSettings(breakeven_stop_trigger="0.25r").breakeven_stop_trigger == "0.25r"
    assert BinanceUSDMTestnetSettings(breakeven_stop_trigger="tp").breakeven_stop_trigger == "tp"
    assert BinanceUSDMTestnetSettings(breakeven_stop_trigger="1r_or_tp").breakeven_stop_trigger == "1r_or_tp"
    assert BinanceUSDMTestnetSettings(breakeven_stop_trigger="off").breakeven_stop_trigger == "off"
    for bad in ("2r", "-0.5r", "abc", "1.5r", ""):
        with pytest.raises(ValidationError):
            BinanceUSDMTestnetSettings(breakeven_stop_trigger=bad)


def test_maybe_guard_policy_and_registration(monkeypatch) -> None:
    started: list[dict] = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon=True) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            started.append(self.kwargs)

    monkeypatch.setattr(binance_usdm_testnet.threading, "Thread", FakeThread)
    settings = _settings()  # defaults: trigger 1r, min conf 55
    client = FakeClient()
    # conf below floor -> no registry, no thread
    binance_usdm_testnet._maybe_guard(
        client, settings.binance_usdm_testnet, "BTCUSDT", "BUY",
        Decimal("90"), Decimal("120"), "pa-sl-x", 50,
    )
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None
    assert started == []
    # conf above floor -> registered and thread started
    binance_usdm_testnet._maybe_guard(
        client, settings.binance_usdm_testnet, "BTCUSDT", "BUY",
        Decimal("90"), Decimal("120"), "pa-sl-x", 58,
    )
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["conf"] == 58
    assert started and started[0]["symbol"] == "BTCUSDT"
    # feature off -> nothing
    settings.binance_usdm_testnet.breakeven_stop_trigger = "off"
    started.clear()
    binance_usdm_testnet._maybe_guard(
        client, settings.binance_usdm_testnet, "BTCUSDT", "BUY",
        Decimal("90"), Decimal("120"), "pa-sl-y", 90,
    )
    assert started == []
    monkeypatch.undo()


def test_resume_breakeven_guards_skips_moved(monkeypatch) -> None:
    started: list[dict] = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon=True) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            started.append(self.kwargs)

    monkeypatch.setattr(binance_usdm_testnet.threading, "Thread", FakeThread)
    settings = _settings()
    _register_test_guard()
    # not moved + conf ok -> resumed
    assert binance_usdm_testnet.resume_breakeven_guards(
        settings, client=FakeClient()
    ) == 1
    assert started
    started.clear()
    # moved -> skipped
    binance_usdm_testnet._patch_guard("BTCUSDT", moved=True)
    assert binance_usdm_testnet.resume_breakeven_guards(
        settings, client=FakeClient()
    ) == 0
    monkeypatch.undo()

class StaleStopClient(MarkSeqClient):
    """Open long whose resting stop was already removed server-side."""

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))
        raise BinanceAPIError(
            'Binance HTTP 400: {"code":-2011,"msg":"Unknown order sent."}'
        )


class BrokenCancelClient(MarkSeqClient):
    """Open long whose stop cancel keeps failing for non-unknown reasons."""

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))
        raise BinanceAPIError("Binance network error: test outage")


def test_guard_move_still_places_breakeven_when_original_stop_unknown(
    monkeypatch,
) -> None:
    """旧 STOP 已被撤(-2011)时, 保本移动仍必须重挂入场价止损, 不许裸奔。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = StaleStopClient([111])  # mark 111 >= 1R(110)
    _register_test_guard()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0
    )
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert places, "must still place a breakeven stop"
    assert places[-1]["stop_price"] == Decimal("100")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True


def test_guard_gives_up_after_repeated_cancel_errors(monkeypatch) -> None:
    """撤单持续失败(非-2011)时有限重试后放弃, 不会无限空转。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = BrokenCancelClient([111] * 20)  # trigger keeps being reached
    _register_test_guard()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0
    )
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert places == [], "must not place a replacement while cancel keeps failing"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is False
    assert len(sleeps) <= 6  # bounded retries: 5 failures + give-up

# ---- 30d 日线大趋势护栏 ------------------------------------------------

def _trend_settings() -> Settings:
    settings = _settings()
    settings.binance_usdm_testnet.leverage = 20
    return settings


def test_counter_trend_below_confidence_rejected() -> None:
    """Long vs a 30d bear trend with conf<55: rejected before any order flow."""
    settings = _trend_settings()
    client = FakeClient()
    decision = _long_decision() | {"trade_confidence": 50}
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=-20.0,
    )
    assert result.status == "rejected"
    assert "Counter-trend vs 30d trend" in result.reason
    assert "50.0 < 55" in result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_counter_trend_high_confidence_scales_leverage() -> None:
    """Counter-30d-trend but conf>=55: allowed with leverage 20x -> 10x (half notional)."""
    settings = _trend_settings()
    client = FakeClient()
    decision = _long_decision() | {"trade_confidence": 65}
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=-20.0,
    )
    assert result.status == "submitted", result.reason
    assert "scaled leverage to 10x" in result.reason
    assert ("set_leverage", "BTCUSDT", 10) in client.calls
    entry = next(call[1] for call in client.calls if call[0] == "entry")
    # notional 20 USDT margin x 10 = 200 @ price 100 -> qty 2 (was 4 at 20x)
    assert entry["quantity"] == Decimal("2")


def test_counter_trend_short_against_bull_scaled() -> None:
    """Short vs a 30d bull trend at conf=55 (boundary): allowed with halved leverage."""
    settings = _trend_settings()
    client = FakeClient()
    decision = _long_decision() | {
        "order_direction": "做空",
        "stop_loss_price": 110,
        "take_profit_price": 90,
        "trade_confidence": 55,
    }
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=12.0,
    )
    assert result.status == "submitted", result.reason
    assert ("set_leverage", "BTCUSDT", 10) in client.calls


def test_neutral_30d_trend_not_restricted() -> None:
    """30d change inside the +/-5% neutral band: no restriction (even low conf)."""
    settings = _trend_settings()
    client = FakeClient()
    decision = _long_decision() | {"trade_confidence": 30}
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=2.0,
    )
    assert result.status == "submitted", result.reason
    assert "counter-trend" not in result.reason.lower()
    assert ("set_leverage", "BTCUSDT", 20) in client.calls


def test_counter_trend_guard_disabled_when_scale_one_and_no_min() -> None:
    """Both switches off: no guard, low-conf counter-trend order proceeds at full size."""
    settings = _trend_settings()
    settings.binance_usdm_testnet.counter_trend_min_confidence = 0
    settings.binance_usdm_testnet.counter_trend_size_scale = 1.0
    client = FakeClient()
    decision = _long_decision() | {"trade_confidence": 40}
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=-20.0,
    )
    assert result.status == "submitted", result.reason
    assert ("set_leverage", "BTCUSDT", 20) in client.calls

# ---- P0-1: TP1 部分止盈 + runner(TP2) --------------------------------

def _partial_settings(pct: float = 50.0) -> Settings:
    settings = _settings()
    settings.binance_usdm_testnet.tp_partial_close_pct = pct
    return settings


def _lot_info(step: str = "0.001", minimum: str = "0.001") -> dict:
    return {"filters": [{"filterType": "LOT_SIZE", "minQty": minimum, "stepSize": step}]}


def test_partial_quantity_floors_to_lot_step_and_validates() -> None:
    """TP1 部分单数量 = 仓位按比例后沿 LOT_SIZE step 向下取整; 不可行回退 None。"""
    q = binance_usdm_testnet._partial_quantity
    info = _lot_info()
    assert q(Decimal("2"), 50.0, info) == Decimal("1")
    assert q(Decimal("167.79"), 50.0, info) == Decimal("83.895")
    assert q(Decimal("0.3"), 50.0, info) == Decimal("0.15")
    assert q(Decimal("2"), 0.0, info) is None  # 关闭
    assert q(Decimal("2"), 100.0, info) is None  # 全平 => 保持现行为
    assert q(Decimal("0.001"), 50.0, info) is None  # 半仓不足 minQty
    assert q(Decimal("2"), 50.0, {"filters": []}) is None  # 无 LOT_SIZE


def test_place_algo_order_reduce_only_quantity_payload() -> None:
    """部分止盈挂单必须带 quantity+reduceOnly, 不能 closePosition。"""
    seen: list[str] = []

    def opener(request, **_kw: object) -> _OkResponse:
        seen.append(str(request.full_url))
        return _OkResponse({"code": 200, "msg": "success"})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=opener
    )
    client.place_close_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="TAKE_PROFIT_MARKET",
        stop_price=Decimal("120"),
        client_algo_id="pa-tp-x0001",
        quantity=Decimal("1.5"),
        close_position=False,
    )
    assert "quantity=1.5" in seen[0], seen[0]
    assert "reduceOnly=true" in seen[0], seen[0]
    assert "closePosition" not in seen[0], seen[0]
    seen.clear()
    client.place_close_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        stop_price=Decimal("90"),
        client_algo_id="pa-sl-x0001",
    )
    assert "closePosition=true" in seen[0], seen[0]
    assert "quantity" not in seen[0], seen[0]


def test_attach_protection_partial_places_half_qty_tp1_and_full_sl() -> None:
    """partial_pct>0 时: SL 仍 closePosition, TP1 改为 reduceOnly 半仓单。"""
    client = FakeClient()
    sl_id, tp_id, _plan_qty = binance_usdm_testnet._attach_protection(
        client, "BTCUSDT", "BUY", Decimal("90"), Decimal("120"),
        quantity=Decimal("2"), target2=Decimal("150"), partial_pct=50.0,
    )
    assert sl_id.startswith("pa-sl-")
    assert tp_id.startswith("pa-tp-")
    prot = [call[1] for call in client.calls if call[0] == "protection"]
    assert prot[0]["order_type"] == "STOP_MARKET"
    assert prot[0]["stop_price"] == Decimal("90")
    assert "quantity" not in prot[0]
    tp = prot[1]
    assert tp["order_type"] == "TAKE_PROFIT_MARKET"
    assert tp["stop_price"] == Decimal("120")
    assert tp["quantity"] == Decimal("1")
    assert tp.get("close_position") is False


def test_attach_protection_falls_back_to_full_tp1_when_infeasible() -> None:
    """无 TP2 / 半仓不可行时回退现状全平 TP1, 不许裸仓。"""
    client = FakeClient()
    binance_usdm_testnet._attach_protection(
        client, "BTCUSDT", "BUY", Decimal("90"), Decimal("120"),
        quantity=Decimal("2"), target2=None, partial_pct=50.0,
    )
    prot = [call[1] for call in client.calls if call[0] == "protection"]
    assert "quantity" not in prot[1]
    assert prot[1].get("close_position") is not False
    client = FakeClient()
    binance_usdm_testnet._attach_protection(
        client, "BTCUSDT", "BUY", Decimal("90"), Decimal("120"),
        quantity=Decimal("2"), target2=Decimal("150"), partial_pct=100.0,
    )
    prot = [call[1] for call in client.calls if call[0] == "protection"]
    assert "quantity" not in prot[1]


def test_attach_protection_partial_failure_cancels_placed_orders() -> None:
    """部分 TP1 挂单失败时, 已挂成功的 SL 也必须撤, 防孤儿全平单。"""
    client = FailSecondProtectionClient()
    with pytest.raises(BinanceAPIError, match="take-profit rejected"):
        binance_usdm_testnet._attach_protection(
            client, "BTCUSDT", "BUY", Decimal("90"), Decimal("120"),
            quantity=Decimal("2"), target2=Decimal("150"), partial_pct=50.0,
        )
    cancels = [call[1] for call in client.calls if call[0] == "cancel_protection"]
    assert len(cancels) == 1
    assert cancels[0]["client_algo_id"].startswith("pa-sl-")
    # TP 单从未被 attach 计入, 不会被撤(服务器端未挂出) -> 无需孤儿清理

class PartialRunnerClient(FakeClient):
    """Open long (qty 2) whose amount drops once the TP1 partial fires."""

    def __init__(self, amounts: list[float]) -> None:
        super().__init__()
        self._amounts = [Decimal(str(a)) for a in amounts]
        self._last_amount = Decimal("0")

    def position_info(self, symbol: str) -> dict:
        self.calls.append(("position_info", symbol))
        if self._amounts:
            self._last_amount = self._amounts.pop(0)
        return {"amount": self._last_amount, "entry": Decimal("100")}


class StaleStopRunnerClient(PartialRunnerClient):
    """Runner account whose resting stop was already removed server-side."""

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))
        raise BinanceAPIError(
            'Binance HTTP 400: {"code":-2011,"msg":"Unknown order sent."}'
        )


class BrokenCancelRunnerClient(PartialRunnerClient):
    """Runner account whose stop cancel keeps failing for non-missing reasons."""

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))
        raise BinanceAPIError("Binance network error: test outage")


def _register_tp_record(symbol: str = "BTCUSDT", extra: dict | None = None) -> None:
    record = {
        "stop_algo_id": "pa-sl-old0001",
        "tp_algo_id": "pa-tp-part0001",
        "stop0": "90",
        "target": "120",
        "target2": "150",
        "qty": "2",
        "partial_qty": "1",
        "side": "BUY",
        "conf": 60,
        "ts": time.time(),
        "moved": False,
        "partial_done": False,
    }
    if extra:
        record.update(extra)
    binance_usdm_testnet._register_guard(symbol, record)


def _run_tp_runner(client: FakeClient, monkeypatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    binance_usdm_testnet._tp_runner_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0
    )
    return sleeps


def _tp_places(client: FakeClient) -> list[dict]:
    return [call[1] for call in client.calls if call[0] == "protection"]


def _tp_cancels(client: FakeClient) -> list[dict]:
    return [call[1] for call in client.calls if call[0] == "cancel_protection"]


def test_tp_runner_swaps_to_breakeven_and_tp2_after_half_close(monkeypatch) -> None:
    """TP1 半仓触发后: 撤原 SL, 挂入场价 STOP + TP2 TAKE_PROFIT, 标记完成。"""
    _register_tp_record()
    client = PartialRunnerClient([2, 2, 1])
    sleeps = _run_tp_runner(client, monkeypatch)
    assert len(sleeps) == 2  # 前两次量未变, 第三轮才触发半仓
    cancels = _tp_cancels(client)
    assert [c["client_algo_id"] for c in cancels] == [
        "pa-tp-part0001",  # 先清 TP1 残单(已成交则视为已清)
        "pa-sl-old0001",  # 再撤原止损
    ]
    places = _tp_places(client)
    assert len(places) == 2
    assert places[0]["order_type"] == "STOP_MARKET"
    assert places[0]["stop_price"] == Decimal("100")
    assert places[1]["order_type"] == "TAKE_PROFIT_MARKET"
    assert places[1]["stop_price"] == Decimal("150")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["moved"] is True
    assert record["partial_done"] is True
    assert record["stop_algo_id"] != "pa-sl-old0001"
    assert record["tp_algo_id"] != "pa-tp-part0001"


def test_tp_runner_cleans_residual_tp1_when_position_closed(monkeypatch) -> None:
    """仓位归零(止损/手动): 清理残留 TP1 部分单并移除注册记录。"""
    _register_tp_record()
    client = PartialRunnerClient([0])
    sleeps = _run_tp_runner(client, monkeypatch)
    assert sleeps == []
    cancels = _tp_cancels(client)
    assert len(cancels) == 1
    assert cancels[0]["client_algo_id"] == "pa-tp-part0001"
    assert _tp_places(client) == []
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_tp_runner_with_breakeven_already_moved_places_tp2_only(monkeypatch) -> None:
    """保本已先移(0.5r 早于 TP1): 不再动 SL, 只补挂 TP2 全平单。"""
    _register_tp_record(extra={"moved": True, "stop_algo_id": "pa-sl-entry1"})
    client = PartialRunnerClient([2, 1])
    _run_tp_runner(client, monkeypatch)
    cancels = _tp_cancels(client)
    assert [c["client_algo_id"] for c in cancels] == ["pa-tp-part0001"]
    assert not [c for c in cancels if c["client_algo_id"].startswith("pa-sl-")]
    places = _tp_places(client)
    assert len(places) == 1
    assert places[0]["order_type"] == "TAKE_PROFIT_MARKET"
    assert places[0]["stop_price"] == Decimal("150")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["partial_done"] is True
    assert record["moved"] is True
    assert record["stop_algo_id"] == "pa-sl-entry1"  # 原保本单保留


def test_tp_runner_tolerates_missing_stop_on_swap(monkeypatch) -> None:
    """撤原 SL 遇 -2011(已被撤): 视为已撤, 照常补挂保本+TP2。"""
    _register_tp_record()
    client = StaleStopRunnerClient([2, 1])
    _run_tp_runner(client, monkeypatch)
    places = _tp_places(client)
    assert len(places) == 2
    assert places[0]["stop_price"] == Decimal("100")
    assert places[1]["stop_price"] == Decimal("150")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["partial_done"] is True and record["moved"] is True


def test_tp_runner_gives_up_after_repeated_cancel_failures(monkeypatch) -> None:
    """撤单持续失败(非 -2011)时有限重试后放弃, 不无限空转。"""
    _register_tp_record()
    client = BrokenCancelRunnerClient([1] * 10)
    sleeps = _run_tp_runner(client, monkeypatch)
    assert _tp_places(client) == []
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["partial_done"] is False and record["moved"] is False
    assert len(sleeps) <= 5


def test_tp_runner_ignores_legacy_guard_record(monkeypatch) -> None:
    """无 partial 字段的旧 guard 记录(非本功能)直接退出, 不动单不删记录。"""
    binance_usdm_testnet._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-old0001", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 60, "ts": time.time(), "moved": False},
    )
    client = PartialRunnerClient([1])
    sleeps = _run_tp_runner(client, monkeypatch)
    assert sleeps == []
    assert client.calls == []
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is False


def _capture_threads(monkeypatch) -> list:
    captured: list = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon=True) -> None:
            self.target = target
            self.kwargs = kwargs

        def start(self) -> None:
            captured.append((self.target, self.kwargs))

    monkeypatch.setattr(binance_usdm_testnet.threading, "Thread", FakeThread)
    return captured


def test_maybe_guard_partial_registers_and_arms_runner(monkeypatch) -> None:
    """pct>0: 注册表补存 qty/partial_qty/target2/tp_algo_id, guard+runner 双线程。"""
    captured = _capture_threads(monkeypatch)
    config = _partial_settings().binance_usdm_testnet
    binance_usdm_testnet._maybe_guard(
        client=FakeClient(),
        config=config,
        symbol="BTCUSDT",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        stop_algo_id="pa-sl-x",
        conf=58,
        quantity=Decimal("2"),
        target2=Decimal("150"),
        tp_algo_id="pa-tp-x",
        partial_qty=Decimal("1"),
    )
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["qty"] == "2"
    assert record["partial_qty"] == "1"
    assert record["target2"] == "150"
    assert record["tp_algo_id"] == "pa-tp-x"
    assert record["moved"] is False
    assert record["partial_done"] is False
    targets = [target for target, _kwargs in captured]
    assert binance_usdm_testnet._breakeven_guard_loop in targets
    assert binance_usdm_testnet._tp_runner_loop in targets
    runner_kwargs = [kwargs for target, kwargs in captured
                     if target is binance_usdm_testnet._tp_runner_loop]
    assert runner_kwargs and runner_kwargs[0]["symbol"] == "BTCUSDT"
    assert runner_kwargs[0]["poll_seconds"] == config.breakeven_poll_seconds


def test_maybe_guard_partial_without_breakeven_still_arms_runner(monkeypatch) -> None:
    """保本 off + 部分止盈 on: 只起 runner 线程, 注册表照常。"""
    captured = _capture_threads(monkeypatch)
    config = _partial_settings().binance_usdm_testnet
    config.breakeven_stop_trigger = "off"
    binance_usdm_testnet._maybe_guard(
        client=FakeClient(),
        config=config,
        symbol="BTCUSDT",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        stop_algo_id="pa-sl-x",
        conf=58,
        quantity=Decimal("2"),
        target2=Decimal("150"),
        tp_algo_id="pa-tp-x",
        partial_qty=Decimal("1"),
    )
    targets = [target for target, _kwargs in captured]
    assert targets == [binance_usdm_testnet._tp_runner_loop]
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None and record["partial_done"] is False


class TimeStopClient(FakeClient):
    """Position stays open until close_market_position is called."""

    def __init__(self, amount: Decimal = Decimal("0.5")) -> None:
        super().__init__()
        self.amount = amount

    def position_info(self, symbol: str) -> dict:
        self.calls.append(("position_info", symbol))
        return {"amount": self.amount, "entry": Decimal("95")}

    def close_market_position(self, **kwargs: object) -> None:
        super().close_market_position(**kwargs)
        self.amount = Decimal("0")


def _legacy_record(ts: float, symbol: str = "BTCUSDT") -> None:
    binance_usdm_testnet._register_guard(
        symbol,
        {"stop_algo_id": "pa-sl-x", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 55, "ts": ts, "moved": False},
    )


def test_time_stop_setting_bounds() -> None:
    """P2-2: 持仓超时分钟数 0=关闭默认, 1..10080 有效, 越界拒绝."""
    from pydantic import ValidationError

    assert BinanceUSDMTestnetSettings().time_stop_minutes == 0
    assert BinanceUSDMTestnetSettings(time_stop_minutes=360).time_stop_minutes == 360
    for bad in (-1, 10081):
        with pytest.raises(ValidationError):
            BinanceUSDMTestnetSettings(time_stop_minutes=bad)


def test_timestop_deadline_hit_math() -> None:
    """P2-2: 到期判定: 无记录/无 ts/未到期 False, 到期 True."""
    hit = binance_usdm_testnet._timestop_deadline_hit
    now = 1_000_000.0
    record = {"ts": now - 3600, "side": "BUY"}
    assert hit(record, 60, now=now) is True
    assert hit({"ts": now - 60, "side": "BUY"}, 10, now=now) is False
    young = {"ts": now - 300, "side": "BUY"}
    assert hit(young, 10, now=now) is False
    assert hit(young, 5, now=now) is True
    assert hit(None, 10, now=now) is False
    assert hit({}, 10, now=now) is False
    assert hit({"ts": 0, "side": "BUY"}, 10, now=now) is False
    assert hit({"ts": now - 60, "side": "BUY"}, 0, now=now) is False


def test_timestop_loop_closes_after_deadline_and_drops() -> None:
    """P2-2: 到期后市价清剩余仓位并移除无 partial 记录."""
    _legacy_record(time.time() - 7200)
    client = TimeStopClient()
    thread = binance_usdm_testnet.threading.Thread(
        target=binance_usdm_testnet._timestop_loop,
        kwargs={"client": client, "symbol": "BTCUSDT",
                "stop_minutes": 60.0, "poll_seconds": 0.02},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any(c[0] == "rollback" for c in client.calls):
        time.sleep(0.02)
    thread.join(timeout=2)
    assert not thread.is_alive()
    closes = [c[1] for c in client.calls if c[0] == "rollback"]
    assert closes and closes[0]["side"] == "SELL"
    assert closes[0]["quantity"] == Decimal("0.5")
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_timestop_loop_young_record_waits_then_closes() -> None:
    """P2-2: 未到期不动作; 记录 ts 推旧后同一线程到期平仓."""
    _legacy_record(time.time() - 60)  # 1 分钟持仓, 阈值 60min
    client = TimeStopClient()
    thread = binance_usdm_testnet.threading.Thread(
        target=binance_usdm_testnet._timestop_loop,
        kwargs={"client": client, "symbol": "BTCUSDT",
                "stop_minutes": 60.0, "poll_seconds": 0.02},
        daemon=True,
    )
    thread.start()
    time.sleep(0.12)
    assert not any(c[0] == "rollback" for c in client.calls)
    _legacy_record(time.time() - 7200)  # 模拟记录被替换成更旧的持仓
    deadline = time.time() + 5
    while time.time() < deadline and not any(c[0] == "rollback" for c in client.calls):
        time.sleep(0.02)
    thread.join(timeout=2)
    assert any(c[0] == "rollback" for c in client.calls)
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_timestop_loop_flat_position_drops_stale_record() -> None:
    """P2-2: 记录在但仓位已平(无 partial): 清残留后移除记录, 不平仓."""
    _legacy_record(time.time() - 7200)
    client = TimeStopClient(amount=Decimal("0"))
    thread = binance_usdm_testnet.threading.Thread(
        target=binance_usdm_testnet._timestop_loop,
        kwargs={"client": client, "symbol": "BTCUSDT",
                "stop_minutes": 60.0, "poll_seconds": 0.02},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not any(c[0] == "rollback" for c in client.calls)
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_timestop_loop_keeps_partial_record_for_runner() -> None:
    """P2-2: 半仓未触发时 runner 仍存活: time-stop 平仓后不撤 TP1 不移记录."""
    binance_usdm_testnet._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-x", "stop0": "90", "target": "120", "side": "BUY",
         "conf": 55, "ts": time.time() - 7200, "moved": False,
         "tp_algo_id": "pa-tp-x", "target2": "150", "qty": "1",
         "partial_qty": "0.5", "partial_done": False},
    )
    client = TimeStopClient(amount=Decimal("1"))
    thread = binance_usdm_testnet.threading.Thread(
        target=binance_usdm_testnet._timestop_loop,
        kwargs={"client": client, "symbol": "BTCUSDT",
                "stop_minutes": 60.0, "poll_seconds": 0.02},
        daemon=True,
    )
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any(c[0] == "rollback" for c in client.calls):
        time.sleep(0.02)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert any(c[0] == "rollback" for c in client.calls)
    assert not any(c[0] == "cancel_protection" for c in client.calls)
    assert binance_usdm_testnet._read_guard("BTCUSDT") is not None


def test_timestop_loop_tp2_phase_flat_cancels_residual_and_drops() -> None:
    """P2-2: TP2 阶段(partial_done, runner 已退)仓位已平: 撤残留 TP1 并移除记录."""
    binance_usdm_testnet._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-x2", "stop0": "90", "target": "120", "side": "BUY",
         "conf": 55, "ts": time.time() - 7200, "moved": True,
         "tp_algo_id": "pa-tp-x", "target2": "150", "qty": "1",
         "partial_qty": "0.5", "partial_done": True},
    )
    client = TimeStopClient(amount=Decimal("0"))
    thread = binance_usdm_testnet.threading.Thread(
        target=binance_usdm_testnet._timestop_loop,
        kwargs={"client": client, "symbol": "BTCUSDT",
                "stop_minutes": 60.0, "poll_seconds": 0.02},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    cancels = [c[1] for c in client.calls if c[0] == "cancel_protection"]
    assert cancels and cancels[0]["client_algo_id"] == "pa-tp-x"
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_maybe_guard_arms_timestop_when_configured(monkeypatch) -> None:
    """P2-2: time_stop_minutes>0 时注册记录并额外拉起 time-stop 线程."""
    captured = _capture_threads(monkeypatch)
    config = _partial_settings().binance_usdm_testnet
    config.time_stop_minutes = 60
    binance_usdm_testnet._maybe_guard(
        client=FakeClient(),
        config=config,
        symbol="BTCUSDT",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        stop_algo_id="pa-sl-x",
        conf=58,
        quantity=Decimal("2"),
        target2=Decimal("150"),
        tp_algo_id="pa-tp-x",
        partial_qty=Decimal("1"),
    )
    targets = [target for target, _kwargs in captured]
    assert binance_usdm_testnet._timestop_loop in targets
    ts_kwargs = [kwargs for target, kwargs in captured
                 if target is binance_usdm_testnet._timestop_loop]
    assert ts_kwargs and ts_kwargs[0]["stop_minutes"] == 60
    assert ts_kwargs[0]["poll_seconds"] == config.breakeven_poll_seconds


def test_resume_time_stops_arms_all_record_kinds(monkeypatch) -> None:
    """P2-2: 重启恢复: 所有含 side/ts 的记录(含 moved/partial_done)都拉起."""
    captured = _capture_threads(monkeypatch)
    settings = _partial_settings()
    settings.binance_usdm_testnet.time_stop_minutes = 60
    _legacy_record(time.time() - 3600, "BTCUSDT")
    _legacy_record(time.time() - 3600, "ADAUSDT")
    binance_usdm_testnet._register_guard(
        "ETHUSDT",
        {"stop_algo_id": "pa-sl-y", "stop0": "90", "target": "120", "side": "BUY",
         "conf": 60, "ts": time.time() - 3600, "moved": True,
         "tp_algo_id": "pa-tp-y", "target2": "150", "qty": "1",
         "partial_qty": "0.5", "partial_done": True},
    )
    count = binance_usdm_testnet.resume_time_stops(settings, client=FakeClient())
    assert count == 3
    entries = sorted((kwargs["symbol"]) for _t, kwargs in captured)
    assert entries == ["ADAUSDT", "BTCUSDT", "ETHUSDT"]
    # 关闭(0 分钟)不恢复
    captured.clear()
    assert binance_usdm_testnet.resume_time_stops(
        _partial_settings(), client=FakeClient()
    ) == 0
    assert captured == []


def test_resume_tp_runners_arms_unfinished_records_only(monkeypatch) -> None:
    """重启恢复: 只拉起 partial_done=False 的记录; 跳过已完成的与旧 guard。"""
    captured = _capture_threads(monkeypatch)
    settings = _partial_settings()
    _register_tp_record("BTCUSDT")
    _register_tp_record("ETHUSDT", extra={"partial_done": True})
    binance_usdm_testnet._register_guard(
        "ADAUSDT",
        {"stop_algo_id": "pa-sl-old2", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 60, "ts": time.time(), "moved": False},
    )
    count = binance_usdm_testnet.resume_tp_runners(settings, client=FakeClient())
    assert count == 1
    entries = [(target, kwargs["symbol"]) for target, kwargs in captured]
    assert entries == [(binance_usdm_testnet._tp_runner_loop, "BTCUSDT")]
    # 旧 guard 记录(无 partial 字段)不得被删
    record = binance_usdm_testnet._read_guard("ADAUSDT")
    assert record and record["stop_algo_id"] == "pa-sl-old2"
    # pct=0(功能关闭) => 不恢复
    captured.clear()
    assert binance_usdm_testnet.resume_tp_runners(
        _partial_settings(pct=0.0), client=FakeClient()
    ) == 0
    assert captured == []


def test_market_signal_partial_enabled_arms_half_tp1_and_runner(monkeypatch) -> None:
    """市价单 + pct50: TP1 只挂半仓 reduceOnly, 注册并拉起 runner 线程。"""
    captured = _capture_threads(monkeypatch)
    client = FakeClient()
    decision = _long_decision() | {
        "take_profit_price_2": 150,
        "trade_confidence": 70,
    }
    result = execute_market_signal(
        decision, _partial_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "submitted", result.reason
    tps = [call[1] for call in client.calls if call[0] == "protection"
           and call[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    assert tps and tps[0]["quantity"] == Decimal("0.1")
    assert tps[0].get("close_position") is False
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["qty"] == "0.2"
    assert record["partial_qty"] == "0.1"
    assert record["target2"] == "150"
    targets = {target for target, _kwargs in captured}
    assert binance_usdm_testnet._tp_runner_loop in targets
    assert binance_usdm_testnet._breakeven_guard_loop in targets


def test_market_signal_partial_without_tp2_falls_back_legacy(monkeypatch) -> None:
    """decision 缺 take_profit_price_2: 回退全平 TP1, 不起 runner, 不注册。"""
    captured = _capture_threads(monkeypatch)
    client = FakeClient()
    result = execute_market_signal(
        _long_decision(), _partial_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "submitted", result.reason
    tps = [call[1] for call in client.calls if call[0] == "protection"
           and call[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    assert tps and "quantity" not in tps[0]
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None
    assert captured == []


def test_limit_pending_record_stores_target2_and_watcher_uses_it(monkeypatch) -> None:
    """限价单 pending 记录须带 target2, 成交后按部分止盈保护并注册 runner。"""
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 95,
        "take_profit_price_2": 150,
    }
    result = execute_market_signal(
        decision, _partial_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "pending", result.reason
    pending = _pending_state()
    assert pending["BTCUSDT"]["target2"] == "150"
    captured = _capture_threads(monkeypatch)
    client = FakeClient()
    client.statuses["pa-fill-t"] = ["FILLED"]
    binance_usdm_testnet._watch_limit_entry(
        client=client,
        symbol="BTCUSDT",
        client_id="pa-fill-t",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        quantity=Decimal("2"),
        signal_id="fill-signal",
        conf=70,
        config=_partial_settings().binance_usdm_testnet,
        target2=Decimal("150"),
        timeout_seconds=1.0,
        poll_interval=0.1,
    )
    tps = [call[1] for call in client.calls if call[0] == "protection"
           and call[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    assert tps and tps[0]["quantity"] == Decimal("1")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None and record["partial_done"] is False
    assert any(
        target is binance_usdm_testnet._tp_runner_loop for target, _k in captured
    )


class _Tp2PlaceFailRunnerClient(PartialRunnerClient):
    """Runner whose breakeven STOP works but TP2 placement always fails."""

    def place_close_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("protection", kwargs))
        if kwargs["order_type"] == "TAKE_PROFIT_MARKET":
            raise BinanceAPIError("Binance network error: tp2 down")


def test_tp_runner_tp2_failure_keeps_live_breakeven_record(monkeypatch) -> None:
    """TP2 限次失败放弃后: 保本单已成功须写回记录(指向新单), 供重启/重跑补 TP2。"""
    _register_tp_record()
    client = _Tp2PlaceFailRunnerClient([2, 1])
    sleeps = _run_tp_runner(client, monkeypatch)
    assert len(sleeps) == 5  # 1 次全量轮询 + 4 次 TP2 重试退避
    cancels = _tp_cancels(client)
    # 每次重试都会先幂等清 TP1 残单(交易所已撤则 -2011 容忍)
    tp_cancels = [c for c in cancels if c["client_algo_id"].startswith("pa-tp-")]
    assert len(tp_cancels) >= 1
    sl_cancels = [c for c in cancels if c["client_algo_id"].startswith("pa-sl-")]
    assert [c["client_algo_id"] for c in sl_cancels] == ["pa-sl-old0001"]
    places = _tp_places(client)
    stops = [p for p in places if p["order_type"] == "STOP_MARKET"]
    assert len(stops) == 1
    assert stops[0]["stop_price"] == Decimal("100")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["moved"] is True          # 保本已就位
    assert record["partial_done"] is False  # TP2 未完成, 可被再次拉起
    assert record["stop_algo_id"] != "pa-sl-old0001"
    assert record["stop_algo_id"].startswith("pa-sl-")
    # 重新拉起(模拟重启 resume): moved=True 分支只补 TP2, 不再动 SL
    client2 = PartialRunnerClient([1])
    sleeps2 = _run_tp_runner(client2, monkeypatch)
    assert sleeps2 == []
    cancels2 = _tp_cancels(client2)
    assert [c["client_algo_id"] for c in cancels2] == ["pa-tp-part0001"]
    assert not [c for c in cancels2 if c["client_algo_id"].startswith("pa-sl-")]
    tp2 = [p for p in _tp_places(client2) if p["order_type"] == "TAKE_PROFIT_MARKET"]
    assert len(tp2) == 1 and tp2[0]["stop_price"] == Decimal("150")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["partial_done"] is True


class _NoInfoClient(FakeClient):
    """Fake whose exchange_info is down (partial plan must come from attach)."""

    def exchange_info(self, symbol: str) -> dict:
        raise BinanceAPIError("exchange info down")


def test_maybe_guard_uses_attach_partial_plan_without_refetch(monkeypatch) -> None:
    """partial_qty 由 attach 算好传入: 注册不再回取 exchange_info, 两次取值不会不一致。"""
    captured = _capture_threads(monkeypatch)
    config = _partial_settings().binance_usdm_testnet
    client = _NoInfoClient()
    binance_usdm_testnet._maybe_guard(
        client=client,
        config=config,
        symbol="BTCUSDT",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        stop_algo_id="pa-sl-x",
        conf=58,
        quantity=Decimal("2"),
        target2=Decimal("150"),
        tp_algo_id="pa-tp-x",
        partial_qty=Decimal("1"),
    )
    assert client.calls == []  # 未触发任何额外 API(exchange_info 不可用也不报错)
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["qty"] == "2"
    assert record["partial_qty"] == "1"
    assert record["partial_done"] is False
    assert binance_usdm_testnet._tp_runner_loop in [t for t, _k in captured]


def test_maybe_guard_without_attach_plan_skips_partial(monkeypatch) -> None:
    """attach 判定不可行(None)时: 即使给了 quantity/target2 也不注册 runner。"""
    captured = _capture_threads(monkeypatch)
    config = _partial_settings().binance_usdm_testnet
    binance_usdm_testnet._maybe_guard(
        client=FakeClient(),
        config=config,
        symbol="BTCUSDT",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        stop_algo_id="pa-sl-x",
        conf=58,
        quantity=Decimal("2"),
        target2=Decimal("150"),
        tp_algo_id="pa-tp-x",
        partial_qty=None,
    )
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None and "partial_qty" not in record
    targets = [t for t, _k in captured]
    assert binance_usdm_testnet._breakeven_guard_loop in targets
    assert binance_usdm_testnet._tp_runner_loop not in targets


def test_tp_runner_cleanup_failure_keeps_record_for_resume(monkeypatch) -> None:
    """仓位清零后清残留 TP1 连续失败: 记录必须保留(供重启 resume 再撤), 不许丢弃句柄。"""
    _register_tp_record()
    client = BrokenCancelRunnerClient([0])
    sleeps = _run_tp_runner(client, monkeypatch)
    cancels = _tp_cancels(client)
    assert len(cancels) == 5
    assert len(sleeps) == 4  # 5 次失败中的 4 次退避
    assert cancels[0]["client_algo_id"] == "pa-tp-part0001"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["tp_algo_id"] == "pa-tp-part0001"  # 句柄仍在, resume 可重试
    assert record["partial_done"] is False



