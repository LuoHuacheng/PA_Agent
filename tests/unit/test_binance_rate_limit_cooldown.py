# ruff: noqa: RUF002 - Chinese docstrings
"""Guard loops sleep through Binance rate-limit bans instead of abandoning."""
from __future__ import annotations

import io
import json
import time
from urllib.error import HTTPError

import pytest

from pa_agent.trading import binance_usdm_testnet as bn
from pa_agent.trading import position_manager as pm
from pa_agent.trading.binance_usdm_testnet import BinanceAPIError, BinanceUSDMTestnetClient
from pa_agent.trading.rate_limit import RateLimitBreaker


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "_RUNTIME_STATE_PATH", str(tmp_path / "state.json"))
    breaker = RateLimitBreaker()
    monkeypatch.setattr(bn, "rate_limiter", breaker)
    return breaker


def _register_guard() -> None:
    bn._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-old0001", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 60, "ts": time.time(), "moved": False},
    )


class BannedClient:
    """Position polls always answered with an HTTP 418 ban."""

    def __init__(self) -> None:
        self.polls = 0

    def position_info(self, symbol: str) -> dict:
        self.polls += 1
        raise BinanceAPIError(
            "Binance HTTP 418: " + json.dumps({
                "code": -1003,
                "msg": "Way too many requests; IP(3.172.30.75) banned until 9999999999999",
            })
        )

    def mark_price(self, symbol: str) -> None:
        raise AssertionError("mark_price must not be reached while banned")


def test_breakeven_guard_does_not_give_up_while_banned(monkeypatch) -> None:
    """限流错误不算弃守计数：ban 期间不直连，sleep 穿 ban 也不退出。"""
    client = BannedClient()
    sleeps: list[float] = []
    monkeypatch.setattr(bn.time, "sleep", sleeps.append)
    monkeypatch.setattr(bn, "rate_limiter", _Remaining(3600.0))
    monkeypatch.setattr(bn.random, "uniform", lambda lo, hi: 0.0)
    records = [{"stop_algo_id": "pa-sl-old0001", "stop0": "90", "target": "120",
                "side": "BUY", "conf": 60, "ts": time.time(), "moved": False}] * 8 + [None]

    def _read_guard(_symbol: str) -> dict | None:
        return records.pop(0)

    monkeypatch.setattr(pm, "_read_guard", _read_guard)
    bn._breakeven_guard_loop(client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0)

    assert client.polls == 0, "ban 期间不得直连 position_info"
    assert len(sleeps) == 8
    assert sleeps == [300.0] * 8, "must sleep the capped cooldown, not 1s poll spam"


def test_rate_limit_cooldown_seconds_formula(monkeypatch) -> None:
    cooldown = bn._rate_limit_cooldown_seconds

    monkeypatch.setattr(bn, "rate_limiter", _Remaining(3600.0))
    assert cooldown(1.0) == 300.0  # cap

    monkeypatch.setattr(bn, "rate_limiter", _Remaining(10.0))
    assert cooldown(1.0) == 15.0  # remaining + 5s margin

    monkeypatch.setattr(bn, "rate_limiter", _Remaining(240.0))
    assert cooldown(30.0) == 245.0  # under cap, over poll

    monkeypatch.setattr(bn, "rate_limiter", _Remaining(None))
    assert cooldown(1.0) == 1.0  # not banned: normal poll cadence


class _Remaining:
    def __init__(self, remaining: float | None) -> None:
        self._remaining = remaining

    def remaining_seconds(self) -> float | None:
        return self._remaining

    def is_banned(self) -> bool:
        return self._remaining is not None

    def banned_until_ms(self) -> int:
        return 9999999999999


def test_request_layer_records_ban_on_http_418(_isolate) -> None:
    """HTTP 418 抛错前把 banned-until 窗口记入熔断器。"""
    until_ms = 1788768959045
    body = json.dumps({"code": -1003, "msg": f"Way too many requests; IP banned until {until_ms}"})

    def opener(request, timeout):
        raise HTTPError(request.full_url, 418, "Too Many Requests", hdrs=None,
                        fp=io.BytesIO(body.encode()))

    client = BinanceUSDMTestnetClient("k", "s", opener=opener)
    with pytest.raises(BinanceAPIError) as err:
        client.mark_price("BTCUSDT")
    assert "Binance HTTP 418" in str(err.value)
    assert _isolate.banned_until_ms() == until_ms
    assert _isolate.is_banned(now_ms=until_ms - 1) is True


def test_request_layer_records_ban_on_json_code_minus_1003(_isolate) -> None:
    """HTTP 200 + code -1003 的响应同样记录封禁。"""
    until_ms = 1788768959045
    body = json.dumps({"code": -1003, "msg": f"Way too many requests; IP banned until {until_ms}"})

    class _CM:
        def __init__(self, raw: bytes) -> None:
            self._raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return self._raw

    def opener(request, timeout):
        return _CM(body.encode())

    client = BinanceUSDMTestnetClient("k", "s", opener=opener)
    with pytest.raises(BinanceAPIError) as err:
        client.mark_price("BTCUSDT")
    assert "Binance error -1003" in str(err.value)
    assert _isolate.banned_until_ms() == until_ms
