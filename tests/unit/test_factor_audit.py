"""时序因子审计的统计核心."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import compute_indicators
from tools.factor_audit import (
    MIN_NULL_SAMPLES,
    AuditThresholds,
    BarFact,
    FactorStats,
    NullQuantiles,
    RepresentativeReport,
    _abs_ic_and_ir,
    _group_facts,
    _quantile,
    audit_frames,
    bar_facts_for,
    build_extractors,
    dedupe_facts,
    factor_correlation_matrix,
    forward_return_at,
    geometry_extractors,
    ic_stats,
    main,
    market_extractors,
    permutation_null,
    render_markdown,
    select_representatives,
    spearman_ic,
    suggest_thresholds,
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
    # 严格大于: 最旧那根 bar 自身绝不能被用作观测
    assert min(fact.ts_ms for fact in facts) > oldest_used_ts


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


def test_build_extractors_defaults_to_both_spaces():
    names = set(build_extractors())
    assert len(names) == 17
    assert len([n for n in names if n.startswith("geometry.")]) == 6
    assert len([n for n in names if n.startswith("market.")]) == 11


def test_build_extractors_can_select_a_single_space():
    names = set(build_extractors("geometry"))
    assert names == set(geometry_extractors())


def test_build_extractors_rejects_unknown_space():
    with pytest.raises(ValueError):
        build_extractors("geometry,nope")


def test_dedupe_does_not_merge_across_timeframes():
    same_ts = 1000
    m5 = BarFact("BTCUSDT", same_ts, 0.5, 0.01, history=5, timeframe="5m")
    m15 = BarFact("BTCUSDT", same_ts, 0.9, 0.01, history=60, timeframe="15m")
    got = dedupe_facts([m5, m15])
    assert len(got) == 2
    assert {f.timeframe for f in got} == {"5m", "15m"}


def test_ic_stats_groups_by_symbol_and_timeframe():
    rows: list[BarFact] = []
    for timeframe in ("5m", "15m"):
        for i in range(40):
            fact = _fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(i))
            rows.append(replace(fact, timeframe=timeframe))
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.group_count == 2
    # 两个周期各自一条完整时序, 不是把 80 个观测混进一组
    assert stats.n_obs == 80
    assert stats.ir_groups == 2


def test_audit_frames_dedupes_bars_shared_by_two_snapshots():
    # 两份快照覆盖同一批 bar: 去重后观测数不应翻倍
    first = _frame([100.0 + i for i in range(60)])
    second = _frame([100.0 + i for i in range(60)])
    extractors = {"geometry.body_ratio": geometry_extractors()["geometry.body_ratio"]}
    single = audit_frames([first], extractors)[0].n_obs
    doubled = audit_frames([first, second], extractors)[0].n_obs
    assert single > 0
    assert doubled == single


def test_ir_not_judged_below_three_groups():
    rows: list[BarFact] = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        rows.extend(_fact(symbol, BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(40))
    stats = ic_stats("x", rows)
    assert stats.ir_groups == 2
    assert not any("IR" in reason for reason in stats.reasons)


def test_identical_group_ics_are_not_rejected_as_zero_ir():
    rows: list[BarFact] = []
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        rows.extend(_fact(symbol, BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(40))
    stats = ic_stats("x", rows)
    assert stats.ir_groups == 3
    assert stats.ic_std <= 1e-12
    assert not any("IR" in reason for reason in stats.reasons)


def test_render_markdown_lists_factors_with_verdict():
    stats = [
        FactorStats(
            name="geometry.body_ratio",
            n_obs=120,
            group_count=3,
            ic_mean=0.05,
            ic_std=0.04,
            ir=1.25,
            monthly_win_rate=0.75,
            passed=True,
            reasons=(),
        ),
        FactorStats(
            name="geometry.range_atr_ratio",
            n_obs=120,
            group_count=3,
            ic_mean=-0.01,
            ic_std=0.05,
            ir=-0.2,
            monthly_win_rate=0.4,
            passed=False,
            reasons=("方向性 IC -0.0100 < 0.02",),
        ),
    ]
    text = render_markdown(stats, frames=2, observations=240, source="records/pending")
    assert "# 因子审计报告" in text
    assert "geometry.body_ratio" in text
    assert "direction IC" in text
    assert "通过" in text and "未通过" in text


def test_cli_returns_two_for_missing_directory(tmp_path):
    assert main(["--records", str(tmp_path / "nope")]) == 2


def test_cli_rejects_unknown_space(tmp_path, capsys):
    # --spaces 未注册时 argparse 会 SystemExit(2) 而不是返回 2, 所以这条能抓住漏注册
    assert main(["--records", str(tmp_path), "--spaces", "nope"]) == 2
    assert "未知因子空间" in capsys.readouterr().err


def test_cli_accepts_a_known_space(tmp_path):
    # 空目录 -> 返回 2; 但参数已被受理, 说明 --spaces 真的注册了
    assert main(["--records", str(tmp_path), "--spaces", "geometry"]) == 2


def test_cli_returns_two_without_records_argument():
    assert main([]) == 2


def test_market_extractors_cover_the_numeric_fields():
    names = set(market_extractors())
    assert "market.range_width_atr" in names
    assert "market.price_position" in names
    assert "market.barbwire_score" in names
    assert "market.zone" not in names  # 分类字段不是因子
    assert "market.swings" not in names


def test_market_extractor_reads_the_newest_bar():
    frame = _frame([100.0 + i for i in range(60)])
    value = market_extractors()["market.price_position"](frame)
    assert value is None or 0.0 <= value <= 1.0


def _noisy_rows(symbol: str, *, seed: int, size: int = 50) -> list[BarFact]:
    """factor 与 forward 完全无关: factor=i, forward 取固定伪随机序列."""
    rng = random.Random(seed)
    return [
        _fact(symbol, BASE_TS + i * DAY_MS, float(i), rng.uniform(-0.01, 0.01)) for i in range(size)
    ]


def _noisy_facts() -> dict[str, list[BarFact]]:
    """每个因子都跨 4 个品种, 保证 _abs_ic_and_ir 有 >= 2 个有效组."""
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
    return {
        f"f{i}": [
            row
            for j, symbol in enumerate(symbols)
            for row in _noisy_rows(symbol, seed=100 + 10 * i + j)
        ]
        for i in range(4)
    }


def test_market_extractors_exclude_absolute_price_levels():
    names = set(market_extractors())
    assert "market.range_high" not in names
    assert "market.range_low" not in names
    assert "market.price_position" in names


def test_abs_ic_and_ir_matches_ic_stats_caliber():
    rows = _increasing_rows("BTCUSDT", swaps=0) + _increasing_rows("ETHUSDT", swaps=3)
    got = _abs_ic_and_ir(_group_facts(rows))
    assert got is not None
    assert got[0] == pytest.approx(abs(ic_stats("x", rows).ic_mean), abs=1e-6)


def test_permutation_null_is_deterministic_for_a_seed():
    facts = _noisy_facts()
    assert permutation_null(facts, rounds=20, seed=7) == permutation_null(facts, rounds=20, seed=7)
    assert permutation_null(facts, rounds=20, seed=7) != permutation_null(facts, rounds=20, seed=8)


def test_permutation_null_on_independent_data_is_near_zero():
    null = permutation_null(_noisy_facts(), rounds=100, seed=7)
    assert null.samples > 0
    assert null.ic_p95 < 0.15


def test_permutation_null_quantiles_are_ordered():
    null = permutation_null(_noisy_facts(), rounds=50, seed=7)
    assert null.ic_p95 <= null.ic_p99
    assert null.ir_p95 <= null.ir_p99


def test_suggest_thresholds_rejects_unknown_level():
    null = NullQuantiles(rounds=200, samples=1000, ic_p95=0.01, ic_p99=0.02, ir_p95=0.1, ir_p99=0.2)
    default = suggest_thresholds(null)
    assert default.min_ic_abs == 0.01
    assert default.min_ir == 0.1
    with pytest.raises(ValueError):
        suggest_thresholds(null, level="p90")


def test_suggest_thresholds_refuses_too_few_samples():
    # 有效分组 < 2 时四条分位数全是 0.0; 直接采用等于把准入门槛清零
    empty = NullQuantiles(rounds=200, samples=0, ic_p95=0.0, ic_p99=0.0, ir_p95=0.0, ir_p99=0.0)
    with pytest.raises(ValueError, match="分位数不可信"):
        suggest_thresholds(empty)
    thin = NullQuantiles(
        rounds=1, samples=MIN_NULL_SAMPLES - 1, ic_p95=0.01, ic_p99=0.02, ir_p95=0.1, ir_p99=0.2
    )
    with pytest.raises(ValueError, match="分位数不可信"):
        suggest_thresholds(thin)


def test_suggest_thresholds_accepts_enough_samples():
    ok = NullQuantiles(
        rounds=200, samples=MIN_NULL_SAMPLES, ic_p95=0.11, ic_p99=0.19, ir_p95=1.4, ir_p99=2.1
    )
    assert suggest_thresholds(ok).min_ic_abs == 0.11


def test_quantile_interpolates():
    assert _quantile([0.0, 1.0], 0.5) == 0.5
    assert _quantile([], 0.9) == 0.0
    assert _quantile([2.0], 0.9) == 2.0


def _ranked_rows(symbol: str, values: list[float]) -> list[BarFact]:
    """factor 按给定序列, forward 用单调递增序列(与 factor 值本身无关)."""
    return [
        _fact(symbol, BASE_TS + i * DAY_MS, float(value), float(i))
        for i, value in enumerate(values)
    ]


def test_correlation_matrix_is_symmetric_with_unit_diagonal():
    facts = {
        "a": _ranked_rows("BTCUSDT", [float(i) for i in range(40)]),
        "b": _ranked_rows("BTCUSDT", [float(40 - i) for i in range(40)]),
        "c": _ranked_rows("BTCUSDT", [float((i * 7) % 40) for i in range(40)]),
    }
    corr = factor_correlation_matrix(facts)
    # 只出现有序对(名字升序) + 对角线, 不会同时有 (a, b) 与 (b, a)
    assert set(corr) == {
        ("a", "a"),
        ("b", "b"),
        ("c", "c"),
        ("a", "b"),
        ("a", "c"),
        ("b", "c"),
    }
    assert corr[("a", "a")] == 1.0
    assert corr[("b", "b")] == 1.0
    assert corr[("c", "c")] == 1.0
    assert corr[("a", "b")] == pytest.approx(-1.0)
    assert corr[("a", "c")] is not None
    assert corr[("b", "c")] is not None
    assert ("b", "a") not in corr

    def lookup(left: str, right: str) -> float | None:
        return corr[(left, right)] if (left, right) in corr else corr[(right, left)]

    assert lookup("b", "a") == lookup("a", "b")
    assert lookup("c", "a") == lookup("a", "c")
    assert lookup("c", "b") == lookup("b", "c")


def test_correlation_matrix_marks_thin_overlap_as_none():
    thin = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(5)]
    wide_a = _ranked_rows("ETHUSDT", [float(i) for i in range(40)])
    wide_b = _ranked_rows("ETHUSDT", [float(i * 3) for i in range(40)])
    corr = factor_correlation_matrix(
        {"thin_a": thin, "thin_b": thin, "wide_a": wide_a, "wide_b": wide_b}
    )
    assert corr[("thin_a", "thin_b")] is None  # 公共观测 5 < min_common=30
    assert corr[("wide_a", "wide_b")] is not None


def test_select_representatives_drops_identical_twins():
    rows = _ranked_rows("BTCUSDT", [float(i) for i in range(40)])
    corr = factor_correlation_matrix({"twin_a": rows, "twin_b": rows})
    assert corr[("twin_a", "twin_b")] == pytest.approx(1.0)
    report = select_representatives(corr, ic_by_name={"twin_a": 0.05, "twin_b": 0.09})
    assert isinstance(report, RepresentativeReport)
    assert report.kept == ("twin_b",)  # |IC| 大的留下
    assert report.redundant == (("twin_a", "twin_b"),)


def test_select_representatives_keeps_uncorrelated_factors():
    corr = {
        ("a", "a"): 1.0,
        ("b", "b"): 1.0,
        ("c", "c"): 1.0,
        ("a", "b"): 0.1,
        ("a", "c"): -0.2,
        ("b", "c"): None,  # 公共观测不足 -> 视为不相关, 直接保留
    }
    report = select_representatives(corr, ic_by_name={"a": 0.05, "b": 0.04, "c": 0.03})
    assert report.kept == ("a", "b", "c")
    assert report.redundant == ()


def test_select_representatives_is_deterministic():
    corr = {("a", "a"): 1.0, ("b", "b"): 1.0, ("a", "b"): 0.9}
    report = select_representatives(corr, ic_by_name={"a": 0.05, "b": 0.05})
    # |IC| 相同 -> 按名字升序, a 先保留, b 因 |corr| 0.9 >= 0.7 判为冗余
    assert report.kept == ("a",)
    assert report.redundant == (("b", "a"),)
    # 字典传入顺序不影响结论
    assert select_representatives(corr, ic_by_name={"b": 0.05, "a": 0.05}) == report


def _family_facts(n_factors: int, *, seed: int, size: int = 60) -> dict[str, list[BarFact]]:
    """n_factors 个互不相同的因子, 共享同一批 bar 的前向收益(与真实数据语义一致).

    真实场景里同一根 bar 的 forward_return 与用哪个因子评估无关; 若各因子在同一根
    bar 上给出不同的收益, 族级构造(共享一份打乱收益)会失真。
    """
    rng = random.Random(seed)
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    forward = {(symbol, i): rng.uniform(-0.01, 0.01) for symbol in symbols for i in range(size)}
    facts: dict[str, list[BarFact]] = {}
    for k in range(n_factors):
        rows: list[BarFact] = []
        for symbol in symbols:
            values = [rng.uniform(-1.0, 1.0) for _ in range(size)]
            rows.extend(
                BarFact(
                    symbol=symbol,
                    ts_ms=BASE_TS + i * DAY_MS,
                    factor=values[i],
                    forward_return=forward[(symbol, i)],
                )
                for i in range(size)
            )
        facts[f"f{k}"] = rows
    return facts


def test_family_wise_null_is_at_least_single_factor_null():
    facts = _family_facts(10, seed=23)
    single = permutation_null(facts, rounds=200, seed=7)
    family = permutation_null(facts, rounds=200, seed=7, family_wise=True)
    # 族级每轮只产出一条样本(当轮 max), 单因子模式产出 因子 x 轮次
    assert family.samples == 200
    assert single.samples == 10 * 200
    # max over N >= 单个 —— 这是族级构造正确性的判据
    assert family.ic_p95 >= single.ic_p95
    assert family.ir_p95 >= single.ir_p95


def test_family_wise_null_is_deterministic_for_a_seed():
    facts = _noisy_facts()
    same = permutation_null(facts, rounds=30, seed=11, family_wise=True)
    assert same == permutation_null(facts, rounds=30, seed=11, family_wise=True)
    assert same != permutation_null(facts, rounds=30, seed=12, family_wise=True)


def test_permutation_null_default_stays_single_factor():
    facts = _noisy_facts()
    default = permutation_null(facts, rounds=20, seed=7)
    explicit = permutation_null(facts, rounds=20, seed=7, family_wise=False)
    assert default == explicit
    assert default.samples == 4 * 20  # 4 个因子 x 20 轮
    assert permutation_null(facts, rounds=20, seed=7, family_wise=True).samples == 20
