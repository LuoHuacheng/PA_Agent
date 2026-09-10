# ruff: noqa: RUF002 - Chinese docstrings
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
from pa_agent.records import cancel_log
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

    def algo_order_status(self, *, client_algo_id: str) -> dict:
        # 默认视为 resting(NEW); 死单场景由测试子类覆盖.
        self.calls.append(("algo_order_status", client_algo_id))
        return {"clientAlgoId": client_algo_id, "algoStatus": "NEW"}

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


def test_rate_limit_failure_is_one_shot_not_retried(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = RateLimitClient(failures=99)
    result = execute_market_signal(
        _long_decision(), _retry_settings(), analysis_symbol="BTCUSDT", client=client
    )
    assert result.status == "failed"
    assert "418" in result.reason
    assert sleeps == [], "rate-limit 失败不得重试/退避"


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


def test_algo_success_body_code_as_string_200_is_not_an_error() -> None:
    """Algo 服务偶发把 code 序列化成字符串 "200": 实测复现, 同样必须视为成功。"""

    def ok_opener(*_args: object, **_kwargs: object) -> _OkResponse:
        return _OkResponse({"code": "200", "msg": "success"})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=ok_opener
    )
    client.cancel_algo_order(client_algo_id="pa-sl-test0002")


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
    # watcher 异步执行: 先挂 protection, 之后才写 seen / 清 pending。只等
    # "protection" 出现会在 _remember_signal 之前跳出, 下面两条断言就会读到
    # 半成品状态(不定时 flaky)。等它真正收尾(seen 已写 + pending 已清)再断言。
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if _state().get("seen") and not _pending_state():
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


# ── 动态止损下限 max(floor, mult × ATR%) ───────────────────────────────

def _atr_settings(*, floor: float = 0.2, multiple: float = 0.8) -> Settings:
    settings = _settings()
    cfg = settings.binance_usdm_testnet
    cfg.min_stop_mode = "atr"
    cfg.min_stop_distance_pct = floor
    cfg.min_stop_atr_multiple = multiple
    return settings


def test_stop_floor_helper_fixed_mode_returns_constant_floor() -> None:
    config = _settings().binance_usdm_testnet
    assert binance_usdm_testnet._stop_distance_floor_pct(config, {"atr_pct": 3.0}) == Decimal("0.45")


def test_stop_floor_helper_atr_mode_uses_max_of_floor_and_multiple() -> None:
    config = _atr_settings().binance_usdm_testnet
    # ATR% 大 → 动态下限 = mult × ATR%
    assert binance_usdm_testnet._stop_distance_floor_pct(config, {"atr_pct": 0.5}) == Decimal("0.4")
    # ATR% 小 → 退回 floor
    assert binance_usdm_testnet._stop_distance_floor_pct(config, {"atr_pct": 0.1}) == Decimal("0.2")
    # 无 ATR 信息 → 退回 floor
    assert binance_usdm_testnet._stop_distance_floor_pct(config, {}) == Decimal("0.2")
    assert binance_usdm_testnet._stop_distance_floor_pct(config, {"atr_pct": None}) == Decimal("0.2")


# ── 计划侧止损抬升到 ATR 动态下限 (P0-2 / Plan B) ─────────────────────

def test_lift_stop_to_min_distance_floor_short_xrp_scenario() -> None:
    """XRPUSDT 30m 复现: entry 1.4213 / SL 1.4267 (gap 0.380%) 低于
    0.7×ATR%(0.6086)=0.426% 下限 → 抬升止损到 tick 对齐 1.4274, RR/方程仍成立。"""
    config = _atr_settings(multiple=0.7).binance_usdm_testnet
    decision = {
        "order_type": "限价单",
        "order_direction": "做空",
        "entry_price": 1.4213,
        "stop_loss_price": 1.4267,
        "take_profit_price": 1.4105,
        "take_profit_price_2": 1.3903,
        "estimated_win_rate": 53,
        "atr_pct": 0.6086,
    }
    changed = binance_usdm_testnet.lift_stop_to_min_distance_floor(
        decision, config, tick=0.0001
    )
    assert changed is True
    assert decision["stop_loss_price"] == 1.4274
    gap = (decision["stop_loss_price"] - 1.4213) / 1.4213 * 100.0
    assert gap >= 0.426


def test_lift_stop_to_min_distance_floor_noop_when_already_above_floor() -> None:
    """止损距离已 ≥ 下限 → 不动止损, 返回 False。"""
    config = _atr_settings(multiple=0.7).binance_usdm_testnet
    decision = {
        "order_type": "限价单",
        "order_direction": "做空",
        "entry_price": 1.4213,
        "stop_loss_price": 1.4280,  # gap ≈ 0.47% ≥ 0.426%
        "take_profit_price": 1.4105,
        "estimated_win_rate": 53,
        "atr_pct": 0.6086,
    }
    changed = binance_usdm_testnet.lift_stop_to_min_distance_floor(
        decision, config, tick=0.0001
    )
    assert changed is False
    assert decision["stop_loss_price"] == 1.4280


def test_lift_stop_to_min_distance_floor_refuses_when_min_rr_breaks() -> None:
    """下限过高会把风险推到超过回报(盈亏比<1) → 拒绝抬升, 保持原样
    （执行层会照旧以 rejected 收尾, 不静默改单）。"""
    settings = _settings()
    settings.binance_usdm_testnet.min_stop_mode = "fixed"
    settings.binance_usdm_testnet.min_stop_distance_pct = 6.0
    config = settings.binance_usdm_testnet
    decision = {
        "order_type": "市价单",
        "order_direction": "做空",
        "entry_price": 100.0,
        "stop_loss_price": 100.2,
        "take_profit_price": 95.0,
        "estimated_win_rate": 60,
    }
    changed = binance_usdm_testnet.lift_stop_to_min_distance_floor(
        decision, config, tick=0.1
    )
    assert changed is False
    assert decision["stop_loss_price"] == 100.2


def test_market_order_uses_dynamic_floor_above_fixed_minimum() -> None:
    """gap 0.3% > floor 0.2% 但 < 0.8×ATR(0.5%)=0.4% → 动态下限拒绝。"""
    settings = _atr_settings()
    client = FakeClient()
    decision = _long_decision() | {
        "stop_loss_price": 99.7,  # vs mark 100 → 0.3% gap (tick 0.1 网格上)
        "atr_pct": 0.5,
    }
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Stop loss too close" in result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_market_order_atr_floor_allows_wide_enough_gap() -> None:
    settings = _atr_settings()
    client = FakeClient()
    decision = _long_decision() | {
        "stop_loss_price": 99.4,  # 0.6% gap >= max(0.2, 0.8×0.5)
        "atr_pct": 0.5,
    }
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "submitted", result.reason


def test_resting_limit_order_uses_dynamic_floor() -> None:
    """限价单 gap 按 entry 价算：0.41% > floor 但 < 0.8×ATR(0.6%)=0.48% → 拒绝。"""
    settings = _atr_settings()
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 98,
        "stop_loss_price": 97.6,
        "take_profit_price": 120,
        "atr_pct": 0.6,
    }
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert "Stop loss too close" in result.reason
    assert "limit_entry" not in [call[0] for call in client.calls]


