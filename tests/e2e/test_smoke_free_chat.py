"""E2E smoke test �?free-chat session after two-stage analysis.

Task 19.4
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.router import route_strategy_files
from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from tests.fixtures.ai_payloads import VALID_STAGE1, VALID_STAGE2_ORDER
from tests.fixtures.kline_bars import make_newest_first_bars
from tests.fixtures.validators import schema_test_validator

CHAT_REPLY_CONTENT = "This is a follow-up AI response."


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


def _make_chat_reply(content: str) -> MagicMock:
    """Build a mock AIReply for a free-chat turn."""
    reply = MagicMock()
    reply.content = content
    reply.reasoning_content = ""
    reply.raw = {"content": content}
    reply.usage = MagicMock()
    reply.usage.prompt_tokens = 80
    reply.usage.completion_tokens = 30
    reply.usage.cached_prompt_tokens = 0
    reply.usage.total_tokens = 110
    return reply


def _make_ctx(tmp_path):
    """Build a minimal AppContext for the free-chat smoke test."""
    mock_client = MagicMock()
    mock_client.stream_chat.side_effect = [
        _make_reply(VALID_STAGE1),
        _make_reply(VALID_STAGE2_ORDER),
        _make_chat_reply(CHAT_REPLY_CONTENT),
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
def test_free_chat_after_analysis(qtbot, tmp_path):
    """After two-stage analysis, a FreeChatSession can send one turn."""
    from pa_agent.ai.session_ledger import SessionTokenLedger
    from pa_agent.gui.main_window import MainWindow
    from pa_agent.orchestrator.free_chat import FreeChatSession
    from pa_agent.util.threading import CancelToken

    ctx, pending_writer = _make_ctx(tmp_path)

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

    # Retrieve the completed record from the save_full call
    assert pending_writer.save_full.called, "save_full should have been called"
    completed_record = pending_writer.save_full.call_args[0][0]

    # Build a SessionTokenLedger for the free-chat session
    ledger = SessionTokenLedger()

    # Create a FreeChatSession wired to the window's client
    session = FreeChatSession(
        base_record=completed_record,
        client=ctx.client,
        assembler=ctx.assembler,
        pending_writer=ctx.pending_writer,
        ledger=ledger,
    )

    # Wire the session to the window
    window._free_chat_session = session

    # Send one message via the session directly (simulating what the UI does)
    cancel_token = CancelToken()
    session.send("What do you think about the entry?", cancel_token)

    # FreeChatSession should have completed one turn
    assert session._turn == 1, f"Expected 1 turn, got {session._turn}"
    assert len(session.history_full) == 2  # user + assistant

    # pending_writer.append_followup should have been called
    pending_writer.append_followup.assert_called_once()
    call_args = pending_writer.append_followup.call_args
    assert call_args[0][0], "append_followup 应带上记录 id"
    followup_turn_arg = call_args[0][1]
    assert followup_turn_arg.turn == 1
    assert followup_turn_arg.cancelled is False

    # Ledger should have been updated
    assert ledger.total_input > 0, "Ledger should have accumulated input tokens"
    assert ledger.total_output > 0, "Ledger should have accumulated output tokens"
