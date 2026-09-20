"""闸门生效验证脚本的判定逻辑."""
from __future__ import annotations

import json

from scripts.verify_gate import (
    describe,
    gate_reason_counts,
    is_entry_attempt,
    load_live_gates,
    load_records,
    plan_is_working,
    violations_for,
)


def test_short_entry_is_a_violation():
    assert violations_for("做空", "broad_channel", ("broad_channel",)) == ["做空单未被拦截"]


def test_blocked_plan_is_not_a_violation():
    """2026-09-20: 被正确拦下的做空 spike 单不是泄漏(exec=gate_blocked)。

    旧脚本不看 execution 标记, 对这两条报 VIOLATION 并 exit 1 —— 假阳性。
    """
    assert violations_for("做空", "spike", ("broad_channel",), status="gate_blocked") == []


def test_withheld_statuses_are_all_not_violations():
    for status in ("gate_blocked", "rejected", "skipped", "dry_run", "failed", "unknown"):
        assert violations_for("做空", "spike", ("broad_channel",), status=status) == []


def test_submitted_short_is_still_a_violation():
    assert violations_for("做空", "broad_channel", ("broad_channel",),
                          status="submitted") == ["做空单未被拦截"]


def test_pending_short_is_still_a_violation():
    assert violations_for("做空", "broad_channel", ("broad_channel",),
                          status="pending") == ["做空单未被拦截"]


def test_short_gate_off_disables_short_check():
    """线上 block_short_entry=false 时做空本就允许, 不该判违规。"""
    assert violations_for("做空", "broad_channel", ("broad_channel",),
                          status="pending", short_gate=False) == []


def test_plan_is_working_matches_execution_marker():
    """未标记 = 旧记录(按已提交处理), 与 execution_marker 口径一致。"""
    assert plan_is_working("") is True
    assert plan_is_working(None) is True
    assert plan_is_working("submitted") is True
    assert plan_is_working("pending") is True
    assert plan_is_working("gate_blocked") is False
    assert plan_is_working("dry_run") is False


def test_live_gates_read_config_not_snapshot(tmp_path):
    """白名单从配置读: 配置里有 trending_tr, 审计就不该再对它报违规。

    旧脚本 DEFAULT_ALLOW 写死三个值, 比 2026-09-20 00:51 的配置旧。
    """
    doc = {"binance_usdm_testnet": {
        "block_short_entry": True,
        "block_cycle_allowlist": ["broad_channel", "trending_tr"],
    }}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    allow, short_gate, source = load_live_gates(path)
    assert list(allow) == ["broad_channel", "trending_tr"]
    assert short_gate is True
    assert str(path) in source
    assert violations_for("做多", "trending_tr", allow, status="submitted") == []


def test_long_in_allowlist_is_clean():
    assert violations_for("做多", "broad_channel", ("broad_channel", "trading_range")) == []


def test_cycle_outside_allowlist_is_a_violation():
    v = violations_for("做多", "trending_tr", ("broad_channel", "normal_channel"))
    assert len(v) == 1 and "trending_tr" in v[0]


def test_empty_allowlist_disables_cycle_check():
    assert violations_for("做多", "trending_tr", ()) == []


def test_missing_cycle_is_not_a_violation():
    assert violations_for("做多", None, ("broad_channel",)) == []


def test_gate_reason_counts_groups_by_prefix():
    infos = [
        {"gate_reasons": ["rr_block: 计划盈亏比 1.37 < 1.5"]},
        {"gate_reasons": ["rr_block: 计划盈亏比 1.20 < 1.5",
                          "short_block: 45 天回放做空 22 笔胜率 32% 净/风险 -0.33"]},
        {"gate_reasons": []},
        {},
    ]
    assert gate_reason_counts(infos) == {"rr_block": 2, "short_block": 1}


def test_describe_surfaces_gate_reasons():
    doc = {"stage2_decision": {"execution": {"status": "gate_blocked",
                                             "gate_reasons": ["rr_block: x"]}}}
    assert describe("p.json", doc)["gate_reasons"] == ["rr_block: x"]


def test_wait_decision_is_not_an_entry_attempt():
    """2026-09-18 16:09 SOLUSDT: order_type=不下单, 不该被当成闸门泄漏。"""
    assert is_entry_attempt("不下单", None) is False


def test_missing_direction_is_not_an_entry_attempt():
    assert is_entry_attempt("限价单", None) is False


def test_real_order_is_an_entry_attempt():
    assert is_entry_attempt("限价单", "做多") is True


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
