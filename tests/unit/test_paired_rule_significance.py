"""配对显著性检验的纯函数回归: 判定规则已冻结, 不可放宽."""

from __future__ import annotations

import math

import pytest

from tools.paired_rule_significance import Z_ONE_SIDED_95, paired_stats, verdict


def test_empty_sample_is_degenerate():
    stats = paired_stats([])
    assert stats["n"] == 0
    assert stats["mean"] == 0.0
    assert verdict(stats)[0] is False


def test_single_sample_has_no_standard_error():
    stats = paired_stats([5.0])
    assert stats["n"] == 1
    assert math.isnan(stats["se"])
    assert verdict(stats) == (False, "样本不足")


def test_known_values_for_three_diffs():
    # [1,2,3]: mean=2, var=1, std=1, se=1/sqrt(3), mde=1.645*se
    stats = paired_stats([1.0, 2.0, 3.0])
    assert stats["mean"] == pytest.approx(2.0)
    assert stats["std"] == pytest.approx(1.0)
    assert stats["se"] == pytest.approx(1.0 / math.sqrt(3))
    assert stats["mde"] == pytest.approx(Z_ONE_SIDED_95 / math.sqrt(3))
    assert stats["mean"] > stats["mde"]
    assert verdict(stats)[0] is True


def test_noisy_positive_mean_is_not_adoptable():
    # 均值为正但没超过配对 MDE -> 不足以采纳
    stats = paired_stats([1.0, -1.0, 1.0])
    assert stats["mean"] > 0
    assert stats["mean"] < stats["mde"]
    ok, note = verdict(stats)
    assert ok is False
    assert note.startswith("未超配对 MDE")


def test_negative_mean_is_rejected_on_direction():
    stats = paired_stats([-3.0, -1.0, -2.0])
    assert verdict(stats) == (False, "方向为负")


def test_large_consistent_improvement_is_adoptable():
    stats = paired_stats([0.5] * 50)
    assert stats["std"] == pytest.approx(0.0)
    assert verdict(stats)[0] is True


def test_z_constant_is_one_sided_95():
    assert Z_ONE_SIDED_95 == 1.645
