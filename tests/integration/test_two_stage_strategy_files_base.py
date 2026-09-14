"""strategy_files_used 口径: 必须含 stage2 常驻 BASE 文件(供 shadow 分桶观察).

动机: 文件29/30(移植策略)走 BASE 常驻注入, 若 strategy_files_used 只记路由结果,
outcomes.csv 的 strategy_files 分桶将永远看不到它们, 48h shadow 无法按桶观察。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from pa_agent.ai.prompt_assembler import STAGE2_BASE_PROMPT_TXT_FILES
from pa_agent.ai.router import route_strategy_files
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken
from tests.fixtures.validators import schema_test_validator

from .conftest import VALID_STAGE1, VALID_STAGE2, make_reply


def test_strategy_files_used_includes_base_prompt_files(
    frame, pending_writer, assembler, exp_reader
):
    client = MagicMock()
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        make_reply(VALID_STAGE2),
    ]
    orchestrator = TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=schema_test_validator(),
        pending_writer=pending_writer,
        exp_reader=exp_reader,
    )

    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=lambda e: None,
    )

    used = list(record.strategy_files_used)
    # 无重复
    assert len(used) == len(set(used))
    # 路由结果在前, 常驻 BASE 文件在后
    routed = route_strategy_files(record.stage1_diagnosis)
    assert used[: len(routed)] == routed
    for name in STAGE2_BASE_PROMPT_TXT_FILES:
        assert name in used, f"strategy_files_used 缺常驻 BASE 文件: {name}"
