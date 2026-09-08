"""Integration tests for §6.3/§9.0 prefills in stage-2 (Task B2)."""
from __future__ import annotations

from pa_agent.ai.coherence_checks import validate_signal_quality_vs_prefill
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame


def _frame(*, atr: float = 2.0) -> KlineFrame:
    # newest first; K1 is a wide bull bar, older bars are calm
    specs = [
        (104.0, 99.5, 103.0),   # K1 high/low/close (open 100)
        (101.0, 99.0, 99.5),    # K2
        (100.5, 98.5, 99.0),
        (100.0, 98.0, 98.8),
        (100.2, 98.2, 99.0),
        (99.5, 97.5, 98.2),
        (99.0, 97.0, 98.0),
    ]
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                 open=100.0 if i == 0 else 99.0,
                 high=h, low=lo, close=c, volume=1000.0, closed=True)
        for i, (h, lo, c) in enumerate(specs)
    )
    ind = IndicatorBundle(ema20=tuple(99.0 for _ in bars),
                          atr14=tuple(atr for _ in bars))
    return KlineFrame(symbol="X", timeframe="1h", bars=bars, indicators=ind,
                      snapshot_ts_local_ms=1)


def _stage2(quality: str, reason: str = "", trace_reason: str = "") -> dict:
    return {
        "bar_analysis": {"signal_bar": {"quality": quality, "reason": reason}},
        "decision": {"reasoning": "", "order_type": "不下单"},
        "decision_trace": (
            [{"node_id": "9.0", "question": "q", "answer": "是", "reason": trace_reason}]
            if trace_reason
            else []
        ),
    }


def test_downgrade_free_and_invalid_to_weak_free():
    frame = _frame()
    # program reads strong on this K1; model downgrades -> no error
    assert validate_signal_quality_vs_prefill(_stage2("weak"), frame) == []
    assert validate_signal_quality_vs_prefill(_stage2("invalid"), frame) == []


def _doji_frame() -> KlineFrame:
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                 open=100.0, high=101.0, low=99.0, close=100.1,
                 volume=1000.0, closed=True)
        for i in range(7)
    )
    return KlineFrame(symbol="X", timeframe="1h", bars=bars,
                      indicators=IndicatorBundle(ema20=(100.0,) * 7,
                                                 atr14=(1.0,) * 7),
                      snapshot_ts_local_ms=1)


def test_upgrade_requires_bar_citation():
    frame = _doji_frame()  # program prefill = weak on a doji K1
    errors = validate_signal_quality_vs_prefill(_stage2("strong"), frame)
    assert len(errors) == 1
    assert "without any K-line citation" in errors[0]
    ok = validate_signal_quality_vs_prefill(
        _stage2("strong", reason="K1 强势多头趋势棒且突破前高"), frame
    )
    assert ok == []


def test_weak_frame_upgrade_medium_requires_citation():
    # a doji K1 makes the program prefill weak
    frame = _doji_frame()
    errors = validate_signal_quality_vs_prefill(_stage2("medium"), frame)
    assert errors, "medium upgrade over weak prefill must be cited"
    # citation anywhere in trace reasons also passes
    assert validate_signal_quality_vs_prefill(
        _stage2("medium", trace_reason="K2-K1 连续放量阳线突破"), frame
    ) == []


def test_stage2_prompt_includes_prefill_block(tmp_path):
    for name in ["二元决策.txt", "上涨通道分析识别.txt", "上涨通道交易策略.txt",
                 "提示词大纲_人设与思维方式.txt", "逐棒分析检查单.txt",
                 "文件16-K线信号识别.txt", "文件17-止损和止盈与仓位管理.txt"]:
        (tmp_path / name).write_text(f"[CONTENT OF {name}]", encoding="utf-8")
    asm = PromptAssembler(prompt_dir=tmp_path)
    stage1 = {"cycle_position": "trading_range", "direction": "neutral",
              "gate_result": "proceed"}
    user = asm.build_stage2(_frame(), stage1, [], [])[1]["content"]
    assert "## 程序节点预判" in user
    assert "§6.3 程序预判" in user
    assert "§9.0 K1 信号棒质量预判" in user
