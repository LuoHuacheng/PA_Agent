"""Unit tests for the Binance shared-IP rate-limit breaker state."""
from __future__ import annotations

from pa_agent.trading.rate_limit import (
    RateLimitBreaker,
    is_rate_limit_text,
    parse_banned_until_ms,
)


def test_parse_banned_until_ms_extracts_epoch_ms() -> None:
    body = (
        'Binance HTTP 418: {"code":-1003,"msg":"Way too many requests; '
        'IP(3.172.30.75) banned until 1788768959045. Please use the websocket '
        'for live updates to avoid bans."}'
    )
    assert parse_banned_until_ms(body) == 1788768959045


def test_parse_banned_until_ms_missing_returns_none() -> None:
    assert parse_banned_until_ms("no ban window here") is None
    assert parse_banned_until_ms("") is None


def test_is_rate_limit_text_matches_ban_markers() -> None:
    assert is_rate_limit_text("Binance HTTP 418: banned until 1788768959045")
    assert is_rate_limit_text('Binance HTTP 429: {"code":-1003,"msg":"Way too many requests"}')
    assert is_rate_limit_text("Binance error -1003: Way too many requests")
    assert not is_rate_limit_text("Binance HTTP 400: invalid symbol")
    assert not is_rate_limit_text("Binance network error: timeout")


def test_breaker_is_banned_until_epoch_passes() -> None:
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=10_000)
    assert breaker.is_banned(now_ms=5_000) is True
    assert breaker.banned_until_ms() == 10_000
    assert breaker.is_banned(now_ms=10_000) is False
    assert breaker.is_banned(now_ms=99_000) is False


def test_breaker_unbanned_by_default() -> None:
    breaker = RateLimitBreaker()
    assert breaker.is_banned() is False
    assert breaker.banned_until_ms() is None
    assert breaker.remaining_seconds() is None


def test_breaker_falls_back_to_window_when_no_until() -> None:
    breaker = RateLimitBreaker(no_until_seconds=90)
    breaker.record_ban(until_ms=None, now_ms=1_000_000)
    assert breaker.is_banned(now_ms=1_000_000) is True
    assert breaker.is_banned(now_ms=1_089_999) is True
    assert breaker.is_banned(now_ms=1_090_000) is False


def test_breaker_keeps_later_ban_when_reported_again() -> None:
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=1_000)
    breaker.record_ban(until_ms=2_000)
    assert breaker.banned_until_ms() == 2_000
    breaker.record_ban(until_ms=1_500)  # 滚动窗口更早的报告不缩短封禁
    assert breaker.banned_until_ms() == 2_000


def test_breaker_clear_resets_state() -> None:
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=10_000)
    breaker.clear()
    assert breaker.is_banned() is False
    assert breaker.banned_until_ms() is None


def test_breaker_remaining_seconds_counts_down_to_zero() -> None:
    breaker = RateLimitBreaker()
    breaker.record_ban(until_ms=10_000)
    assert breaker.remaining_seconds(now_ms=9_000) == 1.0
    assert breaker.remaining_seconds(now_ms=10_000) == 0.0
    assert breaker.remaining_seconds(now_ms=20_000) == 0.0
    assert breaker.is_banned(now_ms=9_000) is True
    assert breaker.remaining_seconds(now_ms=20_000) == 0.0
