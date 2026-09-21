"""Tests for the AI provider preflight probe (pa-monitor doctor / startup check)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pa_agent.ai.provider_preflight import (
    diagnose_gateway_key,
    gateway_config_path,
    probe_provider,
    run_startup_preflight,
)

MODEL = "cline-pass/deepseek-v4.1-flash"


def _settings(base_url: str = "http://127.0.0.1:3123/v1", api_key: str = "sk-test-key", model: str = MODEL) -> SimpleNamespace:
    return SimpleNamespace(
        provider=SimpleNamespace(base_url=base_url, api_key=api_key, model=model)
    )


def _ok_models() -> dict:
    return {"data": [{"id": "other-model"}, {"id": MODEL}]}


def _gateway_config(tmp_path: Path, proxy_key: str) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 3123, "proxyKey": proxy_key}), encoding="utf-8")
    return path


def test_missing_base_url_or_key_is_a_config_problem() -> None:
    no_url = probe_provider(_settings(base_url=""))
    assert (no_url.ok, no_url.kind) == (False, "config")
    no_key = probe_provider(_settings(api_key=""))
    assert (no_key.ok, no_key.kind) == (False, "config")


def test_probe_never_touches_the_network_when_config_is_thin() -> None:
    def explode(*_args: object) -> tuple[int, object]:
        raise AssertionError("不该发起请求")

    assert probe_provider(_settings(api_key=""), http_get=explode).kind == "config"


def test_ok_when_model_is_in_the_gateway_list() -> None:
    calls: list[tuple[str, dict, float]] = []

    def fake_get(url: str, headers: dict, timeout: float) -> tuple[int, object]:
        calls.append((url, headers, timeout))
        return 200, _ok_models()

    result = probe_provider(_settings(), http_get=fake_get)
    assert result.ok is True
    assert result.kind == "ok"
    assert result.models_count == 2
    assert "在列" in result.message
    assert calls[0][0] == "http://127.0.0.1:3123/v1/models"
    assert calls[0][1]["Authorization"] == "Bearer sk-test-key"


def test_empty_model_list_still_passes() -> None:
    """网关不列模型时不能误报 —— 只验到鉴权就够。"""
    result = probe_provider(_settings(), http_get=lambda *_a: (200, {}))
    assert result.ok is True
    assert result.models_count == 0


def test_missing_model_is_reported_but_named() -> None:
    result = probe_provider(_settings(), http_get=lambda *_a: (200, {"data": [{"id": "x"}]}))
    assert (result.ok, result.kind) == (False, "model_missing")
    assert MODEL in result.message


def test_401_is_auth_and_carries_the_gateway_diagnosis(tmp_path: Path) -> None:
    gateway = _gateway_config(tmp_path, "sk_hc_903")
    result = probe_provider(
        _settings(api_key="sk-local-no-auth"),
        http_get=lambda *_a: (401, {"error": {"message": "unauthorized"}}),
        gateway_config=gateway,
    )
    assert (result.ok, result.kind, result.status) == (False, "auth", 401)
    assert "proxyKey" in result.diagnosis
    assert "同步" in result.diagnosis
    assert str(gateway) in result.diagnosis
    assert result.summary().count("\n") == 1


def test_403_counts_as_auth_too() -> None:
    result = probe_provider(_settings(), http_get=lambda *_a: (403, None))
    assert result.kind == "auth"


def test_transport_error_is_network_not_a_crash() -> None:
    def boom(*_args: object) -> tuple[int, object]:
        raise TimeoutError("read timeout")

    result = probe_provider(_settings(), http_get=boom)
    assert (result.ok, result.kind) == (False, "network")
    assert "read timeout" in result.message
    assert result.diagnosis


def test_other_http_status_is_reported_with_body() -> None:
    result = probe_provider(
        _settings(),
        http_get=lambda *_a: (502, {"error": {"message": "empty response content"}}),
    )
    assert (result.ok, result.kind, result.status) == (False, "http_error", 502)
    assert "empty response content" in result.message


def test_diagnosis_matches_equal_key_and_flags_upstream() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway_config(Path(tmp), "sk-test-key")
        text = diagnose_gateway_key("sk-test-key", gateway)
    assert "一致" in text
    assert "上游" in text


def test_diagnosis_explains_an_unauthenticated_gateway() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway_config(Path(tmp), "")
        text = diagnose_gateway_key("whatever", gateway)
    assert "未开鉴权" in text


def test_diagnosis_degrades_silently(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert diagnose_gateway_key("k", broken) == ""
    assert diagnose_gateway_key("k", tmp_path / "missing.json") == ""


def test_gateway_config_path_prefers_explicit_then_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = _gateway_config(tmp_path, "k")
    env_file = tmp_path / "env.json"
    env_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PA_GATEWAY_CONFIG", str(env_file))
    assert gateway_config_path(explicit) == explicit
    assert gateway_config_path() == env_file


def test_startup_preflight_alerts_on_failure_but_never_raises() -> None:
    sent: list[str] = []
    monkeypatch_settings = _settings()

    failing = run_startup_preflight(
        monkeypatch_settings,
        notify=sent.append,
    )
    assert failing.ok is False
    assert len(sent) == 1
    assert "自检失败" in sent[0]
    assert "持仓保护不受影响" in sent[0]


def test_startup_preflight_is_silent_when_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.ai import provider_preflight as mod

    monkeypatch.setattr(mod, "probe_provider", lambda *_a, **_k: mod.PreflightResult(True, "ok", "HTTP 200"))
    sent: list[str] = []
    result = run_startup_preflight(_settings(), notify=sent.append)
    assert result.ok is True
    assert sent == []


def test_startup_preflight_survives_a_broken_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    from pa_agent.ai import provider_preflight as mod

    monkeypatch.setattr(mod, "probe_provider", lambda *_a, **_k: mod.PreflightResult(False, "auth", "HTTP 401"))

    def boom(_text: str) -> None:
        raise RuntimeError("telegram down")

    assert run_startup_preflight(_settings(), notify=boom).ok is False
