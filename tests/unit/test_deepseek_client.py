"""Unit tests for DeepSeekClient — generic OpenAI-compatible behaviour."""

from __future__ import annotations

import logging
import pytest
from unittest.mock import MagicMock, patch
from pa_agent.config.settings import AIProviderSettings
from pa_agent.ai.deepseek_client import (
    DeepSeekClient,
    AIReply,
    CancelledError,
    _completion_max_tokens,
    _is_deepseek_model,
)


def _make_settings(api_key: str = "sk-test-1234abcd") -> AIProviderSettings:
    s = AIProviderSettings()
    s.api_key = api_key
    return s


def _make_mock_response(content: str = "hello", reasoning: str = "thinking...") -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.reasoning_content = reasoning
    choice = MagicMock()
    choice.message = msg
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.total_tokens = 150
    usage.prompt_tokens_details = MagicMock()
    usage.prompt_tokens_details.cached_tokens = 20
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.id = "req-abc123"
    resp.model = "deepseek-v4-pro"
    return resp


def _make_stream(reasoning_chunks: list[str], content_chunks: list[str]) -> list[object]:
    """Build OpenAI-style stream chunks: reasoning, then content, then usage."""
    chunks = []
    for ridx, text in enumerate(reasoning_chunks):
        ch = MagicMock()
        ch.id = f"req-{ridx}"
        ch.model = "deepseek-v4-flash"
        ch.usage = None
        ch.choices = [MagicMock()]
        delta = MagicMock()
        delta.reasoning_content = text
        delta.content = None
        ch.choices[0].delta = delta
        chunks.append(ch)
    for cidx, text in enumerate(content_chunks):
        ch = MagicMock()
        ch.id = f"req-r{cidx}"
        ch.model = "deepseek-v4-flash"
        ch.usage = None
        ch.choices = [MagicMock()]
        delta = MagicMock()
        delta.reasoning_content = None
        delta.content = text
        ch.choices[0].delta = delta
        chunks.append(ch)
    done = MagicMock()
    done.choices = []
    done.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=MagicMock(cached_tokens=0),
    )
    chunks.append(done)
    return chunks


def _thinking_settings() -> AIProviderSettings:
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-flash"
    settings.thinking = True
    settings.reasoning_effort = "high"
    return settings


def _stream_reply_and_records(settings, chunks) -> tuple[AIReply, list]:
    client = DeepSeekClient(settings)
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = iter(chunks)
    records: list = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("pa_agent.ai.deepseek_client")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
            reply = client.stream_chat([{"role": "user", "content": "hi"}])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return reply, records


# ── chat: generic request shaping ────────────────────────────────────────────

def test_chat_does_not_send_forbidden_params():
    """chat() must never pass temperature/top_p/presence_penalty/frequency_penalty."""
    settings = _make_settings()
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        reply = client.chat([{"role": "user", "content": "hi"}])

    call_kwargs = mock_openai.return_value.chat.completions.create.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
    all_kwargs = {**(call_kwargs.args[0] if call_kwargs.args else {}), **kwargs}

    for forbidden in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        assert forbidden not in all_kwargs, f"Forbidden param '{forbidden}' was sent to API"


def test_chat_deepseek_native_uses_adaptive_thinking():
    """DeepSeek native endpoints use thinking.type=adaptive + output_config.effort."""
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"
    settings.thinking = True
    settings.reasoning_effort = "max"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    call_kwargs = mock_openai.return_value.chat.completions.create.call_args
    kwargs = call_kwargs.kwargs
    assert kwargs["extra_body"]["thinking"]["type"] == "adaptive"
    assert kwargs["extra_body"]["output_config"]["effort"] == "max"
    assert kwargs["reasoning_effort"] == "max"


def test_chat_deepseek_model_on_third_party_gateway_uses_adaptive_thinking():
    """DeepSeek-family model ids keep the adaptive protocol on any gateway."""
    settings = _make_settings()
    settings.base_url = "https://api.example-proxy.com/v1"
    settings.model = "deepseek-v4-flash"
    settings.thinking = True
    settings.reasoning_effort = "high"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"] == {"type": "adaptive"}
    assert kwargs["extra_body"]["output_config"] == {"effort": "high"}
    assert kwargs["reasoning_effort"] == "high"


def test_chat_deepseek_thinking_off_sends_disabled():
    """DeepSeek-family with thinking off sends thinking.type=disabled only."""
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-flash"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in kwargs


def test_chat_other_models_thinking_forwards_reasoning_effort_only():
    """Non-DeepSeek models on generic gateways receive no vendor extra_body."""
    settings = _make_settings()
    settings.base_url = "https://api.example-proxy.com/v1"
    settings.model = "claude-sonnet-4-6"
    settings.thinking = True
    settings.reasoning_effort = "high"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs
    assert kwargs["reasoning_effort"] == "high"


