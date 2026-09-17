"""看护循环容错预算: 瞬时网络错误只退避, 不弃守。

背景(2026-09-17 ZEC 事故): 04:56-05:01 Binance SSL UNEXPECTED_EOF 连打, TP
runner / stop watchdog 各记满 5 次即弃守退出; 11:25 TP1 半平成交时已无人看护,
剩余仓位再没挂上 TP2, 最终只靠一张保本止损兜底。
"""
from __future__ import annotations

from pa_agent.trading.guard_loop import (
    GUARD_MAX_ERRORS,
    MAX_BACKOFF_SECONDS,
    ErrorBudget,
    is_transient_error,
)


def test_transient_error_matches_ssl_eof():
    assert is_transient_error(
        Exception(
            "Binance network error: [SSL: UNEXPECTED_EOF_WHILE_READING] "
            "EOF occurred in violation of protocol (_ssl.c:1016)"
        )
    )


def test_transient_error_matches_connection_reset():
    assert is_transient_error(Exception("Binance network error: Connection reset by peer"))
    assert is_transient_error(Exception("Binance HTTP 503: service unavailable"))


def test_transient_error_rejects_business_rejection():
    assert not is_transient_error(
        Exception('Binance HTTP 400: {"code":-1111,"msg":"Precision is over the maximum defined for this asset"}')
    )
    assert not is_transient_error(Exception('Binance HTTP 400: {"code":-2011,"msg":"Unknown order sent."}'))


def test_transient_backoff_doubles_then_caps():
    budget = ErrorBudget()
    assert budget.record_transient(1.0) == 1.0
    assert budget.record_transient(1.0) == 2.0
    assert budget.record_transient(1.0) == 4.0
    assert budget.record_transient(1.0) == 8.0
    assert budget.record_transient(1.0) == 16.0
    assert budget.record_transient(1.0) == 32.0
    assert budget.record_transient(1.0) == 64.0
    assert budget.record_transient(1.0) == 128.0
    assert budget.record_transient(1.0) == 256.0
    assert budget.record_transient(1.0) == MAX_BACKOFF_SECONDS


def test_transient_failures_do_not_exhaust_fatal_budget():
    budget = ErrorBudget()
    for _ in range(GUARD_MAX_ERRORS * 3):
        budget.record_transient(1.0)
    assert budget.record() is False, "瞬时抖动不许烧掉致命预算"


def test_reset_clears_both_counters():
    budget = ErrorBudget()
    budget.record()
    budget.record_transient(1.0)
    budget.reset()
    assert budget.count == 0
    assert budget.transient == 0
