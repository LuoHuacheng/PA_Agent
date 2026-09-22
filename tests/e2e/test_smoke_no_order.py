"""E2E smoke test — no-order path: stage2 returns 不下单.

Task 19.2
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.router import route_strategy_files
from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from tests.fixtures.ai_payloads import VALID_STAGE1, VALID_STAGE2_NO_ORDER
from tests.fixtures.kline_bars import make_newest_first_bars
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


def _make_ctx(tmp_path):
    """Build a minimal AppContext configured for a no-order response."""
    mock_client = MagicMock()
    mock_client.stream_chat.side_effect = [
        _make_reply(VALID_STAGE1),
        _make_reply(VALID_STAGE2_NO_ORDER),
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
def test_no_order_shows_no_trade_conclusion(qtbot, tmp_path):
    """When stage2 returns 不下单, DecisionPanel shows that conclusion."""
    from pa_agent.gui.main_window import MainWindow

    ctx, _pending_writer = _make_ctx(tmp_path)

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()

    # 前置检查要求 >= 20 根已收盘 K 线 (decision_nodes.bar_count_lt_20), 而窗口会
    # 按 analysis_bar_count 把帧截成 N 根已收盘 —— 两边都要给足, 否则分析在
    # preflight 就返回 insufficient_data, 永远走不到 save_full.
    window._ctx.settings.general.analysis_bar_count = 30
    window._last_frame_ready_bars = make_newest_first_bars(31, with_forming=True)
    window._on_submit_analysis()

    # Poll until the analysis is no longer in progress
    qtbot.waitUntil(
        lambda: not window._analysis_in_progress,
        timeout=10_000,
    )

    # DecisionPanel should show 不下单
    conclusion_text = window._decision_panel._conclusion_label.text()
    assert "不下单" in conclusion_text, f"Expected 不下单 conclusion, got: {conclusion_text!r}"

    # 决策叠加线 (entry/TP/SL) 一根都不该有。注意不能拿 chart.items() 里的
    # 全部 InfiniteLine 来断言: 支撑/阻力线 (chart._sr_items) 是照常画的,
    # 它们同样是 InfiniteLine。真正要查的是 OverlayLines 持有的那批。
    chart = window._chart_widget
    decision_lines = chart._overlay._items
    assert (
        decision_lines == []
    ), f"Expected no entry/TP/SL overlay lines for 不下单, found {len(decision_lines)}"
    assert chart._pending_decision is None
