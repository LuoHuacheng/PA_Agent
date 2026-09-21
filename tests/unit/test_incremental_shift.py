"""Tests for mechanical K-line reference shifting (incremental analysis)."""

from __future__ import annotations

from pa_agent.ai.incremental_shift import shift_kline_refs, shift_kline_refs_in_text


def test_shift_in_text_moves_k_numbers():
    assert shift_kline_refs_in_text("K1 破位, 回踩 K3-K5", 4) == "K5 破位, 回踩 K7-K9"


def test_shift_in_text_leaves_plain_k_alone():
    assert shift_kline_refs_in_text("K线没有数字", 4) == "K线没有数字"


def test_shift_in_text_noop_when_n_not_positive():
    assert shift_kline_refs_in_text("K1", 0) == "K1"
    assert shift_kline_refs_in_text("K1", -2) == "K1"


def test_shift_refs_shifts_nested_text_and_bar_indexes():
    prev = {
        "stage1": {
            "bar_range": "K3-K5",
            "bar_from": 3,
            "bar_to": 5,
            "reason": "K2 放量突破",
            "score": 7,
            "flag": True,
        }
    }
    out = shift_kline_refs(prev, 4)
    stage1 = out["stage1"]
    assert stage1["bar_range"] == "K7-K9"
    assert stage1["bar_from"] == 7
    assert stage1["bar_to"] == 9
    assert stage1["reason"] == "K6 放量突破"
    assert stage1["score"] == 7  # 非 K 序号的整数不动
    assert stage1["flag"] is True  # bool 不当整数平移


def test_shift_refs_skips_incremental_delta_subtree():
    prev = {
        "incremental_delta": {"new_closed_bars": ["K1"], "summary": "K1 新增"},
        "reason": "K1 延续",
    }
    out = shift_kline_refs(prev, 4)
    assert out["incremental_delta"] == {"new_closed_bars": ["K1"], "summary": "K1 新增"}
    assert out["reason"] == "K5 延续"


def test_shift_refs_keeps_lists_and_original_on_noop():
    src = {"key_signals": ["K1 缩量", "K2 假突破"]}
    assert shift_kline_refs(src, 3)["key_signals"] == ["K4 缩量", "K5 假突破"]
    assert shift_kline_refs(src, 0) is src
