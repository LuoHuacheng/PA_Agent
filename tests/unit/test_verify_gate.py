"""闸门生效验证脚本的判定逻辑."""
from __future__ import annotations

import json

from scripts.verify_gate import describe, load_records, violations_for


def test_short_entry_is_a_violation():
    assert violations_for("做空", "broad_channel", ("broad_channel",)) == ["做空单未被拦截"]


def test_long_in_allowlist_is_clean():
    assert violations_for("做多", "broad_channel", ("broad_channel", "trading_range")) == []


def test_cycle_outside_allowlist_is_a_violation():
    v = violations_for("做多", "trending_tr", ("broad_channel", "normal_channel"))
    assert len(v) == 1 and "trending_tr" in v[0]


def test_empty_allowlist_disables_cycle_check():
    assert violations_for("做多", "trending_tr", ()) == []


def test_missing_cycle_is_not_a_violation():
    assert violations_for("做多", None, ("broad_channel",)) == []


def test_describe_reads_nested_fields():
    doc = {
        "meta": {"symbol": "BTCUSDT"},
        "stage2_decision": {
            "decision": {"order_direction": "做多"},
            "diagnosis_summary": {"cycle_position": "normal_channel"},
            "execution": {"status": "submitted"},
        },
    }
    info = describe("records/pending/x.json", doc)
    assert info["symbol"] == "BTCUSDT"
    assert info["direction"] == "做多"
    assert info["cycle"] == "normal_channel"
    assert info["exec_status"] == "submitted"
    assert info["file"] == "x.json"


def test_load_records_filters_by_cutoff(tmp_path):
    old = {"meta": {"timestamp_local_ms": 1000}}
    new = {"meta": {"timestamp_local_ms": 5000}}
    (tmp_path / "a.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(new), encoding="utf-8")
    got = load_records(str(tmp_path / "*.json"), 2000)
    assert [p.rsplit("/", 1)[-1] for p, _ in got] == ["b.json"]


def test_load_records_skips_broken_files(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "nometa.json").write_text("{}", encoding="utf-8")
    assert load_records(str(tmp_path / "*.json"), 0) == []