def test_atr_mode_without_atr_info_falls_back_to_fixed_floor() -> None:
    """mode=atr 但 decision 无 ATR → 退回固定 floor(0.2)，0.25% gap 放行。"""
    settings = _atr_settings()
    client = FakeClient()
    decision = _long_decision() | {"stop_loss_price": 99.75}  # 0.25% gap
    result = execute_market_signal(decision, settings, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "submitted", result.reason


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
            "entry": "100",
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
    """浮盈达 1R 后: 以 reduceOnly 桥接单换仓至入场价(先挂桥接保底, 撤旧, 再挂
    正式 closePosition 保本单), 注册表标记 moved。-4130 下不可先挂新 closePosition。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = MarkSeqClient([95, 105, 111])  # 95/105 未达 1R, 111 达 1.1R
    _register_test_guard()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0
    )
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert len(places) == 2, places
    bridge, canonical = places
    # bridge: reduceOnly+qty STOP at entry (coexists with the old closePosition stop)
    assert bridge["order_type"] == "STOP_MARKET"
    assert bridge["stop_price"] == Decimal("100")
    assert bridge.get("quantity") == Decimal("100")
    assert bridge.get("close_position") is False
    # canonical: closePosition STOP at entry
    assert canonical["order_type"] == "STOP_MARKET"
    assert canonical["stop_price"] == Decimal("100")
    assert "quantity" not in canonical
    seq = [c for c in client.calls if c[0] in ("protection", "cancel_protection")]
    kinds = [c[0] for c in seq]
    # 先挂桥接(保底) -> 撤旧 closePosition -> 挂正式保本 closePosition -> 撤桥接
    assert kinds == ["protection", "cancel_protection", "protection", "cancel_protection"]
    assert seq[1][1]["client_algo_id"] == "pa-sl-old0001"
    assert seq[3][1]["client_algo_id"] == seq[0][1]["client_algo_id"], "撤桥接"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True
    assert record["stop_algo_id"] == seq[2][1]["client_algo_id"], "记录指向正式保本单"
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


class ConflictStopClient(MarkSeqClient):
    """Emulate the Binance closePosition single-order rule per order class.

    A second closePosition STOP/TAKE_PROFIT of the same class cannot rest next
    to an open one (-4130: "An open stop or take profit order with GTE and
    closePosition in the direction is existing."); reduceOnly+quantity orders
    never conflict (same shape as the TP1 partial next to the entry STOP).
    """

    def __init__(self, marks: list[float], *, cp_stop_id: str | None = None) -> None:
        super().__init__(marks)
        self._cp_resting: dict[str, str] = {}  # order_type -> client_algo_id
        if cp_stop_id:
            self._cp_resting["STOP_MARKET"] = cp_stop_id

    def place_close_algo_order(self, **kwargs: object) -> None:
        order_type = str(kwargs.get("order_type") or "")
        if "quantity" not in kwargs and order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            resting = self._cp_resting.get(order_type)
            if resting:
                raise BinanceAPIError(
                    'Binance HTTP 400: {"code":-4130,"msg":"An open stop or take '
                    'profit order with GTE and closePosition in the direction is '
                    'existing."}',
                )
            super().place_close_algo_order(**kwargs)
            self._cp_resting[order_type] = str(kwargs["client_algo_id"])
            return
        super().place_close_algo_order(**kwargs)  # reduceOnly+qty bridge: no conflict

    def cancel_algo_order(self, **kwargs: object) -> None:
        super().cancel_algo_order(**kwargs)
        client_algo_id = str(kwargs["client_algo_id"])
        for order_type, resting_id in list(self._cp_resting.items()):
            if resting_id == client_algo_id:
                del self._cp_resting[order_type]
                return


class StuckOldStopClient(ConflictStopClient):
    """Old closePosition STOP cannot be cancelled (network) and still rests:
    a second closePosition STOP is then rejected with -4130."""

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))
        if str(kwargs["client_algo_id"]) == "pa-sl-old0001":
            raise BinanceAPIError("Binance network error: test outage")
        for order_type, resting_id in list(self._cp_resting.items()):
            if resting_id == str(kwargs["client_algo_id"]):
                del self._cp_resting[order_type]
                return


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


def test_guard_keeps_bridge_when_old_stop_cancel_fails(monkeypatch) -> None:
    """撤旧失败(非 -2011, 如网络)且交易所仍拒绝第二张 closePosition 单(-4130):
    不得硬挂也不得裸奔——桥接单(已在场)即为保本保护, 标记 moved。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    client = StuckOldStopClient([111], cp_stop_id="pa-sl-old0001")  # mark >= 1R
    _register_test_guard()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0
    )
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert len(places) == 1, "只有桥接单落地, 正式单被 -4130 拒(未记入)"
    assert places[0]["stop_price"] == Decimal("100")
    assert places[0].get("quantity") == Decimal("100"), "桥接单形态"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True, "桥接单即保本保护, 不得反复重试"
    assert record["stop_algo_id"] == places[0]["client_algo_id"]


def test_guard_keeps_bridge_when_canonical_place_rejected(monkeypatch) -> None:
    """撤旧成功但挂正式保本被拒(如 -1111): 桥接单早已在场, 保本不落空也不裸奔,
    无需再退 stop0 重挂(旧单已撤, 桥接单在 entry 等价完成移损)。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class CanonicalRejectedClient(ConflictStopClient):
        def place_close_algo_order(self, **kwargs: object) -> None:
            if (kwargs.get("order_type") == "STOP_MARKET"
                    and "quantity" not in kwargs
                    and kwargs.get("stop_price") == Decimal("100")):
                raise BinanceAPIError(
                    'Binance HTTP 400: {"code":-1111,"msg":"Precision is over the maximum defined for this asset."}',
                )
            super().place_close_algo_order(**kwargs)

    _register_test_guard()  # stop0=90, target=120, moved=False
    client = CanonicalRejectedClient([111], cp_stop_id="pa-sl-old0001")  # 1R 之上
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0,
        floor_pct=0.45,
    )
    cancels = [c[1] for c in client.calls if c[0] == "cancel_protection"]
    assert len(cancels) == 1, "旧止损只撤一次(已成功)"
    assert cancels[0]["client_algo_id"] == "pa-sl-old0001"
    stops = [c[1] for c in client.calls
             if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    assert len(stops) == 1, "仅桥接单落地(正式单被拒)"
    assert stops[0]["stop_price"] == Decimal("100"), "桥接单仍在 entry"
    assert stops[0].get("quantity") == Decimal("100")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True
    assert record["stop_algo_id"] == stops[0]["client_algo_id"]
    assert record["stop_algo_id"] != "pa-sl-old0001"


def test_guard_breakeven_move_bridges_closeposition_conflict(monkeypatch) -> None:
    """Regression(XRPUSDT 2026-09-09): 旧 closePosition STOP 仍 resting 时直接挂新
    保本 closePosition 单被 -4130 拒绝, guard 死循环刷错; 桥接换单必须完成移损。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    _register_test_guard()
    client = ConflictStopClient([111], cp_stop_id="pa-sl-old0001")
    # 模拟交易所约束: 旧单在场时第二张 closePosition STOP 必拒(-4130)。
    with pytest.raises(BinanceAPIError) as ei:
        client.place_close_algo_order(
            symbol="BTCUSDT", side="SELL", order_type="STOP_MARKET",
            stop_price=Decimal("101"), client_algo_id="pa-probe-cp2",
        )
    assert "-4130" in str(ei.value)
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0
    )
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert len(places) == 2, "桥接 + 正式保本均落地"
    assert all(p["stop_price"] == Decimal("100") for p in places)
    cancels = [c[1] for c in client.calls if c[0] == "cancel_protection"]
    assert cancels[0]["client_algo_id"] == "pa-sl-old0001"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True
    assert record["stop_algo_id"] != "pa-sl-old0001"


