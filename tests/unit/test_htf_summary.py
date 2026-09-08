"""Unit tests for HTF programmatic summaries (Phase D, Task D1)."""
from __future__ import annotations

from pa_agent.ai.htf_summary import build_htf_context_text, summarize_htf
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame


def _frame(kind: str = "trend_up", n: int = 80) -> KlineFrame:
    if kind == "trend_up":
        bars = tuple(
            KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                     open=100.0 + i * 0.2, high=101.0 + i * 0.2,
                     low=99.0 + i * 0.2, close=100.5 + i * 0.2,
                     volume=1000.0, closed=True)
            for i in range(n)
        )
        ema = tuple(100.0 + i * 0.2 for i in range(n))
    elif kind == "range":
        vals = [100.0 + (10.0 * ((i * 7) % 11) - 5.0 * ((i * 3) % 7)) / 10.0 for i in range(n)]
        bars = tuple(
            KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                     open=vals[i], high=vals[i] + 0.6, low=vals[i] - 0.6,
                     close=vals[i] + 0.1, volume=1000.0, closed=True)
            for i in range(n)
        )
        ema = tuple(100.0 + (10.0 * ((i * 7) % 11)) / 10.0 * 0.2 for i in range(n))
    else:
        raise ValueError(kind)
    atr = tuple(1.0 for _ in range(n))
    return KlineFrame(symbol="X", timeframe="1h", bars=bars,
                      indicators=IndicatorBundle(ema20=ema, atr14=atr),
                      snapshot_ts_local_ms=1_700_000_000_000)


def test_summarize_htf_trend_frame():
    text = summarize_htf(_frame("trend_up"))
    assert text
    assert len(text) <= 400
    assert "EMA" in text or "均线" in text


def test_summarize_htf_range_frame():
    text = summarize_htf(_frame("range"))
    assert text
    assert len(text) <= 400


def test_summarize_htf_empty_frame():
    frame = KlineFrame(symbol="X", timeframe="1h", bars=(), indicators=None,
                       snapshot_ts_local_ms=0)
    assert summarize_htf(frame) == ""


def test_build_htf_context_text_joins_sections():
    parts = {"1h": summarize_htf(_frame("range")), "4h": summarize_htf(_frame("trend_up"))}
    text = build_htf_context_text(parts)
    assert text.startswith("## 更高时间框架(程序摘要)")
    assert "1h" in text and "4h" in text
    assert build_htf_context_text({}) == ""



def test_stage1_prompt_injects_htf_block_when_provided(tmp_path):
    from pa_agent.ai.prompt_assembler import PromptAssembler

    for name in ["提示词大纲_人设与思维方式.txt", "市场诊断框架.txt", "二元决策.txt",
                 "二元决策_阶段一闸门.txt", "文件16-K线信号识别.txt", "逐棒分析检查单.txt"]:
        (tmp_path / name).write_text(f"[CONTENT OF {name}]", encoding="utf-8")
    asm = PromptAssembler(prompt_dir=tmp_path)
    text = build_htf_context_text({"1h": summarize_htf(_frame("range"))})
    user_on = asm.build_stage1(_frame("range"), htf_block=text)[1]["content"]
    assert "## 更高时间框架(程序摘要)" in user_on
    assert "1h" in user_on
    user_off = asm.build_stage1(_frame("range"))[1]["content"]
    assert "更高时间框架" not in user_off


def test_htf_monitor_settings_defaults_and_example():
    from pa_agent.config.settings import Settings

    mon = Settings().monitoring
    assert mon.htf_context_enabled is False
    assert mon.htf_timeframes == ["1h", "4h"]
    import json
    from pathlib import Path

    raw = json.loads(Path("config/settings.example.json").read_text(encoding="utf-8"))
    assert raw["monitoring"]["htf_context_enabled"] is False
    assert raw["monitoring"]["htf_timeframes"] == ["1h", "4h"]

