"""Math tests for the calibration report (Task A5).

Realization is win for net>0 and loss otherwise (Q3: net==0 counts as loss).
Brier compares conf/100 against the 0/1 realization; bucket boundaries are
floor(conf / 5) * 5. The suggested threshold is the lowest 5-pt boundary
whose bucket win rate is at least its boundary value (calibrated edge),
None when nothing qualifies.
"""
from __future__ import annotations

import pytest

from pa_agent.feedback.calibration import (
    brier_score,
    bucket_table,
    conf_bucket,
    suggest_threshold,
)


def test_conf_bucket_floors_to_five():
    assert conf_bucket(59) == 55
    assert conf_bucket(60) == 60
    assert conf_bucket(64) == 60
    assert conf_bucket(100) == 100


def test_brier_score_math():
    pairs = [(60, 1), (40, 0), (80, 0)]  # p: .6 .4 .8 ; y: 1 0 0
    expected = ((0.6 - 1) ** 2 + (0.4 - 0) ** 2 + (0.8 - 0) ** 2) / 3
    assert brier_score(pairs) == pytest.approx(expected)
    assert brier_score([]) is None


def test_bucket_table_counts_and_rates():
    pairs = [
        (58, 1), (59, 0),              # bucket 55: 1/2
        (62, 1), (64, 1), (60, 0),     # bucket 60: 2/3
        (71, 0),                        # bucket 70: 0/1
    ]
    table = bucket_table(pairs)
    assert table[55]["n"] == 2 and table[55]["wins"] == 1
    assert table[55]["win_rate"] == pytest.approx(0.5)
    assert table[60]["win_rate"] == pytest.approx(2 / 3)
    assert table[70]["win_rate"] == 0.0
    assert table[60]["avg_conf"] == pytest.approx(62.0)


def test_suggest_threshold_rule():
    pairs = [
        (52, 0), (58, 0),                 # bucket 50 wr 0.0
        (62, 1), (64, 1), (60, 1),        # bucket 60 wr 1.0 >= 0.6
        (72, 0), (73, 1), (70, 0), (71, 0),  # bucket 70 wr 0.25 < 0.7
    ]
    table = bucket_table(pairs)
    assert suggest_threshold(table, min_n=2) == 60
    assert suggest_threshold(bucket_table([(50, 1), (51, 1)]), min_n=2) == 50
    # nothing qualifies -> None
    assert suggest_threshold(bucket_table([(60, 0), (61, 0)]), min_n=2) is None
