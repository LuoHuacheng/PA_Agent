"""Tests for AI-client rate-limit detection and backoff retry."""

from __future__ import annotations

import pytest

from pa_agent.ai.rate_limit import call_with_rate_limit_backoff, is_rate_limit_error


class _Http429(Exception):
    status_code = 429


class _Boom(Exception):
    status_code = 500


class _CancelToken:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def is_set(self) -> bool:
        return self._cancelled


def test_detects_rate_limit_by_status_and_text():
    assert is_rate_limit_error(_Http429("boom"))
    assert is_rate_limit_error(RuntimeError("Error code: 429 - too many requests"))
    assert is_rate_limit_error(RuntimeError("rate_limit exceeded"))
    assert not is_rate_limit_error(_Boom("server error"))
    assert not is_rate_limit_error(None)


def test_detects_chained_cause_rate_limit():
    try:
        try:
            raise _Http429("inner")
        except _Http429 as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as exc:
        assert is_rate_limit_error(exc)


def test_backoff_retries_then_succeeds():
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _Http429("slow down")
        return "ok"

    assert call_with_rate_limit_backoff(fn, base_s=0.0, max_wait_s=0.0) == "ok"
    assert len(calls) == 3


def test_backoff_raises_after_max_retries():
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        raise _Http429("slow down")

    with pytest.raises(_Http429):
        call_with_rate_limit_backoff(fn, max_retries=2, base_s=0.0, max_wait_s=0.0)
    assert len(calls) == 3  # 首次 + 2 次重试


def test_backoff_propagates_non_rate_limit_immediately():
    def fn() -> str:
        raise _Boom("server error")

    with pytest.raises(_Boom):
        call_with_rate_limit_backoff(fn, base_s=0.0, max_wait_s=0.0)


def test_backoff_stops_when_cancel_token_set():
    def fn() -> str:
        raise _Http429("slow down")

    with pytest.raises(_Http429):
        call_with_rate_limit_backoff(
            fn,
            base_s=0.0,
            max_wait_s=0.0,
            cancel_token=_CancelToken(cancelled=True),
        )
