"""账户污染诊断: 策略不可能产生的交易必须能被单独挑出来."""

from __future__ import annotations

import json

import pytest

from pa_agent.feedback.contamination import (
    contamination_summary,
    flag_reasons,
    leg_net,
    leg_notional,
)
from tools.account_contamination import analyzed_symbols, strategy_bounds


def leg(sym, qty=10.0, entry=100.0, realized=-1.0, fees=-0.05):
    return {"sym": sym, "qty": qty, "entry": entry, "realized": realized, "fees": fees}


def test_leg_notional_and_net_handle_missing_fields():
    assert leg_notional({}) == 0.0
    assert leg_notional({"qty": "2", "entry": "3"}) == pytest.approx(6.0)
    assert leg_net({}) == 0.0
    assert leg_net({"realized": -2.0, "fees": -0.5}) == pytest.approx(-2.5)


def test_flag_unanalyzed_symbol():
    assert flag_reasons(leg("HEMIUSDT"), analyzed={"BTCUSDT"}, ceiling=0.0) == (
        "unanalyzed_symbol",
    )
    assert flag_reasons(leg("BTCUSDT"), analyzed={"BTCUSDT"}, ceiling=0.0) == ()


def test_flag_oversized_notional():
    big = leg("BTCUSDT", qty=1000.0, entry=100.0)
    assert flag_reasons(big, analyzed=set(), ceiling=5000.0) == ("oversized_notional",)
    assert flag_reasons(leg("BTCUSDT", qty=50.0, entry=100.0), analyzed=set(), ceiling=5000.0) == ()


def test_flag_accepts_exact_ceiling():
    edge = leg("BTCUSDT", qty=50.0, entry=100.0)  # 5000 == ceiling
    assert flag_reasons(edge, analyzed=set(), ceiling=5000.0) == ()


def test_report_splits_and_totals():
    legs = [
        leg("BTCUSDT", qty=1.0, entry=100.0, realized=5.0, fees=-0.1),
        leg("HEMIUSDT", qty=1_779_986.0, entry=0.0165, realized=-1654.18, fees=-13.85),
    ]
    report = contamination_summary(legs, analyzed={"BTCUSDT"}, ceiling=5000.0, include_flagged=True)
    assert report["legs_total"] == 2
    assert report["clean_count"] == 1
    assert report["clean_net"] == pytest.approx(4.9)
    assert report["flagged_count"] == 1
    assert report["flagged_net"] == pytest.approx(-1668.03)
    assert report["total_net"] == pytest.approx(-1663.13)
    assert report["by_reason"]["unanalyzed_symbol"]["count"] == 1
    assert report["by_reason"]["oversized_notional"]["count"] == 1
    assert report["by_symbol"]["HEMIUSDT"]["net"] == pytest.approx(-1668.03)
    assert report["flagged"][0]["reasons"] == ("unanalyzed_symbol", "oversized_notional")


def test_report_without_criteria_flags_nothing():
    report = contamination_summary([leg("WHATEVERUSDT")], ceiling=0.0)
    assert report["flagged_count"] == 0
    assert report["clean_count"] == 1


def test_strategy_bounds_reads_config(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"binance_usdm_testnet": {"max_notional_usdt": 250.0, "leverage": 20}}),
        encoding="utf-8",
    )
    bounds = strategy_bounds(path)
    assert bounds["notional_ceiling"] == pytest.approx(5000.0)
    assert bounds["leverage"] == pytest.approx(20.0)


def test_strategy_bounds_missing_file_is_zero(tmp_path):
    bounds = strategy_bounds(tmp_path / "nope.json")
    assert bounds["notional_ceiling"] == 0.0
    assert bounds["max_notional_usdt"] == 0.0


def test_analyzed_symbols_parses_filenames(tmp_path):
    (tmp_path / "2026-09-01_12-00-00_BTCUSDT_15m.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-09-01_12-00-00_ethusdt_30m.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert analyzed_symbols(tmp_path) == {"BTCUSDT", "ETHUSDT"}


def test_analyzed_symbols_honours_days_cutoff(tmp_path):
    (tmp_path / "2020-01-01_00-00-00_BTCUSDT_15m.json").write_text("{}", encoding="utf-8")
    assert analyzed_symbols(tmp_path, days=1) == set()