def test_guard_short_bridge_uses_abs_quantity(monkeypatch) -> None:
    """空单(SELL)保本移动: 桥接 reduceOnly 单数量必须取 abs(amount), 否则负数量
    被交易所拒(-1111), 空单永远移不了保本。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class ShortSeqClient(ConflictStopClient):
        def __init__(self, marks: list[float], *, cp_stop_id: str | None = None) -> None:
            super().__init__(marks, cp_stop_id=cp_stop_id)
            self.pos = {"amount": Decimal("-100"), "entry": Decimal("100")}

    binance_usdm_testnet._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-old0001", "stop0": "110", "target": "80",
         "side": "SELL", "conf": 60, "ts": time.time(), "moved": False},
    )
    client = ShortSeqClient([88], cp_stop_id="pa-sl-old0001")  # 88: 距 entry 12 >= 1R(10)
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0,
        floor_pct=0.45,
    )
    places = [c[1] for c in client.calls if c[0] == "protection"]
    assert len(places) == 2, "桥接 + 正式保本均落地"
    assert places[0].get("quantity") == Decimal("100"), "桥接数量必须为正(abs)"
    assert places[0]["side"] == "BUY", "空单离场侧为 BUY"
    assert all(p["stop_price"] == Decimal("100") for p in places)
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True
    assert record["stop_algo_id"] != "pa-sl-old0001"


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


def test_counter_trend_scales_risk_size_in_risk_sizing_mode() -> None:
    """风险定仓模式: 逆势减仓必须落在 risk_usdt 上, 数量真的减半.

    旧实现只缩放 leverage, 而数量由 risk/|anchor-stop| 决定, leverage 仅进
    名义上限 cap; 在 min_stop_distance_pct 下限之下 cap 永不触发, 等于没减仓。
    """
    settings = _trend_settings()
    settings.binance_usdm_testnet.max_notional_usdt = 1000
    settings.binance_usdm_testnet.risk_per_trade_usdt = 2.0
    settings.binance_usdm_testnet.counter_trend_size_scale = 0.5
    decision = _long_decision() | {"stop_loss_price": 99}

    # 顺势 (7 天 +20%, 做多): 全量风险 2U, mark 100 / stop 99 -> qty 2
    client = FakeClient()
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=20.0,
    )
    assert result.status == "submitted", result.reason
    with_trend = next(call[1] for call in client.calls if call[0] == "entry")
    assert Decimal(str(with_trend["quantity"])) == Decimal("2")

    # 逆势 (7 天 -20%, 做多, conf 65 过闸门): 风险金减半 -> qty 1
    client = FakeClient()
    result = execute_market_signal(
        decision | {"trade_confidence": 65, "take_profit_price": 130}, settings,
        analysis_symbol="BTCUSDT", client=client, trend_30d_pct=-20.0,
    )
    assert result.status == "submitted", result.reason
    assert "and size to 0.50x" in result.reason
    against = next(call[1] for call in client.calls if call[0] == "entry")
    assert Decimal(str(against["quantity"])) == Decimal("1")


def test_counter_trend_block_rejects_high_confidence_counter_order() -> None:
    """counter_trend_block 开启: 逆势单直接拒绝, 与置信度无关.

    故意把 min_confidence 和 size_scale 都关掉, 验证拦截不依赖那两个旧开关
    (guard_active 必须单独识别 block).
    """
    settings = _trend_settings()
    settings.binance_usdm_testnet.counter_trend_block = True
    settings.binance_usdm_testnet.counter_trend_min_confidence = 0
    settings.binance_usdm_testnet.counter_trend_size_scale = 1.0
    client = FakeClient()
    decision = _long_decision() | {"trade_confidence": 99}
    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=-20.0,
    )
    assert result.status == "rejected", result.reason
    assert "counter_trend_block" in result.reason
    assert "entry" not in [call[0] for call in client.calls]


def test_counter_trend_block_does_not_touch_with_trend_orders() -> None:
    """禁逆势不能误伤顺势单, 中性带内的单也不受影响."""
    settings = _trend_settings()
    settings.binance_usdm_testnet.counter_trend_block = True

    client = FakeClient()
    result = execute_market_signal(
        _long_decision(), settings, analysis_symbol="BTCUSDT", client=client,
        trend_30d_pct=20.0,
    )
    assert result.status == "submitted", result.reason

    client = FakeClient()
    result = execute_market_signal(
        _long_decision() | {"take_profit_price": 130}, settings,
        analysis_symbol="BTCUSDT", client=client, trend_30d_pct=1.0,
    )
    assert result.status == "submitted", result.reason

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
        if "exchangeInfo" in str(request.full_url):
            return _OkResponse({"symbols": []})  # no PRICE_FILTER -> legacy price
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
    algo_urls = [u for u in seen if "algoOrder" in u]
    assert algo_urls, seen
    assert "quantity=1.5" in algo_urls[0], algo_urls[0]
    assert "reduceOnly=true" in algo_urls[0], algo_urls[0]
    assert "closePosition" not in algo_urls[0], algo_urls[0]
    seen.clear()
    client.place_close_algo_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        stop_price=Decimal("90"),
        client_algo_id="pa-sl-x0001",
    )
    algo_urls = [u for u in seen if "algoOrder" in u]
    assert algo_urls, seen
    assert "closePosition=true" in algo_urls[0], algo_urls[0]
    assert "quantity" not in algo_urls[0], algo_urls[0]


def test_place_algo_order_rounds_trigger_price_to_tick() -> None:
    """-1111 回归: algo 触发价按 PRICE_FILTER tick 向下取整, exchangeInfo 走缓存。"""
    seen: list[str] = []

    def opener(request, **_kw: object) -> _OkResponse:
        seen.append(str(request.full_url))
        if "exchangeInfo" in str(request.full_url):
            return _OkResponse({
                "symbols": [{
                    "symbol": "ETHUSDT",
                    "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
                }]
            })
        return _OkResponse({"code": 200, "msg": "success"})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=opener
    )
    for raw in ("2480.6789", "90.555"):  # 两次同 symbol: 第二次命中缓存
        client.place_close_algo_order(
            symbol="ETHUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            stop_price=Decimal(raw),
            client_algo_id="pa-sl-round" + raw[:2],
        )
    algo_urls = [u for u in seen if "algoOrder" in u]
    assert len(algo_urls) == 2
    assert "triggerPrice=2480.67" in algo_urls[0], algo_urls[0]  # round down
    assert "triggerPrice=90.55" in algo_urls[1], algo_urls[1]
    assert len([u for u in seen if "exchangeInfo" in u]) == 1, "exchangeInfo 应只拉一次"



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
    """TP1 半仓触发后: 先挂桥接保底再撤旧 SL, 挂正式保本 closePosition STOP + TP2,
    全程不触发 -4130 且无裸奔窗口, 注册表标记完成。"""
    _register_tp_record()
    client = PartialRunnerClient([2, 2, 1])
    sleeps = _run_tp_runner(client, monkeypatch)
    assert len(sleeps) == 2  # 前两次量未变, 第三轮才触发半仓
    cancels = _tp_cancels(client)
    assert len(cancels) == 3, cancels
    assert cancels[0]["client_algo_id"] == "pa-tp-part0001"  # 先清 TP1 残单
    assert cancels[1]["client_algo_id"] == "pa-sl-old0001"  # 再撤旧 closePosition SL
    bridge_cancel = cancels[2]["client_algo_id"]
    places = _tp_places(client)
    assert len(places) == 3, places
    bridge, canonical, tp2 = places
    assert bridge["order_type"] == "STOP_MARKET"
    assert bridge["stop_price"] == Decimal("100")
    assert bridge.get("quantity") == Decimal("1"), "桥接单按剩余仓位数"
    assert bridge.get("close_position") is False
    assert canonical["order_type"] == "STOP_MARKET"
    assert canonical["stop_price"] == Decimal("100")
    assert "quantity" not in canonical, "正式保本单为 closePosition 形态"
    assert tp2["order_type"] == "TAKE_PROFIT_MARKET"
    assert tp2["stop_price"] == Decimal("150")
    kinds = [c[0] for c in client.calls if c[0] in ("protection", "cancel_protection")]
    # 清 TP1 残单 -> 桥接(保底) -> 撤旧 -> 正式保本 -> 撤桥接 -> TP2
    assert kinds == [
        "cancel_protection", "protection", "cancel_protection",
        "protection", "cancel_protection", "protection",
    ], kinds
    seq = [c for c in client.calls if c[0] in ("protection", "cancel_protection")]
    assert seq[1][1]["client_algo_id"] == bridge["client_algo_id"], "先挂桥接保底"
    assert seq[2][1]["client_algo_id"] == "pa-sl-old0001"
    assert seq[3][1]["client_algo_id"] == canonical["client_algo_id"], "正式单在撤旧后"
    assert seq[4][1]["client_algo_id"] == bridge_cancel == bridge["client_algo_id"]
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["moved"] is True
    assert record["partial_done"] is True
    assert record["stop_algo_id"] == canonical["client_algo_id"]
    assert record["tp_algo_id"] == tp2["client_algo_id"]


def test_tp_runner_waits_while_short_position_still_full(monkeypatch) -> None:
    """空单仓位为负数: 量未变时必须等待, 不得误判 TP1 已半平。

    回归 2026-09-10 BNBUSDT 秒开秒平: runner 用带符号 amount 与正数 qty 比,
    空单永远不等, 开仓首轮就撤 TP1 + 把止损移到 entry(≈市价), 随即被打掉。
    """
    _register_tp_record(extra={"side": "SELL"})
    client = PartialRunnerClient([-2, -2, -1])
    sleeps = _run_tp_runner(client, monkeypatch)
    assert len(sleeps) == 2, "前两次仓位未减必须等待, 而不是立刻撤 TP1/移损"
    cancels = _tp_cancels(client)
    assert cancels[0]["client_algo_id"] == "pa-tp-part0001", "半仓确认后才清 TP1"
    assert _tp_places(client), "半仓确认后才挂保本止损/TP2"


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
    """撤原 SL 遇 -2011(已被撤): 视为已撤, 桥接+正式保本+TP2 照常落地。"""
    _register_tp_record()
    client = StaleStopRunnerClient([2, 1])
    _run_tp_runner(client, monkeypatch)
    places = _tp_places(client)
    assert len(places) == 3, places  # 桥接 + 正式保本 + TP2
    assert places[0]["order_type"] == "STOP_MARKET"
    assert places[0]["stop_price"] == Decimal("100")
    assert places[0].get("quantity") == Decimal("1")
    assert places[1]["order_type"] == "STOP_MARKET"
    assert places[1]["stop_price"] == Decimal("100")
    assert "quantity" not in places[1]
    assert places[2]["stop_price"] == Decimal("150")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["partial_done"] is True and record["moved"] is True
    assert record["stop_algo_id"] == places[1]["client_algo_id"]


def test_tp_runner_bridges_when_old_stop_cancel_fails(monkeypatch) -> None:
    """撤旧 SL 持续网络失败: 桥接单保底在场, 正式保本(旧单其实已撤或 -4130 判定后)
    照常推进, 不无限空转也不裸奔。"""
    sleeps: list[float] = []

    class OldCancelFailRunnerClient(PartialRunnerClient):
        """Only the OLD closePosition stop cancel keeps failing (non-missing)."""

        def cancel_algo_order(self, **kwargs: object) -> None:
            self.calls.append(("cancel_protection", kwargs))
            if str(kwargs["client_algo_id"]).startswith("pa-sl-old"):
                raise BinanceAPIError("Binance network error: test outage")
            if str(kwargs["client_algo_id"]).startswith("pa-tp-part"):
                raise BinanceAPIError(
                    'Binance HTTP 400: {"code":-2011,"msg":"Unknown order sent."}'
                )

    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    _register_tp_record()
    client = OldCancelFailRunnerClient([1])
    sleeps_used = _run_tp_runner(client, monkeypatch)
    assert sleeps_used == [], "一次轮询内完成, 无需退避重试"
    places = _tp_places(client)
    assert len(places) == 3, places  # 桥接 + 正式保本 + TP2
    assert places[1]["order_type"] == "STOP_MARKET"
    assert places[1]["stop_price"] == Decimal("100")
    assert places[2]["order_type"] == "TAKE_PROFIT_MARKET"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["partial_done"] is True and record["moved"] is True
    assert record["stop_algo_id"] == places[1]["client_algo_id"]


class ConflictStopRunnerClient(PartialRunnerClient):
    """Runner account emulating the Binance closePosition single-order rule.

    A second closePosition STOP/TAKE_PROFIT of the same class is rejected with
    -4130 while one of that class rests; reduceOnly+quantity never conflicts.
    """

    def __init__(self, amounts: list[float]) -> None:
        super().__init__(amounts)
        self._cp_resting: dict[str, str] = {"STOP_MARKET": "pa-sl-old0001"}

    def place_close_algo_order(self, **kwargs: object) -> None:
        order_type = str(kwargs.get("order_type") or "")
        if "quantity" not in kwargs and order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            resting = self._cp_resting.get(order_type)
            if resting:
                raise BinanceAPIError(
                    'Binance HTTP 400: {"code":-4130,"msg":"An open stop or take '
                    'profit order with GTE and closePosition in the direction is '
                    'existing."}',
                )
            self.calls.append(("protection", kwargs))
            self._cp_resting[order_type] = str(kwargs["client_algo_id"])
            return
        self.calls.append(("protection", kwargs))

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))
        client_algo_id = str(kwargs["client_algo_id"])
        for order_type, resting_id in list(self._cp_resting.items()):
            if resting_id == client_algo_id:
                del self._cp_resting[order_type]
                return


def test_tp_runner_swap_bridges_closeposition_conflict(monkeypatch) -> None:
    """Regression(XRPUSDT 2026-09-09 14:21-14:25): 旧 closePosition SL 仍 resting 时
    直接挂新保本 closePosition STOP 必被 -4130 拒绝; 桥接换单须完成移损 + TP2。"""
    _register_tp_record()
    client = ConflictStopRunnerClient([2, 1])
    # 模拟交易所: 旧 closePosition STOP 在场时第二张 closePosition STOP 必拒。
    with pytest.raises(BinanceAPIError) as ei:
        client.place_close_algo_order(
            symbol="BTCUSDT", side="SELL", order_type="STOP_MARKET",
            stop_price=Decimal("101"), client_algo_id="pa-probe-cp2",
        )
    assert "-4130" in str(ei.value)
    sleeps = _run_tp_runner(client, monkeypatch)
    assert len(sleeps) == 1  # 第一轮量未变, 第二轮才触发半仓
    places = _tp_places(client)
    assert len(places) == 3, places  # 桥接 + 正式保本 + TP2, -4130 不再出现
    assert places[0]["order_type"] == "STOP_MARKET"
    assert places[0].get("quantity") == Decimal("1"), "桥接 reduceOnly 形态"
    assert places[1]["order_type"] == "STOP_MARKET"
    assert "quantity" not in places[1]
    assert places[1]["stop_price"] == Decimal("100")
    assert places[2]["order_type"] == "TAKE_PROFIT_MARKET"
    assert places[2]["stop_price"] == Decimal("150")
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["moved"] is True and record["partial_done"] is True
    assert record["stop_algo_id"] == places[1]["client_algo_id"]


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
    assert binance_usdm_testnet._stop_watchdog_loop in targets, "部分止盈须附带看护线程"
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
    assert targets == [
        binance_usdm_testnet._tp_runner_loop,
        binance_usdm_testnet._stop_watchdog_loop,
    ]
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


def test_conf_feedback_setting_defaults_and_bounds() -> None:
    """P2-3: 反馈闸门默认 off, 样本下限 5..200, 越界拒绝."""
    from pydantic import ValidationError

    assert BinanceUSDMTestnetSettings().conf_feedback_mode == "off"
    assert BinanceUSDMTestnetSettings().conf_feedback_min_samples == 30
    s = BinanceUSDMTestnetSettings(conf_feedback_mode="on", conf_feedback_min_samples=10)
    assert s.conf_feedback_mode == "on"
    assert s.conf_feedback_min_samples == 10
    for bad in (3, 201):
        with pytest.raises(ValidationError):
            BinanceUSDMTestnetSettings(conf_feedback_min_samples=bad)
    with pytest.raises(ValidationError):
        BinanceUSDMTestnetSettings(conf_feedback_mode="auto")


def test_measured_win_rate_override_helper(tmp_path, monkeypatch) -> None:
    """P2-3: 仅 on+同桶样本达标才返回实测胜率, 其余 fail-open 返回 None."""
    import json as _json

    buckets = tmp_path / "conf_buckets.json"
    buckets.write_text(_json.dumps({"50": {"n": 30, "wins": 9}}), encoding="utf-8")
    monkeypatch.setattr(binance_usdm_testnet, "_CONF_BUCKETS_PATH", str(buckets))
    cfg = BinanceUSDMTestnetSettings(conf_feedback_mode="on")
    m = binance_usdm_testnet._measured_win_rate_override
    assert m(52.0, cfg) == 0.3
    assert m(49.0, cfg) is None  # 桶 45-49 无样本
    assert m(None, cfg) is None
    cfg2 = BinanceUSDMTestnetSettings(conf_feedback_mode="off")
    assert m(52.0, cfg2) is None
    cfg3 = BinanceUSDMTestnetSettings(conf_feedback_mode="on", conf_feedback_min_samples=50)
    assert m(52.0, cfg3) is None  # 样本 30 < 50
    (tmp_path / "gone.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(binance_usdm_testnet, "_CONF_BUCKETS_PATH", str(tmp_path / "gone.json"))
    assert m(52.0, cfg) is None


def test_executor_trader_equation_uses_measured_win_rate_when_on(
    tmp_path, monkeypatch
) -> None:
    """P2-3: on+达标时方程用桶实测胜率(0.3 拒单), off 时用模型 0.7 放行."""
    import json as _json

    buckets = tmp_path / "conf_buckets.json"
    buckets.write_text(_json.dumps({"50": {"n": 30, "wins": 9}}), encoding="utf-8")
    monkeypatch.setattr(binance_usdm_testnet, "_CONF_BUCKETS_PATH", str(buckets))

    def decision() -> dict:
        return _long_decision() | {
            "entry_price": 100,
            "stop_loss_price": 99,
            "take_profit_price": 100.5,
            "trade_confidence": 52,
        }

    on = _settings()
    on.binance_usdm_testnet.conf_feedback_mode = "on"
    on.binance_usdm_testnet.conf_feedback_min_samples = 30
    client = FakeClient()
    result = execute_market_signal(decision(), on, analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected", result.reason
    assert "Trader" in result.reason
    off = _settings()
    client2 = FakeClient()
    result2 = execute_market_signal(decision(), off, analysis_symbol="BTCUSDT", client=client2)
    assert result2.status == "submitted", result2.reason


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
    assert sl_cancels[0]["client_algo_id"] == "pa-sl-old0001"
    assert len(sl_cancels) == 2, "撤旧后撤桥接"
    assert sl_cancels[1]["client_algo_id"].startswith("pa-sl-")
    places = _tp_places(client)
    stops = [p for p in places if p["order_type"] == "STOP_MARKET"]
    assert len(stops) == 2, "桥接 + 正式保本"
    assert all(p["stop_price"] == Decimal("100") for p in stops)
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["moved"] is True          # 保本已就位
    assert record["partial_done"] is False  # TP2 未完成, 可被再次拉起
    assert record["stop_algo_id"] != "pa-sl-old0001"
    assert record["stop_algo_id"].startswith("pa-sl-")
    assert record["stop_algo_id"] == stops[-1]["client_algo_id"], "记录指向正式保本单"
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


# ---------------------------------------------------------------------------
# user-data stream -> fill watcher wake-up (B3)
# ---------------------------------------------------------------------------


def test_notify_user_order_update_wakes_registered_watcher() -> None:
    """订单事件按 clientOrderId 唤醒对应 watcher, 不误伤其它 watcher. """
    wake_a = binance_usdm_testnet._register_watcher_wake("pa-entry-AAA")
    wake_b = binance_usdm_testnet._register_watcher_wake("pa-entry-BBB")
    try:
        assert wake_a.is_set() is False
        binance_usdm_testnet.notify_user_order_update(
            {"e": "ORDER_TRADE_UPDATE", "o": {"c": "pa-entry-AAA", "s": "BTCUSDT"}}
        )
        assert wake_a.is_set() is True
        assert wake_b.is_set() is False, "其它 watcher 不应被唤醒"
        binance_usdm_testnet.notify_user_order_update({"e": "ACCOUNT_UPDATE", "o": {}})
        binance_usdm_testnet.notify_user_order_update({"e": "ORDER_TRADE_UPDATE", "o": {"c": "unknown"}})
    finally:
        binance_usdm_testnet._unregister_watcher_wake("pa-entry-AAA")
        binance_usdm_testnet._unregister_watcher_wake("pa-entry-BBB")


def test_watcher_wake_registry_cleans_up_on_exit(monkeypatch) -> None:
    """watcher 结束后注册表无残留(任意退出路径)."""
    class OneStatusClient(FakeClient):
        def order_status(self, *, symbol: str, client_id: str) -> str:
            return "FILLED"

    client = OneStatusClient()
    binance_usdm_testnet._persist_pending(
        "BTCUSDT",
        {"client_id": "pa-entry-clean", "signal_id": "clean-signal"},
    )
    monkeypatch.setattr(binance_usdm_testnet.time, "monotonic", lambda: 0.0)
    binance_usdm_testnet._watch_limit_entry(
        client=client,
        symbol="BTCUSDT",
        client_id="pa-entry-clean",
        side="BUY",
        stop=Decimal("90"),
        target=Decimal("120"),
        quantity=Decimal("0.2"),
        signal_id="clean-signal",
        timeout_seconds=1.0,
        poll_interval=0.1,
    )
    with binance_usdm_testnet._watcher_wake_lock:
        assert "pa-entry-clean" not in binance_usdm_testnet._watcher_wake_events


# ---------------------------------------------------------------------------
# -2021 立即触发预检 (breakeven at entry after mark crossed back)
# ---------------------------------------------------------------------------


def test_stop_would_immediately_trigger_strict_sides() -> None:
    f = binance_usdm_testnet._stop_would_immediately_trigger
    # 多单离场(SELL): mark 严格低于 trigger 才立即触发
    assert f("SELL", Decimal("100"), Decimal("99.9")) is True
    assert f("SELL", Decimal("100"), Decimal("100")) is False  # 相等可挂
    assert f("SELL", Decimal("100"), Decimal("100.1")) is False
    # 空单离场(BUY): mark 严格高于 trigger 才立即触发
    assert f("BUY", Decimal("100"), Decimal("100.1")) is True
    assert f("BUY", Decimal("100"), Decimal("100")) is False
    assert f("BUY", Decimal("100"), Decimal("99.9")) is False


def test_tp_runner_skips_breakeven_when_mark_crossed_back(monkeypatch) -> None:
    """mark 已回吐穿 entry(TP1 成交后急跌): 保本单会 -2021, runner 保留原 SL
    并标记 moved, TP2 阶段照常推进, 不再反复尝试挂保本。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class CrossedRunnerClient(PartialRunnerClient):
        def mark_price(self, symbol: str) -> Decimal:
            self.calls.append(("mark_price", symbol))
            return Decimal("99")  # 低于 entry 100 -> 预检拦截

    _register_tp_record()
    client = CrossedRunnerClient([2, 1])  # 第二轮 amount 半仓进入 TP2 阶段
    binance_usdm_testnet._tp_runner_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0
    )
    breakeven_places = [
        c[1] for c in client.calls
        if c[0] == "protection" and c[1].get("stop_price") == Decimal("100")
    ]
    assert breakeven_places == [], "must not place an immediately-triggering breakeven stop"
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["moved"] is True, "runner must mark moved to stop retrying"
    assert record["stop_algo_id"] == "pa-sl-old0001", "original stop stays in place"






