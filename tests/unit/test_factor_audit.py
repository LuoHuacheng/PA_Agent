"""时序因子审计的统计核心."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import compute_indicators
from tools.factor_audit import (
    AuditThresholds,
    BarFact,
    audit_frames,
    bar_facts_for,
    dedupe_facts,
    forward_return_at,
    geometry_extractors,
    ic_stats,
    spearman_ic,
)

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


def _frame(closes: list[float], *, symbol: str = "BTCUSDT") -> KlineFrame:
    """closes 按"旧到新"给出; 返回 newest-first 的 KlineFrame."""
    built = [
        KlineBar(
            seq=len(closes) - i,
            ts_open=1_700_000_000.0 + i * 900.0,
            open=float(c),
            high=float(c) + 1.0,
            low=float(c) - 1.0,
            close=float(c),
            volume=10.0,
        )
        for i, c in enumerate(closes)
    ]
    built.reverse()
    bars = [replace(bar, seq=i + 1) for i, bar in enumerate(built)]
    return KlineFrame(
        symbol=symbol,
        timeframe="15m",
        bars=tuple(bars),
        indicators=compute_indicators(bars),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


def test_forward_return_at_uses_the_newer_neighbour_bar():
    frame = _frame([100.0, 101.0, 102.0])
    assert forward_return_at(frame, 2) == pytest.approx(101.0 / 100.0 - 1.0)
    assert forward_return_at(frame, 1) == pytest.approx(102.0 / 101.0 - 1.0)


def test_forward_return_at_is_none_for_the_newest_bar():
    frame = _frame([100.0, 101.0, 102.0])
    assert forward_return_at(frame, 0) is None


def test_bar_facts_exclude_the_newest_bar_and_short_history():
    frame = _frame([100.0 + i for i in range(60)])
    extractor = geometry_extractors()["geometry.body_ratio"]
    facts = bar_facts_for(frame, "geometry.body_ratio", extractor, min_history=20)
    assert facts
    newest_ts = int(frame.bars[0].ts_open * 1000)
    assert all(fact.ts_ms != newest_ts for fact in facts)
    # 历史不足 min_history 的尾部 bar 也不该产出观测
    oldest_used_ts = int(frame.bars[-1].ts_open * 1000)
    assert all(fact.ts_ms >= oldest_used_ts for fact in facts)


def test_geometry_extractors_expose_all_numeric_fields():
    names = set(geometry_extractors())
    assert names == {
        "geometry.body_ratio",
        "geometry.upper_wick_ratio",
        "geometry.lower_wick_ratio",
        "geometry.close_position",
        "geometry.range_atr_ratio",
        "geometry.overlap_prev_ratio",
    }


def test_audit_frames_returns_one_stats_row_per_extractor():
    frames = [
        _frame([100.0 + i for i in range(60)], symbol="BTCUSDT"),
        _frame([200.0 - i for i in range(60)], symbol="ETHUSDT"),
    ]
    extractors = {
        "geometry.body_ratio": geometry_extractors()["geometry.body_ratio"],
        "constant_zero": lambda frame: 0.0,
    }
    stats = {s.name: s for s in audit_frames(frames, extractors)}
    assert set(stats) == {"geometry.body_ratio", "constant_zero"}
    # 恒定特征无秩 => 无有效 IC
    assert stats["constant_zero"].passed is False
    assert stats["constant_zero"].ic_mean == 0.0


def test_dedupe_keeps_the_deepest_history_for_a_repeated_bar():
    shallow = BarFact("BTCUSDT", 1000, 0.5, 0.01, history=5)
    deep = BarFact("BTCUSDT", 1000, 0.9, 0.01, history=60)
    other = BarFact("BTCUSDT", 2000, 0.1, 0.02, history=30)
    got = dedupe_facts([shallow, other, deep])
    assert [f.ts_ms for f in got] == [1000, 2000]
    assert got[0].factor == 0.9


def test_audit_frames_dedupes_bars_shared_by_two_snapshots():
    # 两份快照覆盖同一批 bar: 去重后观测数不应翻倍
    first = _frame([100.0 + i for i in range(60)])
    second = _frame([100.0 + i for i in range(60)])
    extractors = {"geometry.body_ratio": geometry_extractors()["geometry.body_ratio"]}
    single = audit_frames([first], extractors)[0].n_obs
    doubled = audit_frames([first, second], extractors)[0].n_obs
    assert single > 0
    assert doubled == single
