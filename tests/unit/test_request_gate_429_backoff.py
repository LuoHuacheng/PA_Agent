
"""Request gate spacing and bare-429 exponential breaker backoff.

Covers the "flatten the pulse" fixes:

- Every outbound Binance request passes a process-wide gate that caps
  concurrency and spaces request starts by a minimum gap, so daemon threads
  (guards / TP runners / snapshot poller) can no longer burst together at bar
  close or right after a ban lifts.
- A bare HTTP 429 (no banned-until timestamp) now escalates the local breaker
  window 60s -> 120s -> 240s (cap 300s) while the shared IP keeps answering
  429; a real banned-until response resets the escalation.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.trading import binance_usdm_testnet as bn
from pa_agent.trading.rate_limit import RateLimitBreaker

# ---------------------------------------------------------------------------
# 429 exponential backoff
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    breaker = RateLimitBreaker()
    monkeypatch.setattr(bn, "rate_limiter", breaker)
    monkeypatch.setattr(bn, "_429_strikes", 0)
    monkeypatch.setattr(bn, "_429_last_ts", 0.0)
    monkeypatch.setattr(bn.time, "time", lambda: 1_000_000.0)
    return breaker


def _bare429() -> str:
    return 'Binance HTTP 429: {"code":-1003,"msg":"Too many requests; current limit of IP(3.172.30.75) is 6000 requests per minute"}'


def test_bare_429_escalates_breaker_window(_isolate) -> None:
    """连续无 until 的 429: 本地熔断 60s -> 120s -> 240s(上限 300s)."""
    assert _isolate.banned_until_ms() is None
    bn._observe_rate_limit_error(_bare429())
    assert _isolate.banned_until_ms() == 1_000_060_000
    bn._observe_rate_limit_error(_bare429())
    assert _isolate.banned_until_ms() == 1_000_120_000
    bn._observe_rate_limit_error(_bare429())
    assert _isolate.banned_until_ms() == 1_000_240_000
    bn._observe_rate_limit_error(_bare429())  # 480 -> cap 300
    assert _isolate.banned_until_ms() == 1_000_300_000


class _SpyBreaker:
    """记录 record_ban(until_ms=...) 而不做 max-merge, 便于观察退避窗口本身."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def record_ban(self, *, until_ms: int | None = None, now_ms: int | None = None) -> None:
        self.calls.append(int(until_ms or 0))


def test_418_with_banned_until_resets_escalation(_isolate, monkeypatch) -> None:
    """真实 banned-until(418) 直接生效并清零 429 连击计数. """
    spy = _SpyBreaker()
    monkeypatch.setattr(bn, "rate_limiter", spy)
    bn._observe_rate_limit_error(_bare429())
    bn._observe_rate_limit_error(_bare429())  # 当前 120s 档
    until_ms = 9_000_000_000
    bn._observe_rate_limit_error(f"Binance HTTP 418: banned until {until_ms}")
    bn._observe_rate_limit_error(_bare429())  # 重置后回到 60s 档
    assert spy.calls == [1_000_060_000, 1_000_120_000, until_ms, 1_000_060_000]


def test_429_escalation_expires_after_idle_window(_isolate, monkeypatch) -> None:
    """距上次 429 超过 600s 后连击计数过期, 重新从 60s 起步."""
    spy = _SpyBreaker()
    monkeypatch.setattr(bn, "rate_limiter", spy)
    bn._observe_rate_limit_error(_bare429())
    bn._observe_rate_limit_error(_bare429())  # 120s 档
    monkeypatch.setattr(bn, "_429_last_ts", 999_000.0)  # 1000s 前
    bn._observe_rate_limit_error(_bare429())
    assert spy.calls == [1_000_060_000, 1_000_120_000, 1_000_060_000]


def test_non_rate_limit_error_is_ignored(_isolate) -> None:
    bn._observe_rate_limit_error("Binance HTTP 400: {\"code\":-1111}")
    assert _isolate.banned_until_ms() is None


# ---------------------------------------------------------------------------
# Request gate
# ---------------------------------------------------------------------------


def test_request_gate_spaces_consecutive_starts(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(bn.time, "sleep", sleeps.append)
    gate = bn._RequestGate(max_concurrency=2, min_gap_seconds=0.2)
    with gate:
        pass  # first request: no spacing needed
    with gate:
        pass  # second start must wait for the min gap
    assert len(sleeps) == 1
    assert 0.15 <= sleeps[0] <= 0.25


def test_request_gate_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        bn._RequestGate(max_concurrency=0, min_gap_seconds=0.2)
    with pytest.raises(ValueError):
        bn._RequestGate(max_concurrency=2, min_gap_seconds=-1)


def test_module_gate_defaults_are_sane() -> None:
    assert bn._REQUEST_GATE._min_gap >= 0.1
    assert bn._REQUEST_GATE._sem._value == 2  # 并发上限 2


# ---------------------------------------------------------------------------
# Snapshot poller freshness / jitter wiring
# ---------------------------------------------------------------------------


class _FakeClient:
    def all_mark_prices(self) -> dict[str, Decimal]:
        return {}

    def all_positions(self) -> dict[str, dict]:
        return {}


def test_snapshot_stale_auto_scales_with_poll() -> None:
    short = bn.AccountSnapshotPoller(_FakeClient(), poll_seconds=10)
    assert short._stale_after == 45.0  # max(45, 12)
    long = bn.AccountSnapshotPoller(_FakeClient(), poll_seconds=60)
    assert long._stale_after == 72.0  # max(45, 72)
    explicit = bn.AccountSnapshotPoller(
        _FakeClient(), poll_seconds=10, stale_after_seconds=90
    )
    assert explicit._stale_after == 90.0


def test_snapshot_stale_boundary_uses_configured_limit() -> None:
    poller = bn.AccountSnapshotPoller(
        _FakeClient(), poll_seconds=60, stale_after_seconds=72,
        clock=lambda: 1_000.0,
    )
    poller.refresh()
    assert poller.snapshot_ready(now=1_072.0) is True
    assert poller.snapshot_ready(now=1_072.01) is False


def test_poller_loop_applies_jitter_to_wait(monkeypatch) -> None:
    client = _FakeClient()
    client.all_mark_prices = lambda: {}
    poller = bn.AccountSnapshotPoller(client, poll_seconds=10)
    waits: list[float] = []

    class _Stop:
        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) >= 2  # stop after one refresh

    monkeypatch.setattr(bn.random, "uniform", lambda lo, hi: 0.1)
    monkeypatch.setattr(poller, "_stop", _Stop())
    monkeypatch.setattr(poller, "refresh", lambda: None)
    poller._run()
    assert len(waits) == 2
    assert waits[0] == pytest.approx(11.0)  # 10 * (1 + 0.1)
