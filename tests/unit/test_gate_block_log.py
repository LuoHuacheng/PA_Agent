"""门控拦截台账 + prompt 回灌 (2026-09-22)。

背景: BTCUSDT 13:01/13:32/14:01 连续三轮重推同一个被 short_block 硬拦的做空方案,
模型看不到拦截原因。这里锁住台账写入、窗口读取、摘要渲染与 prompt 注入的开关。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.config.settings import PromptSettings
from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.trading.gate_block_log import (
    read_gate_blocks,
    record_gate_block,
    split_gate_reason,
    summarize_gate_blocks,
)

_NOW = 1_790_000_000.0


def _record(now: float, **kwargs) -> None:
    record_gate_block(base_dir=kwargs.pop("base_dir"), now=now, **kwargs)


def test_split_gate_reason() -> None:
    assert split_gate_reason("rr_block: 计划盈亏比 1.38 < 1.5") == (
        "rr_block",
        "计划盈亏比 1.38 < 1.5",
    )
    assert split_gate_reason("short_block") == ("short_block", "")


def test_record_then_read_round_trip(tmp_path: Path) -> None:
    record_gate_block(
        symbol="BTCUSDT",
        timeframe="30m",
        direction="做空",
        gate_reasons=[
            "short_block: 45 天回放做空 22 笔胜率 32%",
            "rr_block: 计划盈亏比 1.38 < 1.5",
        ],
        entry_price=85879.25,
        base_dir=tmp_path,
        now=_NOW,
    )
    rows = read_gate_blocks(symbol="BTCUSDT", hours=4, base_dir=tmp_path, now=_NOW + 60)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "BTCUSDT"
    assert row["direction"] == "做空"
    assert row["gates"] == ["short_block", "rr_block"]
    assert row["details"]["short_block"] == "45 天回放做空 22 笔胜率 32%"
    assert row["entry_price"] == "85879.25"


def test_read_window_and_symbol_filter(tmp_path: Path) -> None:
    _record(
        _NOW - 6 * 3600,
        symbol="BTCUSDT",
        base_dir=tmp_path,
        gate_reasons=["short_block: 过老"],
    )
    _record(
        _NOW - 60,
        symbol="ETHUSDT",
        base_dir=tmp_path,
        gate_reasons=["session_block: 低流动性窗口"],
    )
    assert read_gate_blocks(symbol="BTCUSDT", hours=4, base_dir=tmp_path, now=_NOW) == []
    eth = read_gate_blocks(symbol="ETHUSDT", hours=4, base_dir=tmp_path, now=_NOW)
    assert len(eth) == 1


def test_read_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert read_gate_blocks(symbol="BTCUSDT", base_dir=tmp_path / "nope") == []


def test_record_without_reasons_writes_nothing(tmp_path: Path) -> None:
    assert record_gate_block(symbol="BTCUSDT", gate_reasons=(), base_dir=tmp_path) is None
    assert list(tmp_path.glob("*.jsonl")) == []


def test_record_survives_unwritable_dir(tmp_path: Path) -> None:
    """审计写失败只告警: 不能反过来影响下单流程。"""
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x", encoding="utf-8")
    assert (
        record_gate_block(
            symbol="BTCUSDT",
            gate_reasons=["short_block: 回放做空胜率 32%"],
            base_dir=blocked,
        )
        is None
    )


def test_summarize_groups_by_direction_and_gate(tmp_path: Path) -> None:
    for offset, reasons in (
        (3600, ["short_block: 45 天回放做空 22 笔胜率 32%", "rr_block: 计划盈亏比 1.38 < 1.5"]),
        (1800, ["short_block: 45 天回放做空 22 笔胜率 32%"]),
    ):
        _record(
            _NOW - offset,
            symbol="BTCUSDT",
            timeframe="30m",
            direction="做空",
            base_dir=tmp_path,
            gate_reasons=reasons,
        )
    rows = read_gate_blocks(symbol="BTCUSDT", hours=4, base_dir=tmp_path, now=_NOW)
    text = summarize_gate_blocks(rows, max_chars=400)
    assert "近期入口门控拦截" in text
    assert "做空 ×2" in text
    assert "short_block" in text
    assert "做空 ×1" in text
    assert "rr_block" in text
    assert "不要重复提交已被拦的同向方案" in text


def test_summarize_respects_char_budget(tmp_path: Path) -> None:
    _record(
        _NOW,
        symbol="BTCUSDT",
        direction="做空",
        base_dir=tmp_path,
        gate_reasons=["short_block: " + "很长" * 200],
    )
    rows = read_gate_blocks(symbol="BTCUSDT", hours=4, base_dir=tmp_path, now=_NOW)
    assert len(summarize_gate_blocks(rows, max_chars=120)) <= 120


def test_summarize_empty_is_empty_string() -> None:
    assert summarize_gate_blocks([]) == ""


# ── prompt 注入 ─────────────────────────────────────────────────────────────


def _assembler(tmp_path: Path, reader, *, prompt_settings=None) -> PromptAssembler:
    return PromptAssembler(
        tmp_path,
        prompt_settings=prompt_settings or PromptSettings(),
        gate_block_reader=reader,
    )


def test_render_gate_feedback_injects_recent_blocks(tmp_path: Path) -> None:
    _record(
        _NOW,
        symbol="BTCUSDT",
        timeframe="30m",
        direction="做空",
        base_dir=tmp_path,
        gate_reasons=["short_block: 45 天回放做空 22 笔胜率 32%"],
    )
    frame = SimpleNamespace(symbol="BTCUSDT", timeframe="30m")
    assembler = _assembler(
        tmp_path, lambda **kw: read_gate_blocks(**kw, base_dir=tmp_path, now=_NOW)
    )
    block = assembler._render_gate_feedback(frame)
    assert "short_block" in block
    assert "做空" in block


def test_render_gate_feedback_disabled_returns_empty(tmp_path: Path) -> None:
    frame = SimpleNamespace(symbol="BTCUSDT")
    assembler = _assembler(
        tmp_path,
        lambda **_kw: [{"gates": ["short_block"], "direction": "做空", "ts_ms": 1}],
        prompt_settings=PromptSettings(gate_feedback_enabled=False),
    )
    assert assembler._render_gate_feedback(frame) == ""


def test_render_gate_feedback_swallows_reader_errors(tmp_path: Path) -> None:
    def _boom(**_kw):
        raise RuntimeError("ledger on fire")

    frame = SimpleNamespace(symbol="BTCUSDT")
    assert _assembler(tmp_path, _boom)._render_gate_feedback(frame) == ""


def test_render_gate_feedback_empty_when_no_rows(tmp_path: Path) -> None:
    frame = SimpleNamespace(symbol="BTCUSDT")
    assert _assembler(tmp_path, read_gate_blocks)._render_gate_feedback(frame) == ""


def test_stage1_user_prompt_carries_gate_feedback(tmp_path: Path) -> None:
    """阶段一完整 prompt 末尾必须带上回灌块 (缺了等于 ② 白做)。"""
    rows = [
        {
            "ts_ms": int(_NOW * 1000),
            "symbol": "BTCUSDT",
            "direction": "做空",
            "gates": ["short_block"],
            "details": {"short_block": "45 天回放做空 22 笔胜率 32%"},
        }
    ]
    frame = _fake_frame()
    assembler = _assembler(tmp_path, lambda **_kw: rows)
    prompt = assembler._build_stage1_user_prompt(frame)
    assert "short_block" in prompt
    assert "不要重复提交已被拦的同向方案" in prompt


def _fake_frame(n: int = 45) -> KlineFrame:
    """最小 KlineFrame: 够阶段一 prompt 的各段渲染器跑完 (45 根, 含长程背景分支)。"""
    bars = tuple(
        KlineBar(
            seq=i + 1,
            ts_open=1_790_000_000_000 + (n - 1 - i) * 1_800_000,
            open=100.0 + (n - 1 - i),
            high=101.0 + (n - 1 - i),
            low=99.0 + (n - 1 - i),
            close=100.5 + (n - 1 - i),
            volume=10.0,
            closed=(i > 0),
        )
        for i in range(n)
    )
    return KlineFrame(
        symbol="BTCUSDT",
        timeframe="30m",
        bars=bars,
        indicators=IndicatorBundle(
            ema20=tuple(100.0 + (n - 1 - i) for i in range(n)),
            atr14=tuple([1.0] * n),
        ),
        snapshot_ts_local_ms=1_790_000_000_000,
    )