def test_chat_other_models_thinking_off_sends_nothing():
    settings = _make_settings()
    settings.base_url = "https://api.example-proxy.com/v1"
    settings.model = "claude-sonnet-4-6"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert "extra_body" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_completion_max_tokens_global_cap():
    """All gateways share the 384K completion cap (no per-vendor caps)."""
    for base_url, model in (
        ("https://api.deepseek.com", "deepseek-v4-pro"),
        ("https://claude-gateway.example.com/v1", "claude-sonnet-4-6"),
        ("https://api.example-proxy.com/v1", "some-model"),
    ):
        settings = _make_settings()
        settings.base_url = base_url
        settings.model = model
        assert _completion_max_tokens(settings, extra_body={}, effort="max") == 384_000


def test_chat_sends_max_tokens_when_thinking():
    settings = _make_settings()
    settings.base_url = "https://api.deepseek.com"
    settings.model = "deepseek-v4-pro"
    settings.thinking = True
    settings.reasoning_effort = "medium"
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat([{"role": "user", "content": "hi"}])

    kwargs = mock_openai.return_value.chat.completions.create.call_args.kwargs
    assert kwargs["max_tokens"] == 384_000


def test_system_message_stays_inline_for_generic_gateways():
    """System turns remain in messages for every OpenAI-compatible gateway."""
    settings = _make_settings()
    settings.base_url = "https://api.example-proxy.com/v1"
    settings.model = "claude-sonnet-4-6"
    settings.thinking = False
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        client.chat(messages)

    sent = mock_openai.return_value.chat.completions.create.call_args.kwargs["messages"]
    assert sent == messages


# ── stream ───────────────────────────────────────────────────────────────────

def test_stream_delivers_reasoning_and_content_tokens():
    settings = _make_settings()
    settings.base_url = "https://api.example-proxy.com/v1"
    settings.thinking = False
    client = DeepSeekClient(settings)

    chunks = _make_stream(reasoning_chunks=["think"], content_chunks=["answer"])
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = iter(chunks)

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        reply = client.stream_chat([{"role": "user", "content": "hi"}])

    assert reply.reasoning_content == "think"
    assert reply.content == "answer"


def test_stream_short_reasoning_with_full_content_does_not_warn() -> None:
    """短 reasoning + 长 content：prompt 明示思考尽量简洁，不应触发告警。"""
    settings = _thinking_settings()
    content_body = "{\"decision\":\"不下单\",\"reasoning\":\"分析内容\"}" * 40  # ~2000 chars
    reply, records = _stream_reply_and_records(
        settings, _make_stream(reasoning_chunks=["已权衡，输出 JSON。"], content_chunks=[content_body])
    )
    assert len(reply.content) > 200
    messages = [r.getMessage() for r in records]
    assert not any("reasoning_content is very short" in m for m in messages), messages


def test_stream_short_reasoning_and_short_content_warns() -> None:
    """reasoning 与 content 都短：疑似 thinking 失效，需告警。"""
    settings = _thinking_settings()
    reply, records = _stream_reply_and_records(
        settings,
        _make_stream(reasoning_chunks=["x"], content_chunks=["{}"]),
    )
    assert len(reply.content) < 200
    warning_msgs = [
        m
        for r in records
        if r.levelno == logging.WARNING
        for m in [r.getMessage()]
    ]
    assert any("reasoning_content is very short" in m for m in warning_msgs), warning_msgs


# ── lifecycle & safety ───────────────────────────────────────────────────────

def test_chat_cancel_token_raises():
    """If cancel_token is set, chat() raises CancelledError before calling API."""
    from pa_agent.util.threading import CancelToken

    settings = _make_settings()
    client = DeepSeekClient(settings)

    token = CancelToken()
    token.set()

    mock_openai = MagicMock()
    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        with pytest.raises(CancelledError):
            client.chat([{"role": "user", "content": "hi"}], cancel_token=token)

    # API must NOT have been called
    mock_openai.return_value.chat.completions.create.assert_not_called()


def test_chat_no_plaintext_key_in_logs(caplog):
    """API key must not appear in log output."""
    settings = _make_settings(api_key="sk-super-secret-9999")
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response()
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with caplog.at_level(logging.DEBUG, logger="pa_agent.ai.deepseek_client"):
        with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
            client.chat([{"role": "user", "content": "hi"}])

    for record in caplog.records:
        assert "sk-super-secret-9999" not in record.getMessage(), (
            f"Plaintext API key found in log: {record.getMessage()}"
        )


def test_chat_returns_aireply_fields():
    """chat() returns an AIReply with all expected fields populated."""
    settings = _make_settings()
    client = DeepSeekClient(settings)

    mock_resp = _make_mock_response(content="answer", reasoning="thought")
    mock_openai = MagicMock()
    mock_openai.return_value.chat.completions.create.return_value = mock_resp

    with patch("pa_agent.ai.deepseek_client._OpenAI", mock_openai):
        reply = client.chat([{"role": "user", "content": "hi"}])

    assert isinstance(reply, AIReply)
    assert reply.content == "answer"
    assert reply.reasoning_content == "thought"
    assert reply.usage.prompt_tokens == 100
    assert reply.usage.completion_tokens == 50
    assert reply.request_id == "req-abc123"
    assert reply.latency_ms >= 0


def test_deepseek_model_sniff():
    assert _is_deepseek_model("gpt-5") is False
    assert _is_deepseek_model("deepseek-v4-pro") is True
    assert _is_deepseek_model("claude-sonnet-4-6") is False
