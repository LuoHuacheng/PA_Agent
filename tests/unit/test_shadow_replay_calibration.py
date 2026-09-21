"""模拟器校准报告: 偏差必须按 R 口径拆分, 否则仓位差异会冒充模型差异."""

from __future__ import annotations

import pytest

from tools.shadow_replay import (
    MIN_CALIB_GROUP_N,
    CalibrationSample,
    calibration_group,
    calibration_report,
)


def sample(
    *,
    real_net=0.0,
    real_risk=4.0,
    sim_net=0.0,
    sim_risk=4.0,
    sim_filled=True,
    close_reason="stop",
    symbol="BTCUSDT",
    timeframe="15m",
):
    """造一笔配对样本, 默认两边都亏 0 且仓位口径一致."""
    return CalibrationSample(
        symbol=symbol,
        timeframe=timeframe,
        close_reason=close_reason,
        sim_reason="stop" if sim_filled else "unfilled",
        sim_filled=sim_filled,
        real_net=real_net,
        real_risk=real_risk,
        sim_net=sim_net,
        sim_risk=sim_risk,
    )


def test_empty_samples_gives_empty_report():
    report = calibration_report([])
    assert report.groups == []
    assert report.total == 0
    assert report.filled == 0
    assert report.min_n == MIN_CALIB_GROUP_N


def test_groups_split_by_sim_fill():
    samples = [sample(sim_filled=True) for _ in range(12)]
    samples += [sample(sim_filled=False) for _ in range(12)]
    report = calibration_report(samples)
    assert [g.label for g in report.groups][:3] == [
        "全部",
        "模拟判定成交",
        "模拟判定未成交",
    ]
    assert report.total == 24
    assert report.filled == 12


def test_r_units_normalise_position_size():
    big = sample(real_net=-60.0, real_risk=40.0, sim_net=-4.0, sim_risk=4.0)
    assert big.real_r == -1.5
    assert big.sim_r == -1.0
    assert big.sim_r - big.real_r == pytest.approx(0.5)


def test_usd_diff_is_misleading_where_r_diff_is_not():
    group = calibration_group("组", [sample(real_net=-60.0, real_risk=40.0, sim_net=-4.0)])
    assert group.u_diff == 56.0
    assert group.r_diff == pytest.approx(0.5)


def test_unfilled_bucket_exposes_dropped_losers():
    samples = [sample(sim_filled=True, real_net=4.0, sim_net=4.0) for _ in range(12)]
    samples += [sample(sim_filled=False, real_net=-2.0) for _ in range(12)]
    report = calibration_report(samples)
    unfilled = next(g for g in report.groups if g.label == "模拟判定未成交")
    assert unfilled.real_r == pytest.approx(-0.5)
    assert unfilled.sim_r == 0.0
    assert unfilled.r_diff == pytest.approx(0.5)


def test_small_buckets_are_reported_as_omitted():
    samples = [sample(close_reason="stop", symbol="AAA") for _ in range(12)]
    samples += [sample(close_reason="tp", symbol="BBB") for _ in range(3)]
    report = calibration_report(samples)
    assert not any(g.label.startswith("平仓:tp") for g in report.groups)
    assert not any(g.label == "品种:BBB" for g in report.groups)
    assert report.omitted == 2


def test_min_n_one_keeps_every_bucket():
    samples = [
        sample(close_reason="stop", symbol="AAA"),
        sample(close_reason="tp", symbol="BBB"),
    ]
    report = calibration_report(samples, min_n=1)
    assert report.omitted == 0
    assert any(g.label == "平仓:tp" for g in report.groups)


def test_empty_group_raises():
    with pytest.raises(ValueError):
        calibration_group("空", [])
