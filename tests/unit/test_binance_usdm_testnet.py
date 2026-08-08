"""Tests for isolated Binance USDⓈ-M Testnet execution."""

from __future__ import annotations

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

    def place_close_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("protection", kwargs))

    def cancel_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("cancel_protection", kwargs))

    def close_market_position(self, **kwargs: object) -> None:
        self.calls.append(("rollback", kwargs))


class FailSecondProtectionClient(FakeClient):
    def place_close_algo_order(self, **kwargs: object) -> None:
        super().place_close_algo_order(**kwargs)
        if len([call for call in self.calls if call[0] == "protection"]) == 2:
            raise BinanceAPIError("take-profit rejected")


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


def test_only_market_order_is_automated() -> None:
    client = FakeClient()
    decision = _long_decision() | {"order_type": "限价单"}
    result = execute_market_signal(decision, _settings(), analysis_symbol="BTCUSDT", client=client)
    assert result.status == "rejected"
    assert not client.calls


def test_protection_failure_cancels_first_order_and_rolls_back_entry() -> None:
    client = FailSecondProtectionClient()
    result = execute_market_signal(
        _long_decision(), _settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "failed"
    assert [call[0] for call in client.calls][-2:] == ["cancel_protection", "rollback"]
