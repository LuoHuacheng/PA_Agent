"""DeepSeek AI client (OpenAI-compatible API)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

from pa_agent.config.settings import AIProviderSettings
from pa_agent.util.mask_secret import mask_secret

try:
    from openai import OpenAI as _OpenAI  # type: ignore[import]
except ImportError as _exc:
    _OpenAI = None  # type: ignore[assignment,misc]
    _OPENAI_IMPORT_ERROR = _exc
else:
    _OPENAI_IMPORT_ERROR = None

logger = logging.getLogger(__name__)


@dataclass
class AIUsage:
    """Token usage from a single API call."""

    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of prompt tokens served from KV cache (0.0–1.0).

        DeepSeek 硬盘缓存命中率。值越高，费用越低。
        0.0 = 无缓存命中；1.0 = 全部命中缓存。
        """
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_prompt_tokens / self.prompt_tokens

    @property
    def cache_miss_tokens(self) -> int:
        """Prompt tokens that were NOT served from cache (billed at full rate)."""
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)


@dataclass
class AIReply:
    """Structured response from a single AI API call."""

    content: str
    reasoning_content: str
    raw: dict[str, Any]  # full raw response dict for debug tab
    usage: AIUsage
    request_id: str
    latency_ms: float


class CancelledError(Exception):
    """Raised when a cancel_token is set before or during an API call."""


def _is_deepseek_native(base_url: str) -> bool:
    return "deepseek.com" in (base_url or "").lower()


def _is_deepseek_model(model: str) -> bool:
    """True for DeepSeek model ids."""
    m = (model or "").lower()
    return "deepseek" in m


def supports_kv_prefix_chain(settings: AIProviderSettings | None) -> bool:
    """Whether Stage 2 may chain after Stage 1 messages for DeepSeek KV prefix cache.

    Only DeepSeek-family endpoints benefit from the prefix-cache chain; all
    other OpenAI-compatible gateways answer standalone.
    """
    if settings is None:
        return True
    return _is_deepseek_native(settings.base_url) or _is_deepseek_model(settings.model)


def _extract_cached_prompt_tokens(usage: Any) -> int:
    """Read KV-cache hit count from provider usage (DeepSeek or OpenAI-compat)."""
    if usage is None:
        return 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is not None:
        return int(hit or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0)
        if cached:
            return int(cached)
    return 0


def _effective_api_model(settings: AIProviderSettings) -> str:
    """Model id sent to the upstream API."""
    return settings.model


# Global completion cap shared by all OpenAI-compatible gateways; upstream may
# clamp below this value, and some proxies reject anything above it.
_GLOBAL_MAX_OUTPUT_TOKENS = 384_000


_EFFORT_TO_ADAPTIVE_OUTPUT: dict[str, str] = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
    "xhigh": "max",
}


def _adaptive_output_effort(reasoning_effort: str | None) -> str:
    key = (reasoning_effort or "medium").strip().lower()
    return _EFFORT_TO_ADAPTIVE_OUTPUT.get(key, "medium")


# Sent to OpenAI-compatible gateways; upstream may clamp below these values.
_PRACTICAL_UNLIMITED_MAX_TOKENS = _GLOBAL_MAX_OUTPUT_TOKENS

# Some gateways (e.g. www.cun.ai) run Cloudflare WAF rules that 403 the SDK's
# default `User-Agent: OpenAI/Python …` ("Your request was blocked."). Send a
# project UA instead; no known gateway validates this header.
_DEFAULT_HEADERS = {"User-Agent": "PA_Agent/1.0"}

# Short-reasoning warning heuristic: Stage 1/2 prompts explicitly tell the model
# to keep thinking concise ("思考请用简体中文并尽量简洁；可缩短思考") so a tiny
# reasoning_content alongside a full JSON content is expected, NOT a symptom of
# thinking being silently dropped. Only warn when BOTH reasoning and content come
# back short.
_SHORT_REASONING_CHARS = 80
_MEANINGFUL_CONTENT_CHARS = 200