# ---------------------------------------------------------------------------
# 补挂校验: 记录止损单必须真实 resting, 缺失即补挂(防裸奔) / resume 看护
# ---------------------------------------------------------------------------
# 事故背景(2026-09-08): TP1 部分止盈后, 保本 guard 撤掉原 STOP 又因触发价
# 精度问题(-1111)挂不上新单; 记录仍指向已撤销的旧单, 重启 resume 只信记录
# 不查实单 → 持仓裸奔数小时. 本组测试: 校验(verify)+补挂(rehang)+看护(watchdog).


def test_client_algo_order_status_queries_by_id_and_live_predicate() -> None:
    """algoOrder 状态查询走 GET clientAlgoId; 只有 NEW 视为 resting。"""
    seen: list[str] = []

    def opener(request, **_kw: object) -> _OkResponse:
        seen.append(str(request.full_url))
        return _OkResponse({"algoStatus": "NEW", "clientAlgoId": "pa-sl-x0001"})

    client = binance_usdm_testnet.BinanceUSDMTestnetClient(
        "test-key", "test-secret", opener=opener
    )
    rec = client.algo_order_status(client_algo_id="pa-sl-x0001")
    assert rec["algoStatus"] == "NEW"
    assert len(seen) == 1
    assert "algoOrder" in seen[0] and "clientAlgoId=pa-sl-x0001" in seen[0]
    live = binance_usdm_testnet._algo_status_live
    assert live("NEW") is True
    for dead in ("CANCELED", "EXPIRED", "", "TRIGGERED", "FAILED"):
        assert live(dead) is False, dead


