"""时序因子审计的统计核心."""

from __future__ import annotations

from tools.factor_audit import AuditThresholds, BarFact, ic_stats, spearman_ic

BASE_TS = 1_700_000_000_000
DAY_MS = 86_400_000


def _fact(symbol: str, ts_ms: int, factor: float, forward: float) -> BarFact:
    return BarFact(symbol=symbol, ts_ms=ts_ms, factor=factor, forward_return=forward)


def _increasing_rows(symbol: str, *, swaps: int) -> list[BarFact]:
    """factor 单调递增; forward 在 swaps 处做相邻逆序对, 用来压低 IC。"""
    factors = [float(i) for i in range(40)]
    forwards = [float(i) for i in range(40)]
    for k in range(swaps):
        forwards[2 * k], forwards[2 * k + 1] = forwards[2 * k + 1], forwards[2 * k]
    return [_fact(symbol, BASE_TS + i * DAY_MS, factors[i], forwards[i]) for i in range(40)]


def test_spearman_perfect_positive():
    assert spearman_ic([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == 1.0


def test_spearman_perfect_negative():
    assert spearman_ic([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) == -1.0


def test_spearman_is_rank_based_not_linear():
    # 单调但非线性, Spearman 仍为 1
    assert spearman_ic([1, 2, 3, 4, 5], [1, 4, 9, 16, 25]) == 1.0


def test_spearman_returns_none_for_constant_series():
    assert spearman_ic([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None


def test_spearman_returns_none_for_too_few_or_mismatched():
    assert spearman_ic([1, 2], [1, 2]) is None
    assert spearman_ic([1, 2, 3], [1, 2]) is None


def test_ic_stats_passes_a_predictive_factor():
    rows = _increasing_rows("BTCUSDT", swaps=0) + _increasing_rows("ETHUSDT", swaps=3)
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.n_obs == 80
    assert stats.group_count == 2
    assert stats.ic_mean > 0.9
    assert stats.ir > 0.3
    assert stats.monthly_win_rate > 0.5
    assert stats.passed is True
    assert stats.reasons == ()


def test_ic_stats_rejects_a_direction_flipped_factor():
    rows = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(-i)) for i in range(40)]
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.ic_mean < -0.9
    assert stats.passed is False
    assert any("方向" in reason for reason in stats.reasons)


def test_ic_stats_rejects_a_too_small_sample():
    rows = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(5)]
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.passed is False
    assert any("观测数" in reason for reason in stats.reasons)


def test_ic_stats_respects_negative_expected_direction():
    rows = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(-i)) for i in range(40)]
    stats = ic_stats("geometry.body_ratio", rows, expected_direction="negative")
    assert stats.ic_mean < -0.9
    assert not any("方向" in reason for reason in stats.reasons)


def test_thresholds_are_overridable():
    rows = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(40)]
    strict = AuditThresholds(min_obs=1000)
    assert ic_stats("x", rows, thresholds=strict).passed is False
