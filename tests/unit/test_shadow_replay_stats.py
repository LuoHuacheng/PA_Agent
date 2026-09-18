"""自助法置信区间: 没有区间就不许下结论."""
from __future__ import annotations

from tools.shadow_replay import bootstrap_mean_ci


def test_all_positive_sample_has_positive_interval():
    lo, hi = bootstrap_mean_ci([5.0] * 30)
    assert lo > 0 and hi > 0


def test_mixed_sample_straddles_zero():
    lo, hi = bootstrap_mean_ci([10.0, -9.0] * 15)
    assert lo < 0 < hi


def test_seed_makes_it_deterministic():
    assert bootstrap_mean_ci([1.0, -2.0, 3.0]) == bootstrap_mean_ci([1.0, -2.0, 3.0])


def test_empty_input_is_zero_interval():
    assert bootstrap_mean_ci([]) == (0.0, 0.0)
