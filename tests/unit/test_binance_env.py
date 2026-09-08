"""Unit tests for the Binance testnet/live execution environment switch.

The active environment follows ``settings.binance_usdm_environment``; live
keeps its own config section, gateways, state file and labels, and refuses
structural mistakes (both sections enabled / live without credentials).
"""

import json

import pytest

from pa_agent.config.settings import Settings, load_settings, save_settings
from pa_agent.trading import binance_env
from pa_agent.trading.binance_env import (
    LIVE_ENV,
    TESTNET_ENV,
    active_cfg,
    env_conflicts,
    resolve_env,
)
from pa_agent.trading.binance_usdm_testnet import (
    BinanceAPIError,
    BinanceUSDMTestnetClient,
    _load_state,
    _save_state,
    active_environment,
    configure_binance_environment,
    execute_market_signal,
)


@pytest.fixture(autouse=True)
def _reset_runtime_env():
    """Every test starts from (and returns to) the default testnet env."""
    configure_binance_environment(None)
    yield
    configure_binance_environment(None)


def test_defaults_are_testnet_and_live_is_safe():
    """environment defaults to testnet; live section is inert by default."""
    s = Settings()
    assert s.binance_usdm_environment == "testnet"
    live = s.binance_usdm_live
    assert live.enabled is False
    assert live.dry_run is True
    assert live.emergency_stop is True
    assert live.api_key == ""
    assert live.api_secret == ""


def test_profiles_cover_env_specific_wiring():
    assert TESTNET_ENV.rest_base == "https://testnet.binancefuture.com"
    assert TESTNET_ENV.ws_base == "wss://fstream.binancefuture.com"
    assert TESTNET_ENV.state_file == "binance_usdm_testnet_state.json"
    assert LIVE_ENV.rest_base == "https://fapi.binance.com"
    assert LIVE_ENV.ws_base == "wss://fstream.binance.com"
    assert LIVE_ENV.state_file == "binance_usdm_live_state.json"
    assert LIVE_ENV.label_zh == "实盘"
    assert binance_env.env_for_key("live") is LIVE_ENV
    assert binance_env.env_for_key("bogus") is TESTNET_ENV


def test_resolve_and_active_cfg_select_section():
    s = Settings()
    assert resolve_env(s) is TESTNET_ENV
    assert active_cfg(s) is s.binance_usdm_testnet
    s.binance_usdm_environment = "live"
    s.binance_usdm_live.enabled = True
    assert resolve_env(s) is LIVE_ENV
    assert active_cfg(s) is s.binance_usdm_live
    # settings-less and duck-typed calls keep legacy testnet behaviour
    assert resolve_env(None) is TESTNET_ENV
    assert active_cfg(None).enabled is False


def test_live_settings_round_trip(tmp_path):
    """live credentials/safety flags persist in the settings file."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.binance_usdm_environment = "live"
    original.binance_usdm_live.api_key = "live-key"
    original.binance_usdm_live.api_secret = "live-secret"
    original.binance_usdm_live.enabled = True
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.binance_usdm_environment == "live"
    assert loaded.binance_usdm_live.api_key == "live-key"
    assert loaded.binance_usdm_live.api_secret == "live-secret"
    assert loaded.binance_usdm_live.enabled is True
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["binance_usdm_environment"] == "live"
    assert data["binance_usdm_live"]["api_key"] == "live-key"


def test_conflicts_refuse_both_enabled_and_keyless_live():
    s = Settings()
    assert env_conflicts(s) is None
    # live armed alone under the testnet env is dormant: nothing executes,
    # so no conflict - the switch only fires once environment=live is declared
    s.binance_usdm_live.enabled = True
    assert env_conflicts(s) is None
    # both sections enabled: refuse regardless of which env is declared
    s.binance_usdm_testnet.enabled = True
    assert env_conflicts(s) is not None
    s.binance_usdm_live.enabled = False
    s.binance_usdm_testnet.enabled = False
    assert env_conflicts(s) is None
    # live declared on without credentials: refuse
    s.binance_usdm_environment = "live"
    s.binance_usdm_live.enabled = True
    assert "未配置" in env_conflicts(s)
    s.binance_usdm_live.api_key = "k"
    s.binance_usdm_live.api_secret = "s"
    assert env_conflicts(s) is None


def test_execute_skipped_reason_and_conflict_follow_environment():
    # live declared, disabled: label and gate come from the live section
    s = Settings()
    s.binance_usdm_environment = "live"
    result = execute_market_signal({}, s)
    assert result.status == "skipped"
    assert result.reason == "Binance Live automation disabled"
    # both sections enabled: execution refuses with the conflict reason
    s.binance_usdm_testnet.enabled = True
    s.binance_usdm_live.enabled = True
    result = execute_market_signal({}, s)
    assert result.status == "rejected"
    assert "同时 enabled" in result.reason


def test_client_gateway_follows_runtime_environment(monkeypatch):
    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_opener(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse()

    s = Settings()
    s.binance_usdm_environment = "live"
    s.binance_usdm_live.api_key = "k"
    s.binance_usdm_live.api_secret = "s"
    configure_binance_environment(s)
    client = BinanceUSDMTestnetClient("k", "s", opener=fake_opener)
    with pytest.raises(BinanceAPIError):
        client.exchange_info("BTCUSDT")
    assert captured["url"].startswith(LIVE_ENV.rest_base)
    # back to testnet defaults: gateway follows the environment
    configure_binance_environment(None)
    client2 = BinanceUSDMTestnetClient("k", "s", opener=fake_opener)
    with pytest.raises(BinanceAPIError):
        client2.exchange_info("BTCUSDT")
    assert captured["url"].startswith(TESTNET_ENV.rest_base)
    # explicit base_url pins the gateway regardless of environment
    client3 = BinanceUSDMTestnetClient(
        "k", "s", opener=fake_opener, base_url="https://example.test"
    )
    with pytest.raises(BinanceAPIError):
        client3.exchange_info("BTCUSDT")
    assert captured["url"].startswith("https://example.test")


def test_state_file_is_separated_per_environment(monkeypatch, tmp_path):
    from pa_agent.trading import binance_usdm_testnet as mod

    monkeypatch.setattr(mod, "_RUNTIME_STATE_PATH", str(tmp_path / "state.json"))
    s = Settings()
    s.binance_usdm_environment = "live"
    configure_binance_environment(s)
    assert active_environment().key == "live"
    _save_state({"seen": {"x": 1}})
    live_file = tmp_path / LIVE_ENV.state_file
    assert live_file.exists()
    assert _load_state() == {"seen": {"x": 1}}
    # a testnet env reads/writes its own file, not the live one
    configure_binance_environment(None)
    assert _load_state() == {}
    _save_state({"seen": {"y": 2}})
    testnet_file = tmp_path / TESTNET_ENV.state_file
    assert testnet_file.exists()
    assert not live_file.read_text(encoding="utf-8").count("\"y\"")


def test_missing_credentials_message_is_env_labeled(monkeypatch):
    s = Settings()
    s.binance_usdm_environment = "live"
    configure_binance_environment(s)
    with pytest.raises(ValueError, match="Binance Live API key and secret"):
        BinanceUSDMTestnetClient("", "")