def _thinking_enabled(extra_body: dict[str, Any], effort: str | None) -> bool:
    if extra_body:
        return extra_body.get("thinking", {}).get("type") in ("enabled", "adaptive")
    return effort is not None and effort != "none"


def _completion_max_tokens(
    settings: AIProviderSettings,
    *,
    extra_body: dict[str, Any],
    effort: str | None,
) -> int:
    """Total completion budget (thinking + content) for OpenAI-compatible APIs."""
    del settings, effort, extra_body
    return _PRACTICAL_UNLIMITED_MAX_TOKENS


def _resolve_thinking_params(
    settings: AIProviderSettings,
    *,
    thinking: bool | None,
    reasoning_effort: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Return (extra_body, reasoning_effort) for chat.completions.create.

    Only DeepSeek-native endpoints need thinking.type=adaptive plus
    output_config.effort.  Every other OpenAI-compatible gateway receives a
    plain top-level reasoning_effort, following settings without per-vendor
    special-casing.
    """
    _thinking = thinking if thinking is not None else settings.thinking
    _effort = reasoning_effort if reasoning_effort is not None else settings.reasoning_effort

    if _is_deepseek_native(settings.base_url) or _is_deepseek_model(settings.model):
        if _thinking:
            extra_body = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": _adaptive_output_effort(_effort)},
            }
            return extra_body, _effort or "medium"
        return {"thinking": {"type": "disabled"}}, None

    if not _thinking:
        return {}, None
    # Generic OpenAI-compatible gateway: pass reasoning_effort through only.
    return {}, _effort or "medium"




class DeepSeekClient:
    """Thin wrapper around the OpenAI-compatible DeepSeek API."""

    def __init__(self, settings: AIProviderSettings, logger_: logging.Logger | None = None) -> None:
        self._settings = settings
        self._log = logger_ or logger

    def update_provider(self, settings: AIProviderSettings) -> None:
        """Replace in-memory provider settings (e.g. after a settings reload)."""
        self._settings = settings

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        context_window: int | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
    ) -> AIReply:
        """Send *messages* to the DeepSeek API and return a structured reply.

        Raises CancelledError if cancel_token is set before the call.
        Never sends temperature/top_p/presence_penalty/frequency_penalty.
        """
        # Check cancellation before making the network call
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        extra_body, _effort = _resolve_thinking_params(
            self._settings, thinking=thinking, reasoning_effort=reasoning_effort
        )
        api_messages = messages
        _thinking_on = _thinking_enabled(extra_body, _effort)
        _max_tokens = _completion_max_tokens(self._settings, extra_body=extra_body, effort=_effort)

        masked_key = mask_secret(self._settings.api_key)
        self._log.debug(
            "DeepSeekClient.chat: model=%s thinking=%s effort=%s max_tokens=%s "
            "key=...%s msgs=%d",
            self._settings.model,
            _thinking_on,
            _effort,
            _max_tokens,
            masked_key[-4:] if len(masked_key) >= 4 else "****",
            len(api_messages),
        )

        if _OpenAI is None:
            raise RuntimeError("openai package is not installed") from _OPENAI_IMPORT_ERROR

        client = _OpenAI(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            default_headers=_DEFAULT_HEADERS,
        )

        t0 = time.monotonic()
        create_kwargs: dict[str, Any] = {
            "model": _effective_api_model(self._settings),
            "messages": api_messages,
            "timeout": timeout_s,
            "max_tokens": _max_tokens,
        }
        if extra_body:
            create_kwargs["extra_body"] = extra_body
        if _effort is not None:
            create_kwargs["reasoning_effort"] = _effort
        # When thinking mode is OFF, set temperature=0 for maximum instruction-following
        # fidelity and JSON format compliance.  Thinking mode is incompatible with
        # temperature (DeepSeek/Anthropic spec), so we only inject it when safe.
        if not _thinking_on:
            create_kwargs["temperature"] = 0
        try:
            response = client.chat.completions.create(
                **create_kwargs,
                # IMPORTANT: do NOT add temperature, top_p, presence_penalty,
                # frequency_penalty — they are incompatible with thinking mode.
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error("DeepSeekClient API error after %.0f ms: %s", latency_ms, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000

        msg = response.choices[0].message
        content = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", None) or ""

        # Build usage
        u = response.usage
        usage = AIUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0),
            cached_prompt_tokens=_extract_cached_prompt_tokens(u),
            completion_tokens=getattr(u, "completion_tokens", 0),
            total_tokens=getattr(u, "total_tokens", 0),
        )

        request_id = getattr(response, "id", "") or ""

        # Build raw dict for debug tab — mask API key if it somehow appears
        raw: dict[str, Any] = {
            "id": request_id,
            "model": getattr(response, "model", ""),
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.debug(
            "DeepSeekClient.chat done: latency=%.0f ms tokens=%d/%d",
            latency_ms,
            usage.prompt_tokens,
            usage.completion_tokens,
        )

        # Log KV-cache hit rate so operators can monitor savings.
        # DeepSeek硬盘缓存：prompt_cache_hit_tokens 是命中缓存的 token 数。
        if usage.prompt_tokens > 0:
            hit_rate = usage.cached_prompt_tokens / usage.prompt_tokens * 100
            self._log.info(
                "KV-cache: hit=%d miss=%d total_prompt=%d hit_rate=%.1f%%",
                usage.cached_prompt_tokens,
                usage.prompt_tokens - usage.cached_prompt_tokens,
                usage.prompt_tokens,
                hit_rate,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
    ) -> AIReply:
        """Stream *messages* to the DeepSeek API, calling callbacks per token.

        Follows the official DeepSeek streaming example exactly:
        - reasoning_content tokens arrive first (thinking phase)
        - content tokens arrive after (answer phase)
        - delta.reasoning_content is None (not empty string) when absent

        Parameters
        ----------
        on_reasoning_token:
            Called with each reasoning/thinking token chunk as it arrives.
        on_content_token:
            Called with each content token chunk as it arrives.

        Returns the same AIReply as chat() once the stream is complete.
        Raises CancelledError if cancel_token is set before or during the call.
        """
        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        extra_body, _effort = _resolve_thinking_params(
            self._settings, thinking=thinking, reasoning_effort=reasoning_effort
        )
        api_messages = messages
        _thinking_on = _thinking_enabled(extra_body, _effort)
        _max_tokens = _completion_max_tokens(self._settings, extra_body=extra_body, effort=_effort)

        self._log.info(
            "DeepSeekClient.stream_chat: model=%s thinking=%s reasoning_effort=%s "
            "max_tokens=%s msgs=%d",
            self._settings.model,
            _thinking_on,
            _effort,
            _max_tokens,
            len(api_messages),
        )

        if _OpenAI is None:
            raise RuntimeError("openai package is not installed") from _OPENAI_IMPORT_ERROR

        client = _OpenAI(
            base_url=self._settings.base_url,
            api_key=self._settings.api_key,
            default_headers=_DEFAULT_HEADERS,
        )

        t0 = time.monotonic()
        reasoning_content = ""
        content = ""
        request_id = ""
        model_name = ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0

        try:
            # Build kwargs with stream_options to get usage in the final chunk.
            # Some providers may not support it; if the create() call itself
            # rejects stream_options we retry without it.
            stream_kwargs: dict[str, Any] = {
                "model": _effective_api_model(self._settings),
                "messages": api_messages,
                "timeout": timeout_s,
                "max_tokens": _max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if extra_body:
                stream_kwargs["extra_body"] = extra_body
            if _effort is not None:
                stream_kwargs["reasoning_effort"] = _effort

            try:
                stream = client.chat.completions.create(**stream_kwargs)
            except Exception:
                # Retry without stream_options if provider rejects it
                self._log.debug("stream_options not supported; retrying without it")
                stream_kwargs.pop("stream_options", None)
                stream = client.chat.completions.create(**stream_kwargs)

            for chunk in stream:
                # Check cancellation on each chunk
                if cancel_token is not None and cancel_token.is_set():
                    raise CancelledError("Request cancelled during streaming")

                # Extract usage from the final chunk (stream_options)
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    u = chunk.usage
                    prompt_tokens = getattr(u, "prompt_tokens", 0) or prompt_tokens
                    completion_tokens = getattr(u, "completion_tokens", 0) or completion_tokens
                    total_tokens = getattr(u, "total_tokens", 0) or total_tokens
                    cached_tokens = _extract_cached_prompt_tokens(u) or cached_tokens

                if not getattr(chunk, "choices", None):
                    continue

                request_id = request_id or (getattr(chunk, "id", "") or "")
                model_name = model_name or (getattr(chunk, "model", "") or "")

                choice0 = chunk.choices[0]
                delta = getattr(choice0, "delta", None)
                if delta is None:
                    continue

                # Official pattern: reasoning_content is None when absent, not ""
                # reasoning_content arrives first (thinking phase), then content
                r = getattr(delta, "reasoning_content", None)
                if r:
                    reasoning_content += r
                    if on_reasoning_token is not None:
                        on_reasoning_token(r)
                elif delta.content:
                    content += delta.content
                    if on_content_token is not None:
                        on_content_token(delta.content)

        except CancelledError:
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error("DeepSeekClient stream error after %.0f ms: %s", latency_ms, exc)
            raise

        latency_ms = (time.monotonic() - t0) * 1000

        usage = AIUsage(
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        raw: dict[str, Any] = {
            "id": request_id,
            "model": model_name,
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.info(
            "DeepSeekClient.stream_chat done: latency=%.0f ms "
            "reasoning_chars=%d content_chars=%d deepseek_thinking=%s effort=%s",
            latency_ms,
            len(reasoning_content),
            len(content),
            _thinking_on,
            _effort,
        )

        # Log KV-cache hit rate for stream calls as well.
        if usage.prompt_tokens > 0:
            hit_rate = usage.cached_prompt_tokens / usage.prompt_tokens * 100
            self._log.info(
                "KV-cache: hit=%d miss=%d total_prompt=%d hit_rate=%.1f%%",
                usage.cached_prompt_tokens,
                usage.prompt_tokens - usage.cached_prompt_tokens,
                usage.prompt_tokens,
                hit_rate,
            )
        if not content.strip():
            self._log.warning(
                "API returned empty content (model=%s base_url=%s). "
                "Check 原始 tab Raw Response; verify the model ID matches the gateway.",
                self._settings.model,
                self._settings.base_url,
            )
        elif _thinking_on and len(reasoning_content) < _SHORT_REASONING_CHARS:
            if len(content.strip()) < _MEANINGFUL_CONTENT_CHARS:
                # Both reasoning AND content came back short although thinking was
                # requested — a genuine drop symptom (e.g. wrong param style, model
                # ID / token group mismatch), not prompt-driven brevity.
                self._log.warning(
                    "Thinking enabled but reasoning_content is very short (%d chars) "
                    "and content is also short (%d chars). "
                    "Check the model ID and reasoning_effort=%s.",
                    len(reasoning_content),
                    len(content.strip()),
                    _effort,
                )
            else:
                # Short reasoning alongside a full content body is expected when the
                # prompt instructs concise thinking; log at debug to keep the noise down.
                self._log.debug(
                    "Thinking on but reasoning_content short (%d chars) with full content (%d chars); "
                    "model likely followed the concise-thinking instruction.",
                    len(reasoning_content),
                    len(content.strip()),
                )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )
