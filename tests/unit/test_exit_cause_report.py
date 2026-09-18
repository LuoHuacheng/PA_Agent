"""Exit-causation attribution: excursions, noise scale, and verdicts."""
from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path

import pytest

# The tool is a script (run from the repo root with tools/ on sys.path), so the
# test mirrors that import path instead of importing it as a package.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from exit_cause_report import (  # noqa: E402
    BUCKETS,
    MINUTE_MS,
    analyse,
    bucket_of,
    close_source,
    excursion,
    exit_kind,
    noise_scale,
    post_exit,
    summarise,
    verdict,
)

T0 = 1_700_000_000_000


def bar(t: int, o: float, h: float, lo: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}


def times_of(bars: list[dict]) -> list[int]:
    return [b["time"] for b in bars]


def test_excursion_is_signed_against_the_position() -> None:
    # entry 100: dips to 99 (-1%) and peaks at 101 (+1%)
    bars = [bar(T0, 100, 100.5, 99.9, 100.2), bar(T0 + MINUTE_MS, 100.2, 101.0, 99.0, 100.0)]
    long = excursion(bars, 1, 100.0, T0, T0 + MINUTE_MS)
    assert long["mae_pct"] == pytest.approx(-1.0)
    assert long["mfe_pct"] == pytest.approx(1.0)
    assert long["t_mfe_min"] == 1.0
    short = excursion(bars, -1, 100.0, T0, T0 + MINUTE_MS)
    # the same 101 high is the adverse extreme for a short
    assert short["mae_pct"] == pytest.approx(-1.0)
    assert short["mfe_pct"] == pytest.approx(1.0)
    assert short["t_mae_min"] == 1.0


def test_excursion_ignores_bars_outside_the_position_life() -> None:
    bars = [bar(T0 - MINUTE_MS, 100, 120, 80, 100), bar(T0, 100, 100.1, 99.9, 100)]
    row = excursion(bars, 1, 100.0, T0, T0)
    # the wild pre-entry candle must not leak into the excursion
    assert row["mae_pct"] == pytest.approx(-0.1)
    assert row["mfe_pct"] == pytest.approx(0.1)


def test_noise_scale_is_the_median_pre_entry_candle_range() -> None:
    prior = [bar(T0 - (i + 1) * MINUTE_MS, 100, 100.25, 99.75, 100) for i in range(60)]
    assert noise_scale(prior, T0) == pytest.approx(0.5)
    assert noise_scale(prior[:5], T0) is None  # too little history to judge noise


def test_post_exit_separates_a_noise_stop_from_a_correct_one() -> None:
    # stopped-out short at 101: price keeps running for 10m, then returns to entry
    noise = [bar(T0 + i * MINUTE_MS, 101, 101.5, 100.9, 101.3) for i in range(1, 11)]
    noise += [bar(T0 + i * MINUTE_MS, 100.1, 100.2, 99.6, 99.8) for i in range(11, 21)]
    noise_row = post_exit(noise, -1, 100.0, 101.0, T0)
    assert noise_row["recover_min"] == 11.0
    assert verdict("stop", 1.0, noise_row).startswith("止损过紧")

    # price never comes back: the stop protected us
    away = [bar(T0 + i * MINUTE_MS, 101, 104.0, 100.5, 103.0) for i in range(1, 61)]
    away_row = post_exit(away, -1, 100.0, 101.0, T0)
    assert away_row["recover_min"] is None
    assert away_row["post_adverse_pct"] < 0
    assert verdict("stop", 1.0, away_row).startswith("止损正确")


def test_verdict_flags_a_missing_stop() -> None:
    assert verdict("no_stop", None, {}) == "缺止损"
    assert verdict("signal", 1.0, {}) == "策略/止盈平仓"


@pytest.mark.parametrize(
    ("close_px", "stop", "target", "expected"),
    [
        (101.0, 101.0, 95.0, "stop"),
        (95.0, 101.0, 95.0, "tp"),
        (98.0, 101.0, 95.0, "signal"),
        (98.0, None, None, "no_stop"),
    ],
)
def test_exit_kind_classifies_the_ending_price(close_px, stop, target, expected) -> None:
    pos = {"entry": 100.0, "close_px": close_px, "stop": stop, "target": target}
    assert exit_kind(pos) == expected


def test_analyse_marks_a_never_working_short_as_an_entry_problem() -> None:
    # short from 100, stop 101: price only ever rose, so it never showed profit
    bars = [bar(T0 - i * MINUTE_MS, 100, 100.05, 99.95, 100) for i in range(60, 0, -1)]
    bars += [bar(T0 + i * MINUTE_MS, 100 + i * 0.02, 100.1 + i * 0.02, 99.9, 100.1) for i in range(10)]
    pos = {
        "sym": "TESTUSDT",
        "side": -1,
        "entry": 100.0,
        "close_px": 101.0,
        "stop": 101.0,
        "target": 95.0,
        "opened_at": T0,
        "closed_at": T0 + 9 * MINUTE_MS,
        "realized": -5.0,
        "fees": -0.2,
        "conf": 56,
    }
    row = analyse(pos, bars, times_of(bars))
    assert row["dur_min"] == pytest.approx(9.0)
    assert row["stop_dist_pct"] == pytest.approx(1.0)
    assert row["exit_kind"] == "stop"
    assert row["net"] == pytest.approx(-5.2)
    assert row["mfe_pct"] < 0.25  # never went our way
    assert row["entry_quality"].startswith("入场即错")


@pytest.mark.parametrize(
    ("cid", "expected"),
    [
        ("pa-sl-abc", "pa-sl(交易所止损)"),
        ("pa-tp-abc", "pa-tp(交易所止盈)"),
        ("pa-rollback-abc", "pa-rollback(主动平仓)"),
        ("pa-entry-abc", "pa-entry(反手)"),
        ("x-cvBPrNm9", "无pa-cid(无法归属)"),
        ("", "订单不可查(超出allOrders)"),
    ],
)
def test_close_source_reads_the_last_exit_order_id(cid, expected) -> None:
    pos = {"exits": [{"time": 1, "cid": "pa-tp-stale"}, {"time": 2, "cid": cid}]}
    assert close_source(pos) == expected


def test_close_source_handles_no_exit_rows() -> None:
    assert close_source({}) == "unknown(无成交记录)"


def test_summarise_aggregates_by_verdict_and_bucket_boundaries() -> None:
    rows = [
        {"net": -1.0, "mae_pct": -0.5, "mfe_pct": 0.1, "exit_kind": "stop",
         "stop_vs_noise": 0.5, "entry_quality": "入场即错(几乎无浮盈)",
         "verdict": "止损过紧(1h内回到入场)", "close_source": "pa-sl(交易所止损)"},
        {"net": 2.0, "mae_pct": -0.2, "mfe_pct": 0.9, "exit_kind": "signal",
         "stop_vs_noise": 3.0, "entry_quality": "曾浮盈", "verdict": "策略/止盈平仓",
         "close_source": "无pa-cid(无法归属)"},
    ]
    s = summarise(rows, "T")
    assert s["n"] == 2
    assert s["winrate"] == pytest.approx(50.0)
    assert s["net"] == pytest.approx(1.0)
    assert s["stops_tight"] == 1
    assert s["never_worked"] == 1
    assert s["by_verdict"]["策略/止盈平仓"]["net"] == pytest.approx(2.0)
    assert s["by_source"]["pa-sl(交易所止损)"]["n"] == 1
    assert s["by_source"]["无pa-cid(无法归属)"]["net"] == pytest.approx(2.0)
    assert summarise([], "empty") == {}


def test_buckets_partition_every_duration() -> None:
    assert bucket_of(0) == "<30m"
    assert bucket_of(29.999) == "<30m"
    assert bucket_of(30) == "30-60m"
    assert bucket_of(179) == "1-3h"
    assert bucket_of(360) == "6-12h"
    assert bucket_of(10 ** 6) == ">24h"
    # adjacent, non-overlapping, and covering the whole line
    for (_prev, _prev_lo, prev_hi), (_next, next_lo, _next_hi) in pairwise(BUCKETS):
        assert prev_hi == next_lo
