"""K-line table trim knob for the stage-1 prompt (Phase C option A)."""
from __future__ import annotations

import re

from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.config.settings import PromptSettings, Settings
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

_STAGE1_FILES = [
    "提示词大纲_人设与思维方式.txt", "市场诊断框架.txt", "二元决策.txt",
    "二元决策_阶段一闸门.txt", "文件16-K线信号识别.txt", "逐棒分析检查单.txt",
]


def _frame(n: int = 60) -> KlineFrame:
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                 open=100.0 + i * 0.1, high=101.0 + i * 0.1,
                 low=99.0 + i * 0.1, close=100.5 + i * 0.1,
                 volume=1000.0, closed=True)
        for i in range(n)
    )
    ind = IndicatorBundle(ema20=tuple(100.0 + i * 0.1 for i in range(n)),
                          atr14=tuple(1.0 for _ in range(n)))
    return KlineFrame(symbol="X", timeframe="1h", bars=bars, indicators=ind,
                      snapshot_ts_local_ms=1_700_000_000_000)


def _assembler(tmp_path, limit: int | None) -> PromptAssembler:
    for name in _STAGE1_FILES:
        (tmp_path / name).write_text(f"[CONTENT OF {name}]", encoding="utf-8")
    if limit is None:
        return PromptAssembler(prompt_dir=tmp_path)
    settings = PromptSettings(stage1_kline_rows_limit=limit)
    return PromptAssembler(prompt_dir=tmp_path, prompt_settings=settings)


def _row_count(user: str) -> int:
    # kline table rows start with a numeric seq and carry a timestamp after the
    # first pipe; geometry rows do not
    rows = re.findall(r"(?:^|\n)\d+ +\|\s*\d{4}-\d{2}-\d{2}", user)
    return len(rows)


def test_prompt_settings_default_and_example():
    assert PromptSettings().stage1_kline_rows_limit == 0
    import json
    from pathlib import Path

    raw = json.loads(Path("config/settings.example.json").read_text(encoding="utf-8"))
    assert raw["prompt"]["stage1_kline_rows_limit"] == 0
    assert Settings.model_validate(raw).prompt.stage1_kline_rows_limit == 0


def test_stage1_full_table_without_knob(tmp_path):
    user = _assembler(tmp_path, None).build_stage1(_frame())[1]["content"]
    assert _row_count(user) == 60
    assert "更早 K 线概览" not in user


def test_stage1_limited_table_with_rollup(tmp_path):
    user = _assembler(tmp_path, 40).build_stage1(_frame())[1]["content"]
    assert _row_count(user) == 40
    assert "K41-K50" in user and "K51-K60" in user  # rollup chunks
    assert "K40" in user  # the newest kept row still present


def test_rollup_block_empty_when_limit_full(tmp_path):
    asm = _assembler(tmp_path, 0)
    frame = _frame()
    assert asm._render_kline_rollup_block(frame, 0) == ""
    assert asm._render_kline_rollup_block(frame, len(frame.bars)) == ""
