"""Tests for isolated Binance USDⓈ-M Testnet execution."""

from __future__ import annotations

import io
import json
import time
from decimal import Decimal
from http.client import RemoteDisconnected
from urllib.error import HTTPError

import pytest

from pa_agent.config.settings import Settings
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
    deadline = time.monotonic() + 5.0
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
    client = FakeClient()
    # gap 0.3% >= 0.2% minimum → allowed
    decision = _long_decision() | {"stop_loss_price": 99.7}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
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
    client = FakeClient()
    decision = _long_decision() | {
        "order_type": "限价单",
        "entry_price": 120,
        "stop_loss_price": 99.7,  # vs mark 100 → 0.3% gap >= minimum
        "take_profit_price": 150,
    }
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
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

