"""Routing-consistency tests for program cycle candidates (Task B1)."""
from __future__ import annotations

from unittest.mock import patch

from pa_agent.ai.coherence_checks import check_cycle_candidate_route
from pa_agent.ai.cycle_candidates import CycleCandidate

_CLEAR = [
    CycleCandidate("trading_range", 64.0, "箱宽4.2xATR, 突破质量failed"),
    CycleCandidate("trending_tr", 58.0, "倾斜箱体"),
]


def _stage1(cycle: str = "trading_range", alt: str = "") -> dict:
    stage = {"gate_result": "proceed", "cycle_position": cycle}
    if alt:
        stage["alternative_cycle_position"] = alt
    return stage


def test_route_ok_when_cycle_in_candidates():
    with patch("pa_agent.ai.cycle_candidates.score_cycle", return_value=_CLEAR), patch(
        "pa_agent.ai.cycle_candidates.build_metrics", return_value=object()
    ):
        assert check_cycle_candidate_route(_stage1("trading_range"), object()) == []


def test_route_ok_when_alternative_matches():
    with patch("pa_agent.ai.cycle_candidates.score_cycle", return_value=_CLEAR), patch(
        "pa_agent.ai.cycle_candidates.build_metrics", return_value=object()
    ):
        assert check_cycle_candidate_route(
            _stage1("spike", alt="trending_tr"), object()
        ) == []


def test_route_error_when_outside_and_no_override():
    with patch("pa_agent.ai.cycle_candidates.score_cycle", return_value=_CLEAR), patch(
        "pa_agent.ai.cycle_candidates.build_metrics", return_value=object()
    ):
        errors = check_cycle_candidate_route(_stage1("extreme_tr"), object())
    assert len(errors) == 1
    assert "not in program cycle candidates" in errors[0]
    assert "trading_range" in errors[0]


def test_route_override_node_12_escapes():
    stage = _stage1("spike")
    stage["node_overrides"] = [
        {"node_id": "1.2", "answer": "是", "branch": "spike",
         "override_reason": "K1-K3 连续三根趋势棒带缺口走出区间, 程序箱宽统计被长区间污染"}
    ]
    with patch("pa_agent.ai.cycle_candidates.score_cycle", return_value=_CLEAR), patch(
        "pa_agent.ai.cycle_candidates.build_metrics", return_value=object()
    ):
        assert check_cycle_candidate_route(stage, object()) == []


def test_route_skipped_without_frame_or_soft_candidates():
    assert check_cycle_candidate_route(_stage1("spike"), None) == []
    with patch("pa_agent.ai.cycle_candidates.score_cycle", return_value=[]):
        assert check_cycle_candidate_route(_stage1("spike"), object()) == []


def test_route_skip_when_gate_wait():
    stage = {"gate_result": "wait", "cycle_position": "unknown"}
    with patch("pa_agent.ai.cycle_candidates.score_cycle", return_value=_CLEAR), patch(
        "pa_agent.ai.cycle_candidates.build_metrics", return_value=object()
    ):
        assert check_cycle_candidate_route(stage, object()) == []


def test_stage1_prompt_renders_candidate_block(tmp_path):
    from pa_agent.ai.prompt_assembler import PromptAssembler
    from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

    for name in ["提示词大纲_人设与思维方式.txt", "二元决策_阶段一闸门.txt", "市场诊断框架.txt",
                 "文件16-K线信号识别.txt", "逐棒分析检查单.txt", "文件17-止损和止盈与仓位管理.txt"]:
        (tmp_path / name).write_text(f"[CONTENT OF {name}]", encoding="utf-8")
    asm = PromptAssembler(prompt_dir=tmp_path)
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                 open=100.0, high=101.0, low=99.0, close=100.5,
                 volume=100.0, closed=True)
        for i in range(5)
    )
    frame = KlineFrame(symbol="X", timeframe="1h", bars=bars,
                       indicators=IndicatorBundle(ema20=(100.0,) * 5,
                                                  atr14=(1.0,) * 5),
                       snapshot_ts_local_ms=1)
    with patch("pa_agent.ai.cycle_candidates.build_metrics",
               return_value=object()), patch(
        "pa_agent.ai.cycle_candidates.score_cycle", return_value=_CLEAR
    ):
        user = asm.build_stage1(frame)[1]["content"]
    assert "## 程序周期候选集" in user
    assert "trading_range（置信 64" in user