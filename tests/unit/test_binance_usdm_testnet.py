"""Tests for isolated Binance USDⓈ-M Testnet execution."""

from __future__ import annotations

import json
import time
from decimal import Decimal

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
        "exchange_info",
        "mark_price",
        "set_leverage",
        "entry",
        "protection",
        "protection",
    ]
    protections = [call[1] for call in client.calls if call[0] == "protection"]
    assert {order["order_type"] for order in protections} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}


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
    result = execute_market_signal(_long_decision(), _settings(), analysis_symbol="BTCUSDT", client=client)
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
    assert not binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError("upstream proxy: order does not exist")
    )
    assert not binance_usdm_testnet._is_missing_order_error(
        BinanceAPIError("Binance error -2014: Order does not exist")
    )
