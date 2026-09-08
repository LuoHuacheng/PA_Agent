"""Unit tests for the base-rate prompt injector (Task A4)."""
from __future__ import annotations

from unittest.mock import patch

from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.config.settings import FeedbackSettings
from pa_agent.feedback.base_rate_injector import (
    load_stats_block,
    render_base_rate_block,
)
from pa_agent.feedback.rule_stats import build_group_stats


def _fb(**overrides) -> FeedbackSettings:
    return FeedbackSettings(**{"enabled": True, "min_samples": 1, **overrides})


def _stats_rows():
    """win/loss rows across two strategy files, one cycle pair."""
    return [
        {"strategy_files": ["a.txt"], "cycle_position": "trading_range",
         "diag_direction": "bullish", "conf": 60, "outcome": "win", "win_r": 1.0},
        {"strategy_files": ["a.txt"], "cycle_position": "trading_range",
         "diag_direction": "bullish", "conf": 55, "outcome": "loss", "win_r": -1.0},
        {"strategy_files": ["a.txt", "b.txt"], "cycle_position": "trading_range",
         "diag_direction": "bullish", "conf": 60, "outcome": "open", "win_r": None},
        {"strategy_files": ["b.txt"], "cycle_position": "normal_channel",
         "diag_direction": "bearish", "conf": 70, "outcome": "win", "win_r": 2.0},
    ]


def test_render_base_rate_block_formats_and_orders_by_n():
    groups = build_group_stats(_stats_rows(), group_by=("strategy_file",), min_samples=1)
    block = render_base_rate_block(groups, max_lines=6, days=30)
    assert block.startswith("## 历史胜率先验")
    assert "a.txt" in block and "b.txt" in block
    # a.txt (n=2) sorts before b.txt (n=1); 50% win at 0.0R mean
    a_line = next(line for line in block.splitlines() if "a.txt" in line)
    assert "胜率 50%" in a_line
    assert "均盈亏" in a_line and "均置信" in a_line


def test_render_skips_insufficient_and_empty():
    assert render_base_rate_block({}, max_lines=6, days=30) == ""
    groups = build_group_stats(_stats_rows(), group_by=("strategy_file",), min_samples=10)
    assert render_base_rate_block(groups, max_lines=6, days=30) == ""


def test_render_respects_max_lines_cap():
    groups = build_group_stats(
        [{"strategy_files": [f"f{i}.txt"], "outcome": "win", "win_r": 1.0,
          "conf": 60} for i in range(5)],
        group_by=("strategy_file",), min_samples=1,
    )
    block = render_base_rate_block(groups, max_lines=2, days=30)
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(lines) == 2


def test_load_stats_block_disabled_or_missing_file(tmp_path):
    assert load_stats_block(FeedbackSettings(enabled=False)) == ""
    enabled = _fb()
    assert load_stats_block(enabled, outcomes_path=tmp_path / "nope.csv") == ""


def _write_outcomes(path) -> None:
    header = (
        "uid,symbol,timeframe,direction,order_type,outcome,conf,win_r,"
        "strategy_files,cycle_position,diag_direction\n"
    )
    rows = [
        'u1,BTCUSDT,15m,做多,市价单,win,60,1.0,"[""a.txt""]",trading_range,bullish',
        'u2,BTCUSDT,15m,做多,市价单,loss,55,-1.0,"[""a.txt""]",trading_range,bullish',
        'u3,BTCUSDT,15m,做多,市价单,win,70,2.0,"[""b.txt""]",normal_channel,bearish',
    ]
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")


def test_load_stats_block_reads_outcomes_csv(tmp_path):
    fp = tmp_path / "outcomes.csv"
    _write_outcomes(fp)
    block = load_stats_block(_fb(), outcomes_path=fp)
    assert "a.txt" in block and "b.txt" in block
    assert "胜率 50%" in block  # a.txt: 1 win / 2 closed
    assert "近30天" in block or "近 30" in block


def test_assembler_appends_stats_only_when_enabled(tmp_path, monkeypatch):
    for name in ["二元决策.txt", "上涨通道分析识别.txt", "上涨通道交易策略.txt",
                 "提示词大纲_人设与思维方式.txt", "逐棒分析检查单.txt", "文件16-K线信号识别.txt",
                 "文件17-止损和止盈与仓位管理.txt"]:
        (tmp_path / name).write_text(f"[CONTENT OF {name}]", encoding="utf-8")
    assembler = PromptAssembler(prompt_dir=tmp_path)
    from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

    def _frame():
        bars = tuple(
            KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                     open=2600.0 + i, high=2610.0 + i, low=2590.0 + i,
                     close=2605.0 + i, volume=1000.0, closed=(i != 0))
            for i in range(5)
        )
        ind = IndicatorBundle(ema20=tuple(2600.0 + i for i in range(5)),
                              atr14=tuple(5.0 for _ in range(5)))
        return KlineFrame(symbol="XAUUSD", timeframe="1h", bars=bars,
                          indicators=ind, snapshot_ts_local_ms=1_700_000_000_000)

    stage1 = {"cycle_position": "normal_channel", "direction": "bullish",
              "gate_result": "proceed"}
    fake_settings = type("S", (), {})()
    import pa_agent.config.settings as settings_mod
    with patch.object(settings_mod, "load_settings", return_value=fake_settings):
        # disabled: nothing appended
        fake_settings.feedback = FeedbackSettings(enabled=False)
        user_off = assembler.build_stage2(_frame(), stage1, [], [])[1]["content"]
        assert "历史胜率先验" not in user_off
        # enabled: stats block appended before the final reminder
        fake_settings.feedback = _fb()
        with patch("pa_agent.feedback.base_rate_injector.load_stats_block",
                   return_value="MARKER-STATS"):
            user_on = assembler.build_stage2(_frame(), stage1, [], [])[1]["content"]
    assert "MARKER-STATS" in user_on
    assert user_on.find("MARKER-STATS") < user_on.find("最后一步")
