"""传输层重试: 网关抖动不该报废整轮分析 (2026-09-22)。

背景: 16:30 三连 + 17:00 BTC 四轮分析以
"peer closed connection without sending complete message body" 直接失败,
原因是 ``_stream_chat_resilient`` 单次尝试。这里锁住重试的四条边界:
重试成功、耗尽后抛出、非传输错误不重试、已 stream 过不重试(防重复输出)。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken


def _orchestrator(client: object, *, max_retries: int = 2, backoff: float = 0.0):
    settings = SimpleNamespace(
        provider=SimpleNamespace(
            transport_retry_max=max_retries,
            transport_retry_backoff_seconds=backoff,
        )
    )
    return TwoStageOrchestrator(
        client=client,
        assembler=MagicMock(),
        router=MagicMock(),
        validator=MagicMock(),
        pending_writer=MagicMock(),
        exp_reader=MagicMock(),
        settings=settings,
    )


def _call(orch: TwoStageOrchestrator, on_retry=None, content_cb=None):
    return orch._stream_chat_resilient(
        [{"role": "user", "content": "hi"}],
        on_reasoning_token=None,
        on_content_token=content_cb,
        cancel_token=CancelToken(),
        thinking=False,
        reasoning_effort="low",
        stage_label="Stage 1",
        on_retry=on_retry,
    )


def test_transport_error_is_retried_then_succeeds() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = [httpx.ReadError("peer closed"), "reply"]
    retries: list[int] = []

    assert _call(_orchestrator(client), on_retry=lambda: retries.append(1)) == "reply"
    assert client.stream_chat.call_count == 2
    assert retries == [1]


def test_transport_error_gives_up_after_max_retries() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = httpx.ReadError("peer closed")

    with pytest.raises(httpx.ReadError):
        _call(_orchestrator(client, max_retries=2))
    # 首次 + 2 次重试 = 3
    assert client.stream_chat.call_count == 3


def test_retry_disabled_keeps_single_attempt() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = httpx.ReadError("peer closed")

    with pytest.raises(httpx.ReadError):
        _call(_orchestrator(client, max_retries=0))
    assert client.stream_chat.call_count == 1


def test_non_transport_error_is_not_retried() -> None:
    client = MagicMock()
    client.stream_chat.side_effect = ValueError("bad request")

    with pytest.raises(ValueError):
        _call(_orchestrator(client))
    assert client.stream_chat.call_count == 1


def test_partially_streamed_reply_is_not_replayed() -> None:
    """已向 UI 吐过 token 的调用不能重放, 否则同一段内容会输出两遍。"""
    client = MagicMock()

    def _stream(_messages, *, on_content_token=None, **_kwargs):
        if on_content_token is not None:
            on_content_token("partial")
        raise httpx.ReadError("peer closed mid-stream")

    client.stream_chat.side_effect = _stream
    chunks: list[str] = []

    with pytest.raises(httpx.ReadError):
        _call(_orchestrator(client), content_cb=chunks.append)
    assert client.stream_chat.call_count == 1
    assert chunks == ["partial"]


def test_retry_policy_reads_settings_defaults_when_missing() -> None:
    orch = _orchestrator(MagicMock())
    orch._settings = SimpleNamespace(provider=SimpleNamespace())
    assert orch._transport_retry_policy() == (2, 5.0)
    orch._settings = None
    assert orch._transport_retry_policy() == (2, 5.0)
