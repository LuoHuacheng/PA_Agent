# ruff: noqa: RUF002 - Chinese docstrings
"""Tests for the process-wide rate-limit gate and guard cooldown jitter.

Covers the "one global gate" fixes:

- The request layer refuses to send any Binance request while a ban is live.
- current_position / current_mark_price refuse the direct-REST fallback during
  a ban once the shared snapshot has gone stale.
- Guard loops sleep through the ban plus a small wake-up jitter so that, when
  the ban lifts, the per-symbol managers do not all fire at once and re-trip
  the shared-IP limit.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.trading import binance_usdm_testnet as bn
from pa_agent.trading.binance_usdm_testnet import BinanceAPIError, BinanceUSDMTestnetClient
from pa_agent.trading.rate_limit import RateLimitBreaker


def _banned_breaker() -> RateLimitBreaker:
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=9999999999999)
    return breaker


class _CM:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def test_request_layer_blocks_before_sending_when_banned(monkeypatch) -> None:
    """封禁期间请求层直接抛限流错误，不向交易所发出任何请求。"""
    monkeypatch.setattr(bn, "rate_limiter", _banned_breaker())
    sent: list[str] = []

    def opener(request, timeout):
        sent.append(request.full_url)
        return _CM(b'{"serverTime": 1}')

    client = BinanceUSDMTestnetClient("k", "s", opener=opener)
    with pytest.raises(BinanceAPIError) as err:
        client.mark_price("BTCUSDT")
    assert "418" in str(err.value)
    assert sent == [], "banned 时不得发出请求"


def test_current_position_refuses_direct_call_when_banned_and_snapshot_stale(monkeypatch) -> None:
    """快照过期且封禁中：抛限流错误而不是逐品种直连。"""
    monkeypatch.setattr(bn, "rate_limiter", _banned_breaker())

    class _StalePoller:
        def snapshot_ready(self) -> bool:
            return False

    monkeypatch.setattr(bn, "_snapshot_poller", _StalePoller())

    class _Spy:
        def __init__(self) -> None:
            self.direct = 0

        def position_info(self, symbol: str) -> dict:
            self.direct += 1
            return {"amount": Decimal("1"), "entry": Decimal("100")}

    client = _Spy()
    with pytest.raises(BinanceAPIError) as err:
        bn.current_position(client, "BTCUSDT")
    assert "418" in str(err.value)
    assert client.direct == 0, "封禁期间不得直连 position_info"


def test_current_mark_price_refuses_direct_call_when_banned_and_snapshot_stale(monkeypatch) -> None:
    monkeypatch.setattr(bn, "rate_limiter", _banned_breaker())

    class _StalePoller:
        def snapshot_ready(self) -> bool:
            return False

    monkeypatch.setattr(bn, "_snapshot_poller", _StalePoller())

    class _Spy:
        def __init__(self) -> None:
            self.direct = 0

        def mark_price(self, symbol: str) -> Decimal:
            self.direct += 1
            return Decimal("100")

    client = _Spy()
    with pytest.raises(BinanceAPIError) as err:
        bn.current_mark_price(client, "BTCUSDT")
    assert "418" in str(err.value)
    assert client.direct == 0


class _Remaining:
    def __init__(self, remaining: float | None) -> None:
        self._remaining = remaining

    def remaining_seconds(self) -> float | None:
        return self._remaining


def test_guard_rate_limit_wait_sleeps_through_ban_plus_jitter(monkeypatch) -> None:
    """guard 冷却 = 穿ban剩余时间 + 5s 余量 + 随机抖动，避免解禁同时醒来。"""
    monkeypatch.setattr(bn, "rate_limiter", _Remaining(10.0))
    monkeypatch.setattr(bn.random, "uniform", lambda lo, hi: 2.5)
    sleeps: list[float] = []
    monkeypatch.setattr(bn.time, "sleep", sleeps.append)
    bn._guard_rate_limit_wait(1.0)
    assert sleeps == [17.5]


def test_guard_rate_limit_wait_uses_poll_cadence_when_not_banned(monkeypatch) -> None:
    monkeypatch.setattr(bn, "rate_limiter", _Remaining(None))
    monkeypatch.setattr(bn.random, "uniform", lambda lo, hi: 0.0)
    sleeps: list[float] = []
    monkeypatch.setattr(bn.time, "sleep", sleeps.append)
    bn._guard_rate_limit_wait(2.0)
    assert sleeps == [2.0]
