"""Tests for AI client factory (single OpenAI-compatible route)."""
from __future__ import annotations

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.config.settings import AIProviderSettings


def test_any_model_routes_to_generic_openai_client() -> None:
    settings = AIProviderSettings(
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, DeepSeekClient)


def test_third_party_gateway_routes_to_generic_openai_client() -> None:
    settings = AIProviderSettings(
        model="DeepSeek-V4-Flash-0731",
        base_url="https://api.example.com/v1",
        api_key="sk-test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, DeepSeekClient)
