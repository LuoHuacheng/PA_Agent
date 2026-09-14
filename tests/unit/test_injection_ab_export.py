"""tools/audit/injection_ab_export.py 的单元测试（VergeX 29/30 注入 A/B 导出）。"""
from __future__ import annotations

import json

from tools.audit.injection_ab_export import (
    MARKER_FILES,
    backfill_outcomes,
    extract_row,
    load_outcomes,
    summarize,
)


def _record(*, order_type="限价单", direction="做多", conf=55, files=None, exc=None, ts=1789358568483):
    dec = {
        "order_type": order_type,
        "order_direction": direction if order_type != "不下单" else None,
        "entry_price": 100.79 if order_type != "不下单" else None,
        "stop_loss_price": 100.61 if order_type != "不下单" else None,
        "take_profit_price": 101.20 if order_type != "不下单" else None,
        "trade_confidence": conf,
        "diagnosis_confidence": 55,
        "estimated_win_rate": 52,
    }
    return {
        "meta": {"timestamp_local_ms": ts, "symbol": "SOLUSDT", "timeframe": "30m"},
        "stage1_diagnosis": {
            "cycle_position": "trading_range", "direction": "neutral", "gate_result": "proceed"
        },
        "stage2_messages": [{"content": "x" * 100}],
        "stage2_decision": {
            "decision": dec,
            "diagnosis_summary": {"cycle_position": "trading_range", "direction": "neutral"},
            "bar_analysis": {"bar_type": "other", "always_in": "long"},
            "terminal": {"node_id": "11.1", "outcome": "trade"},
            "execution": {"status": "gate_blocked", "gate_reasons": ["neutral_diag_block: x"]},
        },
        "strategy_files_used": files if files is not None else list(MARKER_FILES),
        "exception": exc,
        "usage_total": {"total_tokens": 1234},
    }


def test_extract_row_marks_post_bucket_and_metrics() -> None:
    row = extract_row(_record(), "2026-09-14_12-09-48_SOLUSDT_30m.json")
    assert row["bucket"] == "post_injection"
    assert row["symbol"] == "SOLUSDT"
    assert row["order_type"] == "限价单"
    assert row["rr"] == 2.28  # (101.20-100.79)/(100.79-100.61)
    assert row["execution_status"] == "gate_blocked"
    assert row["gate_reasons"] == "neutral_diag_block: x"
    assert row["strategy_files_count"] == len(MARKER_FILES)
    assert row["stage2_prompt_chars"] == 100
    assert row["filled"] == ""  # 回填前为空


def test_extract_row_pre_bucket_without_marker_files() -> None:
    row = extract_row(_record(files=["逐棒分析检查单.txt"]), "2026-09-13_20-09-11_ETHUSDT_30m.json")
    assert row["bucket"] == "pre_injection"
    assert row["strategy_files_count"] == 1


def test_extract_row_survives_exception_only_record() -> None:
    rec = _record(exc={"type": "network_error", "stage": "stage2"})
    rec["stage2_decision"] = {}
    row = extract_row(rec, "2026-09-14_12-09-11_BNBUSDT_30m.json")
    assert row["exception_type"] == "network_error"
    assert row["exception_stage"] == "stage2"
    assert row["order_type"] == ""
    assert row["rr"] == "" or row["rr"] is None


def test_backfill_outcomes_joins_by_record_file(tmp_path) -> None:
    csv_path = tmp_path / "outcomes.csv"
    csv_path.write_text(
        "record_file,close_reason,outcome,win_r,net_usdt\n"
        "2026-09-14_12-09-48_SOLUSDT_30m.json,TP2,win,1.85,7.40\n",
        encoding="utf-8",
    )
    outcomes = load_outcomes(csv_path)
    rows = [extract_row(_record(), "2026-09-14_12-09-48_SOLUSDT_30m.json"),
            extract_row(_record(), "2026-09-14_13-09-48_SOLUSDT_30m.json")]
    backfill_outcomes(rows, outcomes)
    assert rows[0]["filled"] == "yes" and rows[0]["outcome"] == "win" and rows[0]["win_r"] == "1.85"
    assert rows[1]["filled"] == "" and rows[1]["outcome"] == ""


def test_summarize_skips_exception_records() -> None:
    good = extract_row(_record(), "a.json")
    bad = extract_row(_record(exc={"type": "network_error", "stage": "stage2"}), "b.json")
    stats = summarize([good, bad])
    post = stats["post_injection"]
    assert post["records"] == 2
    assert post["complete"] == 1
    assert post["exceptions"] == 1
    assert post["order_rate"] == 1.0
    assert stats["pre_injection"]["records"] == 0
