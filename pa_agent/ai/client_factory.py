"""Construct the correct AI client for the configured provider route."""

from __future__ import annotations

import logging
from pa_agent.ai.ai_client import AIClient
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.config.settings import AIProviderSettings


def create_ai_client(
    settings: AIProviderSettings,
    logger_: logging.Logger | None = None,
) -> AIClient:
    """Return the OpenAI-compatible DeepSeekClient for the configured provider.

    Every AI request goes through the same OpenAI-compatible client; the
    endpoint, model and API key come exclusively from settings.json.
    """
    log = logger_ or logging.getLogger(__name__)
    log.info(
        "AI client route: OpenAI-compatible (model=%s base_url=%s)",
        settings.model,
        settings.base_url or "(empty)",
    )
    return DeepSeekClient(settings=settings, logger_=log)