def test_rehang_stop_price_ladder_stage_and_floor() -> None:
    """补挂候选价: TP2/触发阶段 entry 优先, 静态阶段 stop0 优先;
    立即触发(-2021)候选跳过; 最后兜底 mark 外 min-distance floor。"""
    cands = binance_usdm_testnet._rehang_stop_candidates
    # 多单 BUY: entry 100 / stop0 90
    assert cands(side="BUY", entry=Decimal("100"), stop0=Decimal("90"),
                 mark=Decimal("105"), stage_tp2=False,
                 floor_pct=0.45) == [Decimal("90"), Decimal("100"), Decimal("104.5275")]
    assert cands(side="BUY", entry=Decimal("100"), stop0=Decimal("90"),
                 mark=Decimal("105"), stage_tp2=True,
                 floor_pct=0.45) == [Decimal("100"), Decimal("90"), Decimal("104.5275")]
    # mark 已回吐到 95(entry 与 stop0 之间): entry 立即触发被剔除
    assert cands(side="BUY", entry=Decimal("100"), stop0=Decimal("90"),
                 mark=Decimal("95"), stage_tp2=True,
                 floor_pct=0.45) == [Decimal("90"), Decimal("94.5725")]
    # mark 已跌破 stop0: entry/stop0 均立即触发 → floor = mark*(1-0.45%)
    assert cands(side="BUY", entry=Decimal("100"), stop0=Decimal("90"),
                 mark=Decimal("80"), stage_tp2=True,
                 floor_pct=0.45) == [Decimal("79.64")]
    # 无 floor 配置且无候选 → 空列表
    assert cands(side="BUY", entry=Decimal("100"), stop0=Decimal("90"),
                 mark=Decimal("80"), stage_tp2=True, floor_pct=0.0) == []
    # 空单 SELL: entry 100 / stop0 110; BUY 离场单 mark 升破 trigger 才立即触发.
    # mark 105 已升破 entry 100 → entry 候选剔除(会 -2021); floor=mark*(1+0.45%)
    assert cands(side="SELL", entry=Decimal("100"), stop0=Decimal("110"),
                 mark=Decimal("105"), stage_tp2=False,
                 floor_pct=0.45) == [Decimal("110"), Decimal("105.4725")]
    assert cands(side="SELL", entry=Decimal("100"), stop0=Decimal("110"),
                 mark=Decimal("105"), stage_tp2=True,
                 floor_pct=0.45) == [Decimal("110"), Decimal("105.4725")]
    # 空单 mark 已升破 stop0(130): floor 在 mark 上方 mark*(1+0.45%)
    assert cands(side="SELL", entry=Decimal("100"), stop0=Decimal("110"),
                 mark=Decimal("130"), stage_tp2=True,
                 floor_pct=0.45) == [Decimal("130.585")]
    assert cands(side="SELL", entry=Decimal("100"), stop0=Decimal("110"),
                 mark=Decimal("130"), stage_tp2=True, floor_pct=0.0) == []


