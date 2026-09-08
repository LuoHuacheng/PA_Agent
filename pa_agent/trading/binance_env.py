"""Binance USDⓈ-M execution environment registry (testnet / live).

One process runs one execution environment. The active environment comes
from ``settings.binance_usdm_environment`` (default: ``testnet``, so existing
setups behave exactly as before). ``live`` is opt-in and its config section
defaults keep every safety switch on, so switching the field alone never
places real orders.

Everything environment-specific (REST base URL, user-data WS gateway, runtime
state file, log/notification labels) is derived here so the trading code has
one source of truth. This module must stay free of trading imports (settings
layer stays trading-agnostic); consumers import it directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pa_agent.config.settings import BinanceUSDMTestnetSettings

EnvKey = Literal["testnet", "live"]


@dataclass(frozen=True)
class BinanceTradeEnv:
    """Everything that differs between the testnet and the live environment."""

    key: EnvKey
    rest_base: str
    ws_base: str
    #: Runtime execution state file name inside trade_records/ (kept separate
    #: per environment so guards/watchers never manage the other env orders).
    state_file: str
    label_en: str
    label_zh: str


#: WS base URLs must match the official Binance USDⓈ-M Futures gateway for
#: each environment (see binance_user_data._DEFAULT_WS_BASE, the testnet one).
TESTNET_ENV = BinanceTradeEnv(
    key="testnet",
    rest_base="https://testnet.binancefuture.com",
    ws_base="wss://fstream.binancefuture.com",
    state_file="binance_usdm_testnet_state.json",
    label_en="Testnet",
    label_zh="测试网",
)

LIVE_ENV = BinanceTradeEnv(
    key="live",
    rest_base="https://fapi.binance.com",
    ws_base="wss://fstream.binance.com",
    state_file="binance_usdm_live_state.json",
    label_en="Live",
    label_zh="实盘",
)

_ENVS: dict[str, BinanceTradeEnv] = {
    TESTNET_ENV.key: TESTNET_ENV,
    LIVE_ENV.key: LIVE_ENV,
}


def env_for_key(key: str | None) -> BinanceTradeEnv:
    """Resolve an environment key to its profile (unknown keys fall back to testnet)."""
    return _ENVS.get(str(key or "").strip().lower(), TESTNET_ENV)


def env_key_of(settings: Any | None) -> EnvKey:
    """Environment key declared by *settings* (duck-typed; default testnet)."""
    if settings is None:
        return "testnet"
    return env_for_key(getattr(settings, "binance_usdm_environment", "testnet")).key


def resolve_env(settings: Any | None) -> BinanceTradeEnv:
    """Profile of the environment declared by *settings* (default testnet)."""
    return env_for_key(env_key_of(settings))


def active_cfg(settings: Any | None) -> BinanceUSDMTestnetSettings:
    """Active execution section: live section when live, testnet otherwise.

    Settings-less calls and duck-typed fakes (tests) fall back to a default
    testnet section, preserving legacy behaviour.
    """
    if settings is None:
        return BinanceUSDMTestnetSettings()
    if env_key_of(settings) == "live":
        section = getattr(settings, "binance_usdm_live", None)
    else:
        section = getattr(settings, "binance_usdm_testnet", None)
    return section if section is not None else BinanceUSDMTestnetSettings()


def env_conflicts(settings: Any | None) -> str | None:
    """Structural config errors that must refuse execution, else None.

    - both sections enabled: one process must never touch two accounts;
    - live declared with empty credentials: nothing to sign requests with.
    """
    if settings is None:
        return None
    testnet_enabled = bool(
        getattr(getattr(settings, "binance_usdm_testnet", None), "enabled", False)
    )
    live_enabled = bool(
        getattr(getattr(settings, "binance_usdm_live", None), "enabled", False)
    )
    if testnet_enabled and live_enabled:
        return (
            "binance_usdm_testnet 与 binance_usdm_live 同时 enabled:"
            " 同一进程只能运行一个环境"
        )
    if env_key_of(settings) == "live" and live_enabled:
        cfg = active_cfg(settings)
        if not (cfg.api_key or "").strip() or not (cfg.api_secret or "").strip():
            return (
                "binance_usdm_environment=live 但 binance_usdm_live"
                " 未配置 api_key/api_secret"
            )
    return None
