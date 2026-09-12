"""AI 客户端 seam: 消费方依赖的传输接口 + 传输层错误/能力判定。

历史上 two_stage 内嵌 openai/httpx/winerror 错误分类、prompt_assembler 反向
import deepseek_client 判断能力 — 厂商知识越过 seam 上漏。本 module 把
interface(Protocol)与传输侧判定收拢到一处; DeepSeekClient 是唯一实现
(OpenAI 兼容网关), 测试可注入内存 fake 实现同一 Protocol。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pa_agent.ai.deepseek_client import AIReply
    from pa_agent.config.settings import AIProviderSettings


@runtime_checkable
class AIClient(Protocol):
    """LLM 传输接口(结构化 subtype: DeepSeekClient 及其测试替身)。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        context_window: int | None = None,
        cancel_token: Any = None,
        timeout_s: float = 600.0,
    ) -> "AIReply": ...

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: Any = None,
        timeout_s: float = 600.0,
    ) -> "AIReply": ...


def supports_kv_prefix_chain(settings: "AIProviderSettings | None") -> bool:
    """Whether Stage 2 may chain after Stage 1 messages for DeepSeek KV prefix cache.

    Only DeepSeek-family endpoints benefit from the prefix-cache chain; all
    other OpenAI-compatible gateways answer standalone.
    """
    if settings is None:
        return True
    base_url = (getattr(settings, "base_url", "") or "").lower()
    model = (getattr(settings, "model", "") or "").lower()
    return "deepseek.com" in base_url or "deepseek" in model


def is_transport_error(exc: Exception) -> bool:
    """True if *exc* is a network/timeout error (SDK, httpx, or OS reset)."""
    from pa_agent.ai.deepseek_client import CancelledError

    if isinstance(exc, CancelledError):
        return False

    try:
        import openai  # type: ignore[import]

        if isinstance(
            exc,
            (
                openai.APITimeoutError,
                openai.APIConnectionError,
                openai.APIStatusError,
            ),
        ):
            return True
    except ImportError:
        pass

    try:
        import httpx  # type: ignore[import]

        if isinstance(
            exc,
            (
                httpx.ReadError,
                httpx.ConnectError,
                httpx.TimeoutException,
                httpx.RemoteProtocolError,
            ),
        ):
            return True
    except ImportError:
        pass

    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, TimeoutError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (
        10054,  # WSAECONNRESET — remote host closed connection
        10053,  # WSAECONNABORTED
        10060,  # WSAETIMEDOUT
    ):
        return True

    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_transport_error(cause)
    return False