class _DeadStopClient(FakeClient):
    """algo 状态查询 mixin: 记录内 id 已 CANCELED, 其它一律 NEW。

    不定义 __init__(MRO 安全): 组合类须在自身 __init__ 里设置 self._dead。
    """

    def algo_order_status(self, *, client_algo_id: str) -> dict:
        self.calls.append(("algo_order_status", client_algo_id))
        status = "CANCELED" if client_algo_id in self._dead else "NEW"
        return {"clientAlgoId": client_algo_id, "algoStatus": status}


def test_tp_runner_rehangs_when_recorded_stop_dead(monkeypatch) -> None:
    """TP1 半仓已成交但记录止损已死(CANCELED): runner 进 TP2 前必须补挂,
    不许"保留原止损"裸奔; mark 在 entry 下方时退回 stop0。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class DeadStopRunnerClient(PartialRunnerClient, _DeadStopClient):
        def __init__(self) -> None:
            super().__init__([2, 1])
            self._dead = {"pa-sl-old0001"}

        def mark_price(self, symbol: str) -> Decimal:
            self.calls.append(("mark_price", symbol))
            return Decimal("99")  # entry 100 之下 -> entry 保本不可挂

    _register_tp_record()
    client = DeadStopRunnerClient()
    binance_usdm_testnet._tp_runner_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0, floor_pct=0.45
    )
    stops = [c[1] for c in client.calls
             if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    assert [s["stop_price"] for s in stops] == [Decimal("90")], stops
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["stop_algo_id"] != "pa-sl-old0001"
    assert record["stop_algo_id"].startswith("pa-sl-")
    assert record["moved"] is True
    assert record["partial_done"] is True
    tp2 = [c[1] for c in client.calls
           if c[0] == "protection" and c[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    assert [t["stop_price"] for t in tp2] == [Decimal("150")]
    sl_cancels = [c[1] for c in client.calls
                  if c[0] == "cancel_protection"
                  and c[1]["client_algo_id"].startswith("pa-sl-")]
    assert not sl_cancels, "已死止损无需再撤(补挂路径不得触发撤旧)"


def test_tp_runner_floor_rehang_when_mark_below_static_stop(monkeypatch) -> None:
    """mark 已跌破原静态止损: entry/stop0 都立即触发, 补挂在 mark 下方
    min-distance floor(0.45%), 仓位绝不裸奔。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class BelowStopClient(PartialRunnerClient, _DeadStopClient):
        def __init__(self) -> None:
            super().__init__([2, 1])
            self._dead = {"pa-sl-old0001"}

        def mark_price(self, symbol: str) -> Decimal:
            self.calls.append(("mark_price", symbol))
            return Decimal("80")  # 低于 stop0 90 -> 只能挂 floor

    _register_tp_record()
    client = BelowStopClient()
    binance_usdm_testnet._tp_runner_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0, floor_pct=0.45
    )
    stops = [c[1] for c in client.calls
             if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    assert [s["stop_price"] for s in stops] == [Decimal("79.64")], stops
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record and record["partial_done"] is True and record["moved"] is True


def test_tp_runner_retries_when_no_rehang_candidate(monkeypatch) -> None:
    """无 floor 且 mark 已破止损位: 无候选可挂 → runner 限次重试后保留记录
    (供重启/resume 再补), 不谎报 partial_done。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class NoFloorClient(PartialRunnerClient, _DeadStopClient):
        def __init__(self) -> None:
            super().__init__([2, 1])
            self._dead = {"pa-sl-old0001"}

        def mark_price(self, symbol: str) -> Decimal:
            self.calls.append(("mark_price", symbol))
            return Decimal("80")

    _register_tp_record()
    client = NoFloorClient()
    binance_usdm_testnet._tp_runner_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0, floor_pct=0.0
    )
    assert len(sleeps) >= 4, "heal 失败应限次退避重试"
    assert not [c[1] for c in client.calls
                if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["partial_done"] is False, "无候选可挂不得谎报完成"
    assert record["moved"] is True, "TP1 已触发: 进入 TP2 语义, guard 不再挂 entry"
    assert record["stop_algo_id"] == "pa-sl-old0001", "无候选时记录句柄保留供补挂"


def test_guard_heals_dead_stop_after_breakeven_rejected(monkeypatch) -> None:
    """guard 达 1R 挂保本被拒(如 -1111)且原止损已死: 补挂候选价, 不许裸奔退出。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class RejectEntryClient(MarkSeqClient, _DeadStopClient):
        def __init__(self) -> None:
            super().__init__([111])  # 1R 之上
            self._dead = {"pa-sl-old0001"}

        def place_close_algo_order(self, **kwargs: object) -> None:
            super().place_close_algo_order(**kwargs)
            if (kwargs.get("order_type") == "STOP_MARKET"
                    and kwargs.get("stop_price") == Decimal("100")):
                raise BinanceAPIError(
                    'Binance HTTP 400: {"code":-1111,"msg":"Precision is over the maximum defined for this asset."}'
                )

    _register_test_guard()  # stop0=90, target=120, moved=False
    client = RejectEntryClient()
    binance_usdm_testnet._breakeven_guard_loop(
        client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0,
        floor_pct=0.45,
    )
    record = binance_usdm_testnet._read_guard("BTCUSDT")
    assert record is not None
    assert record["moved"] is True, "补挂成功后 guard 应结束"
    assert record["stop_algo_id"].startswith("pa-sl-")
    assert record["stop_algo_id"] != "pa-sl-old0001"
    stops = [c[1] for c in client.calls
             if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    placed = [s["stop_price"] for s in stops if s.get("stop_price") == Decimal("90")]
    assert placed, "补挂必须落到 stop0 90"
    assert not [c[1] for c in client.calls if c[0] == "cancel_protection"]


def test_watchdog_heals_dead_stop_and_drops_on_flat(monkeypatch) -> None:
    """TP2 阶段看护: 记录止损死 → 补挂; 仓位平 → 撤残留 TP 并移除记录。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)

    class WatchClient(PartialRunnerClient, _DeadStopClient):
        def __init__(self) -> None:
            super().__init__([1, 1, 0])
            self._dead = {"pa-sl-old0001"}

        def mark_price(self, symbol: str) -> Decimal:
            self.calls.append(("mark_price", symbol))
            return Decimal("99")

    _register_tp_record(extra={"moved": True, "partial_done": True})
    client = WatchClient()
    binance_usdm_testnet._stop_watchdog_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0, floor_pct=0.45
    )
    stops = [c[1] for c in client.calls
             if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    assert [s["stop_price"] for s in stops] == [Decimal("90")], stops
    cancels = [c[1] for c in client.calls if c[0] == "cancel_protection"]
    assert cancels and cancels[-1]["client_algo_id"] == "pa-tp-part0001"
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None
    assert len(sleeps) >= 1


def test_watchdog_waits_while_short_position_still_full(monkeypatch) -> None:
    """空单仓位完整(量未减)时看护线程不得进入止损补挂校验分支。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    _register_tp_record(extra={"side": "SELL"})
    client = PartialRunnerClient([-2, -2, 0])
    binance_usdm_testnet._stop_watchdog_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0, floor_pct=0.45
    )
    assert len(sleeps) == 2, "仓位完整的两轮必须等待"
    assert not [c for c in client.calls if c[0] == "algo_order_status"], (
        "未到复核间隔不得校验/补挂止损"
    )
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_watchdog_verifies_unmoved_record_after_idle_ticks(monkeypatch) -> None:
    """未移动记录(保本/TP1 未触发)此前没人核验止损: 看护须按
    _UNMOVED_STOP_VERIFY_TICKS 间隔复核, 止损已死则补挂, 仓位平后清记录。"""
    sleeps: list[float] = []
    monkeypatch.setattr(binance_usdm_testnet.time, "sleep", sleeps.append)
    ticks = binance_usdm_testnet._UNMOVED_STOP_VERIFY_TICKS

    class UnmovedClient(PartialRunnerClient, _DeadStopClient):
        def __init__(self) -> None:
            super().__init__([2] * ticks + [0])
            self._dead = {"pa-sl-old0001"}

        def mark_price(self, symbol: str) -> Decimal:
            self.calls.append(("mark_price", symbol))
            return Decimal("99")  # entry 100 之下: 补挂只能落 stop0 90

    _register_tp_record()  # moved=False / partial_done=False / qty=2
    client = UnmovedClient()
    binance_usdm_testnet._stop_watchdog_loop(
        client=client, symbol="BTCUSDT", poll_seconds=1.0, floor_pct=0.45
    )
    verified = [c for c in client.calls if c[0] == "algo_order_status"]
    assert len(verified) == 1, "空闲期内只复核一次"
    stops = [c[1] for c in client.calls
             if c[0] == "protection" and c[1]["order_type"] == "STOP_MARKET"]
    assert [s["stop_price"] for s in stops] == [Decimal("90")], stops
    assert len(sleeps) == ticks, "阈值前的每轮都要等待"
    assert binance_usdm_testnet._read_guard("BTCUSDT") is None


def test_resume_stop_watchdogs_arms_terminal_records(monkeypatch) -> None:
    """重启恢复: 拉起全部记录(含 partial_done / moved / 未移动), 让补挂校验在
    重启后立刻生效; 未移动记录此前无人核验止损, 是 09-07 裸奔事故的成因。"""
    captured = _capture_threads(monkeypatch)
    settings = _partial_settings()
    _register_tp_record("BTCUSDT")  # partial 未完成 -> TP runner resume 会管
    _register_tp_record("ETHUSDT", extra={"moved": True, "partial_done": True})
    _legacy_record(time.time(), "ADAUSDT")  # unmoved 旧 guard 记录
    binance_usdm_testnet._register_guard(
        "SOLUSDT",
        {"stop_algo_id": "pa-sl-moved", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 60, "ts": time.time(), "moved": True},
    )
    count = binance_usdm_testnet.resume_stop_watchdogs(settings, client=FakeClient())
    entries = sorted((target, kwargs["symbol"]) for target, kwargs in captured)
    assert count == 4, count
    assert [(binance_usdm_testnet._stop_watchdog_loop, s) for s in
            ("ADAUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")] == entries
    kwargs = next(k for _t, k in captured if k["symbol"] == "ETHUSDT")
    assert kwargs["floor_pct"] == settings.binance_usdm_testnet.min_stop_distance_pct
    assert kwargs["poll_seconds"] == settings.binance_usdm_testnet.breakeven_poll_seconds

# ── 撤单审计 (cancel audit trail) ───────────────────────────────────────


def _cancel_records(tmp_path) -> list[dict]:
    directory = tmp_path / "cancels"
    if not directory.exists():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("cancels-*.jsonl")):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


