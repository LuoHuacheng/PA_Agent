"""E2E smoke test — happy path: two-stage analysis produces a trading decision.

Task 19.1
"""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.router import route_strategy_files
from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from tests.fixtures.ai_payloads import VALID_STAGE1, VALID_STAGE2_ORDER
from tests.fixtures.kline_bars import make_trending_bars
from tests.fixtures.validators import schema_test_validator


def _make_reply(content_dict: dict) -> MagicMock:
    reply = MagicMock()
    reply.content = json.dumps(content_dict)
    reply.raw = {"content": reply.content}
    reply.usage = MagicMock()
    reply.usage.prompt_tokens = 100
    reply.usage.completion_tokens = 50
    reply.usage.cached_prompt_tokens = 0
    reply.usage.total_tokens = 150
    return reply


def _make_ctx(tmp_path, stage2_response=None):
    """Build a minimal AppContext with all required components."""
    if stage2_response is None:
        stage2_response = VALID_STAGE2_ORDER

    mock_client = MagicMock()
    mock_client.stream_chat.side_effect = [
        _make_reply(VALID_STAGE1),
        _make_reply(stage2_response),
    ]

    mock_assembler = MagicMock()
    mock_assembler.build_stage1.return_value = [{"role": "system", "content": "s1"}]
    mock_assembler.build_stage2.return_value = [{"role": "system", "content": "s2"}]

    pending_writer = MagicMock()

    ctx = AppContext()
    # settings 不能留空: MainWindow 的启动检查 (_on_startup_api_key_check) 与
    # _submit_block_reason() 都要读它; 空值会让「提交分析」被 API Key 门槛直接
    # 拦掉, 用例拿到的是永远空白的 DecisionPanel.
    ctx.settings = Settings()
    ctx.settings.provider.api_key = "sk-e2e-placeholder"
    ctx.client = mock_client
    ctx.assembler = mock_assembler
    ctx.router = route_strategy_files
    ctx.validator = schema_test_validator()
    ctx.pending_writer = pending_writer
    ctx.exp_reader = MagicMock()
    ctx.exp_reader.read_top5.return_value = []

    return ctx, pending_writer


@pytest.mark.e2e
def test_happy_path_shows_trading_decision(qtbot, tmp_path):
    """Full two-stage analysis completes and DecisionPanel shows a trade."""
    from pa_agent.gui.main_window import MainWindow

    # 必须是真上涨序列: 平坦序列会被门控读成中性震荡, 阶段一 direction 被归一化成
    # neutral, LLM 的多单随即被降级成「不下单」, 这个用例就永远测不到下单分支。
    # 同时价位要跟行情自洽, 否则交易者方程会把订单判掉。
    bars = make_trending_bars(31, with_forming=True)
    newest_high = max(bar.high for bar in bars if bar.closed)
    entry = newest_high + 1.0
    stage2 = copy.deepcopy(VALID_STAGE2_ORDER)
    stage2["decision"]["entry_price"] = entry
    stage2["decision"]["stop_loss_price"] = entry - 20.0
    stage2["decision"]["take_profit_price"] = entry + 40.0
    stage2["decision"]["take_profit_price_2"] = entry + 60.0

    ctx, pending_writer = _make_ctx(tmp_path, stage2)

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()

    # 前置检查要求 >= 20 根已收盘 K 线 (decision_nodes.bar_count_lt_20), 而窗口会
    # 按 analysis_bar_count 把帧截成 N 根已收盘 —— 两边都要给足, 否则分析在
    # preflight 就返回 insufficient_data, 永远走不到 save_full.
    window._ctx.settings.general.analysis_bar_count = 30
    window._last_frame_ready_bars = bars

    window._on_submit_analysis()

    # Poll until the analysis is no longer in progress (worker done).
    # Using waitUntil avoids the race where the worker finishes before
    # waitSignal is set up (mock data is very fast).
    qtbot.waitUntil(
        lambda: not window._analysis_in_progress,
        timeout=10_000,
    )

    # DecisionPanel should show a trading decision (not 不下单)
    conclusion_text = window._decision_panel._conclusion_label.text()
    assert "不下单" not in conclusion_text, f"Expected a trading decision, got: {conclusion_text!r}"
    assert conclusion_text != "—", "DecisionPanel still shows default '—', expected a decision"

    # PendingWriter.save_full should have been called
    pending_writer.save_full.assert_called_once()
