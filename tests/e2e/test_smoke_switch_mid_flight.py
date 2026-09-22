"""E2E smoke test �?symbol switch mid-flight cancels the AI worker.

Task 19.3
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from pa_agent.ai.router import route_strategy_files
from pa_agent.app_context import AppContext
from pa_agent.config.settings import Settings
from tests.fixtures.ai_payloads import VALID_STAGE1, VALID_STAGE2_ORDER
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


def _make_ctx_slow_stage2(tmp_path):
    """Build a context where stage2 blocks until a cancel token is set."""
    # stage2 call blocks for up to 5 s, but respects the cancel token
    stage2_started = threading.Event()

    def slow_chat(messages, cancel_token=None, **kwargs):
        call_count = slow_chat._call_count
        slow_chat._call_count += 1

        if call_count == 0:
            # Stage 1 �?return immediately
            return _make_reply(VALID_STAGE1)
        else:
            # Stage 2 �?signal that we've started, then block until cancelled
            stage2_started.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if cancel_token is not None and cancel_token.is_set():
                    from pa_agent.ai.deepseek_client import CancelledError

                    raise CancelledError("cancelled by token")
                time.sleep(0.05)
            return _make_reply(VALID_STAGE2_ORDER)

    slow_chat._call_count = 0

    mock_client = MagicMock()
    mock_client.stream_chat.side_effect = slow_chat

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

    return ctx, pending_writer, stage2_started


@pytest.mark.e2e
def test_switch_mid_flight_cancels_worker(qtbot, tmp_path):
    """Switching symbol while stage2 is running cancels the worker."""
    from pa_agent.gui.main_window import MainWindow

    ctx, _pending_writer, stage2_started = _make_ctx_slow_stage2(tmp_path)

    window = MainWindow(ctx)
    qtbot.addWidget(window)
    window.show()

    # 前置检查要求 >= 20 根已收盘 K 线 (decision_nodes.bar_count_lt_20), 而窗口会
    # 按 analysis_bar_count 把帧截成 N 根已收盘 —— 两边都要给足, 否则分析在
    # preflight 就返回 insufficient_data, 永远走不到 save_full.
    window._ctx.settings.general.analysis_bar_count = 30
    window._last_frame_ready_bars = make_newest_first_bars(31, with_forming=True)

    window._on_submit_analysis()
    # 提交是异步的: 窗口会先起快照/预处理步骤, _AnalysisWorker 随后才挂到 window._worker.
    qtbot.waitUntil(lambda: window._worker is not None, timeout=5_000)
    worker = window._worker
    assert worker is not None, "Worker should have been created"

    # Wait until stage2 has started (so we know the worker is mid-flight)
    assert stage2_started.wait(timeout=5.0), "Stage 2 did not start within 5 s"

    # Trigger symbol switch mid-flight
    window._symbol_combo.setCurrentText("EURUSD")

    # Worker should be cancelled and finish within a reasonable time
    # (the slow_chat loop checks cancel_token every 50 ms)
    finished = worker.wait(6_000)  # 6 s timeout
    assert finished, "Worker did not finish after symbol switch"

    # 切品种后追问输入必须禁用。入口现在是 AISidebar 的实时面板 (_stream_panel),
    # 不是「tab 1」—— 页签已搬进 AISidebar 且顺序不同, MainWindow 上不再有 _tabs.
    panel = window._stream_panel
    assert panel is not None, "stream panel 缺失"
    assert not panel._input_edit.isEnabled(), "切品种后追问输入应禁用"
    assert not panel._send_btn.isEnabled(), "切品种后发送按钮应禁用"