@pytest.fixture(autouse=True)
def _isolate_cancel_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cancel_log, "CANCEL_LOG_DIR", tmp_path / "cancels")


def test_timed_out_limit_entry_is_recorded_in_cancel_audit(tmp_path) -> None:
    """Timeout cancels must land in the audit trail with a stable reason."""
    client = FakeClient()
    client.limit_orders["pa-entry-timeout"] = {"status": "NEW"}
    _persist_pending_record("BTCUSDT", "pa-entry-timeout", "timeout-signal")

    binance_usdm_testnet._cancel_and_settle(
        client,
        "BTCUSDT",
        "pa-entry-timeout",
        "BUY",
        Decimal("90"),
        Decimal("120"),
        Decimal("167.79"),
        "timeout-signal",
        context="timed out",
    )

    records = _cancel_records(tmp_path)
    assert [record["reason"] for record in records] == ["limit_entry_timeout"]
    assert records[0]["symbol"] == "BTCUSDT"
    assert records[0]["client_id"] == "pa-entry-timeout"
    assert records[0]["entry_price"] == "100"
    assert records[0]["signal_id"] == "timeout-signal"
    assert records[0]["detail"]


def test_timed_out_after_status_failures_maps_to_timeout_reason(tmp_path) -> None:
    """The status-failure timeout is the same cancel reason as a plain timeout."""
    client = FakeClient()
    client.limit_orders["pa-entry-timeout"] = {"status": "NEW"}
    _persist_pending_record("BTCUSDT", "pa-entry-timeout", "timeout-signal")

    binance_usdm_testnet._cancel_and_settle(
        client,
        "BTCUSDT",
        "pa-entry-timeout",
        "BUY",
        Decimal("90"),
        Decimal("120"),
        Decimal("167.79"),
        "timeout-signal",
        context="timed out after status failures",
    )

    records = _cancel_records(tmp_path)
    assert [record["reason"] for record in records] == ["limit_entry_timeout"]


