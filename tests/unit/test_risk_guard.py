"""日度亏损熔断的纯函数单测."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pa_agent.trading import risk_guard

TZ8 = timezone(timedelta(hours=8))


def _rows(*pairs) -> list[dict]:
    return [{"incomeType": kind, "income": str(value)} for kind, value in pairs]


def test_realized_net_counts_only_realized_and_commission() -> None:
    """资金费与转账不计入 (与 repo 口径 net = realized + fees 一致)."""
    rows = _rows(
        ("REALIZED_PNL", -12.5),
        ("COMMISSION", -1.5),
        ("FUNDING_FEE", 3.0),
        ("TRANSFER", 100.0),
    )
    assert risk_guard.realized_net(rows) == -14.0


def test_realized_net_tolerates_junk_rows() -> None:
    rows = [{"incomeType": "REALIZED_PNL", "income": "abc"}, None, "x", {}, {"income": "1"}]
    assert risk_guard.realized_net(rows) == 0.0
    assert risk_guard.realized_net(None) == 0.0


def test_breach_respects_limit_sign_and_disabled_zero() -> None:
    losing = _rows(("REALIZED_PNL", -20.0), ("COMMISSION", -1.0))
    assert risk_guard.breach(losing, 0.0) == (False, -21.0)   # 0 = 关闭
    assert risk_guard.breach(losing, 30.0) == (False, -21.0)
    assert risk_guard.breach(losing, 20.0) == (True, -21.0)
    assert risk_guard.breach(losing, -20.0) == (False, -21.0)  # 非法值视为关闭
    assert risk_guard.breach(_rows(("REALIZED_PNL", 5.0)), 1.0) == (False, 5.0)


def test_halt_record_expires_on_next_local_day() -> None:
    now = int(datetime(2026, 9, 10, 23, 0, tzinfo=TZ8).timestamp() * 1000)
    state = {risk_guard.HALT_KEY: risk_guard.build_halt(-31.0, 30.0, now)}
    assert risk_guard.halt_record(state, now) is not None
    assert risk_guard.halt_record(state, now + 3 * 3600 * 1000) is None  # 次日 02:00
    assert risk_guard.halt_record({}, now) is None
    assert risk_guard.halt_record({"risk_halt": "junk"}, now) is None


def test_build_halt_and_day_start_use_local_midnight() -> None:
    now = int(datetime(2026, 9, 10, 15, 30, tzinfo=TZ8).timestamp() * 1000)
    halt = risk_guard.build_halt(-33.5, 30.0, now)
    assert halt["day"] == "20260910"
    assert halt["net_usdt"] == -33.5
    assert halt["limit_usdt"] == 30.0
    start = datetime.fromtimestamp(risk_guard.day_start_ms(now) / 1000, TZ8)
    assert (start.hour, start.minute, start.day) == (0, 0, 10)
