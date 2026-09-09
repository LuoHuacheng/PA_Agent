"""Unit tests for DeepSeek KV prefix-chain provider detection."""
from __future__ import annotations

from pa_agent.ai.deepseek_client import supports_kv_prefix_chain
from pa_agent.config.settings import AIProviderSettings


def test_prefix_chain_enabled_for_deepseek_native():
    settings = AIProviderSettings(
        base_url="https://api.deepseek.com",
        model="deepseek-reasoner",
        api_key="sk-test",
    )
    assert supports_kv_prefix_chain(settings) is True


def test_prefix_chain_enabled_for_deepseek_model_on_proxy():
    settings = AIProviderSettings(
        base_url="https://api.example.com/v1",
        model="deepseek-chat",
        api_key="sk-test",
    )
    assert supports_kv_prefix_chain(settings) is True


def test_prefix_chain_disabled_for_non_deepseek_gateway():
    settings = AIProviderSettings(
        base_url="https://gateway.example.com",
        model="claude-sonnet-4-6",
        api_key="sk-test",
    )
    assert supports_kv_prefix_chain(settings) is False


def test_prefix_chain_disabled_for_unknown_model_on_proxy():
    settings = AIProviderSettings(
        base_url="https://api.example.com/v1",
        model="gpt-5",
        api_key="sk-test",
    )
    assert supports_kv_prefix_chain(settings) is False