def test_replaced_limit_entry_is_recorded_in_cancel_audit(tmp_path) -> None:
    """Replacing a resting entry must record the old order as plan_replaced."""
    client = FakeClient()
    client.limit_orders["pa-entry-old"] = {"status": "NEW"}
    _persist_pending_record("BTCUSDT", "pa-entry-old", "old-signal")

    result = binance_usdm_testnet._replace_pending_limit(client, "BTCUSDT")

    assert result is None, "a stale resting entry must not block a fresh one"
    records = _cancel_records(tmp_path)
    assert [record["reason"] for record in records] == ["plan_replaced"]
    assert records[0]["client_id"] == "pa-entry-old"
    assert records[0]["entry_price"] == "100"


def test_failed_cancel_is_recorded_in_cancel_audit(tmp_path) -> None:
    """A rejected cancel must be visible: pending record stays, audit says why."""
    client = FakeClient()
    client.limit_orders["pa-entry-old"] = {"status": "NEW"}
    _persist_pending_record("BTCUSDT", "pa-entry-old", "old-signal")

    def refuse(**_kwargs: object) -> None:
        raise BinanceAPIError("Binance error -1003: too many requests")

    client.cancel_order = refuse  # type: ignore[method-assign]

    result = binance_usdm_testnet._replace_pending_limit(client, "BTCUSDT")

    assert result is not None and result.status == "failed"
    records = _cancel_records(tmp_path)
    assert [record["reason"] for record in records] == ["cancel_failed"]
    assert records[0]["client_id"] == "pa-entry-old"


def test_stale_entry_removed_is_recorded_in_cancel_audit(tmp_path) -> None:
    """An entry the exchange already forgot is still worth one audit line."""
    client = FakeClient()
    binance_usdm_testnet._persist_pending(
        "BTCUSDT",
        {
            "client_id": "pa-entry-dead",
            "signal_id": "dead-signal",
            "side": "BUY",
            "quantity": "1",
            "stop": "90",
            "target": "120",
            "entry": "100",
            "placed_at": time.time() - 60,
        },
    )

    def missing(**_kwargs: object) -> str:
        raise BinanceAPIError('Binance HTTP 400: {"code":-2013,"msg":"Order does not exist."}')

    client.order_status = missing  # type: ignore[method-assign]

    assert binance_usdm_testnet._replace_pending_limit(client, "BTCUSDT") is None
    records = _cancel_records(tmp_path)
    assert [record["reason"] for record in records] == ["stale_entry_removed"]
    assert records[0]["client_id"] == "pa-entry-dead"


# ── 同价位轮换冷却 (same-level repricing cooldown) ──────────────────────


def _stub_watcher_thread(monkeypatch) -> list[dict]:
    started: list[dict] = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon: bool = True) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            started.append(self.kwargs)

    monkeypatch.setattr(binance_usdm_testnet.threading, "Thread", FakeThread)
    return started


def _seed_last_canceled_entry(
    symbol: str, entry: str, *, age_seconds: float = 60.0, reason: str = "limit_entry_timeout"
) -> None:
    with binance_usdm_testnet._STATE_LOCK:
        state = binance_usdm_testnet._load_state()
        state.setdefault("last_canceled_entries", {})[symbol] = {
            "entry": entry,
            "reason": reason,
            "ts": time.time() - age_seconds,
        }
        binance_usdm_testnet._save_state(state)


def _limit_decision(entry: float = 95) -> dict:
    return _long_decision() | {"order_type": "限价单", "entry_price": entry}


def test_same_level_reentry_inside_cooldown_is_skipped(monkeypatch) -> None:
    """Cancel-then-rehang the same level is churn: skip instead of re-placing."""
    _stub_watcher_thread(monkeypatch)
    _seed_last_canceled_entry("BTCUSDT", "95.2")  # 2 ticks away (tick size 0.1)
    client = FakeClient()

    result = execute_market_signal(
        _limit_decision(95), _settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "skipped", result.reason
    assert "同价位轮换冷却" in result.reason
    assert "limit_entry" not in [call[0] for call in client.calls]


def test_reentry_far_from_the_last_cancel_is_placed(monkeypatch) -> None:
    started = _stub_watcher_thread(monkeypatch)
    _seed_last_canceled_entry("BTCUSDT", "90")  # 50 ticks away
    client = FakeClient()

    result = execute_market_signal(
        _limit_decision(95), _settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "pending", result.reason
    assert [call[0] for call in client.calls].count("limit_entry") == 1
    assert started, "an accepted entry must arm its fill watcher"


def test_reentry_is_allowed_after_the_cooldown_expires(monkeypatch) -> None:
    _stub_watcher_thread(monkeypatch)
    _seed_last_canceled_entry("BTCUSDT", "95.2", age_seconds=31 * 60)
    client = FakeClient()

    result = execute_market_signal(
        _limit_decision(95), _settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "pending", result.reason


def test_repricing_cooldown_can_be_disabled(monkeypatch) -> None:
    _stub_watcher_thread(monkeypatch)
    _seed_last_canceled_entry("BTCUSDT", "95")
    settings = _settings()
    settings.binance_usdm_testnet.limit_repricing_min_ticks = 0
    client = FakeClient()

    result = execute_market_signal(
        _limit_decision(95), settings, analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "pending", result.reason


def test_timed_out_cancel_remembers_entry_for_the_cooldown(tmp_path) -> None:
    client = FakeClient()
    client.limit_orders["pa-entry-timeout"] = {"status": "NEW"}
    _persist_pending_record("BTCUSDT", "pa-entry-timeout", "timeout-signal")

    binance_usdm_testnet._cancel_and_settle(
        client,
        "BTCUSDT",
        "pa-entry-timeout",
        "BUY",
        Decimal("90"),
        Decimal("120"),
        Decimal("167.79"),
        "timeout-signal",
        context="timed out",
    )

    remembered = (_state().get("last_canceled_entries") or {}).get("BTCUSDT") or {}
    assert remembered.get("entry") == "100"
    assert remembered.get("reason") == "limit_entry_timeout"


def test_replaced_entry_remembers_entry_for_the_cooldown(tmp_path) -> None:
    client = FakeClient()
    client.limit_orders["pa-entry-old"] = {"status": "NEW"}
    _persist_pending_record("BTCUSDT", "pa-entry-old", "old-signal")

    assert binance_usdm_testnet._replace_pending_limit(client, "BTCUSDT") is None

    remembered = (_state().get("last_canceled_entries") or {}).get("BTCUSDT") or {}
    assert remembered.get("entry") == "100"
    assert remembered.get("reason") == "plan_replaced"



