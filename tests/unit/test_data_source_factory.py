"""Tests for data source factory and settings."""
from __future__ import annotations

import pa_agent.data.factory as factory

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tushare_source import TushareSource
from pa_agent.data.tradingview import TradingViewSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "tradingview"
    assert normalize_data_source_kind(None) == "tradingview"


def test_mt5_falls_back_to_tradingview_on_non_windows(monkeypatch):
    monkeypatch.setattr(factory.sys, "platform", "darwin")
    assert factory.normalize_data_source_kind("mt5") == "tradingview"


def test_mt5_is_not_a_ui_choice_on_non_windows():
    if factory.sys.platform != "win32":
        assert "mt5" not in {kind for kind, _ in DATA_SOURCE_CHOICES}


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("eastmoney") == "eastmoney"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"


def test_mt5_in_ui_choices():
    """MT5 仅 Windows 时在 UI 可选列表中且排首位；非 Windows 时不可选。"""
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    if factory.sys.platform == "win32":
        assert "mt5" in ui_kinds
        assert DATA_SOURCE_CHOICES[0][0] == "mt5"
    else:
        assert "mt5" not in ui_kinds
    # eastmoney / AkShare 仍是隐藏源
    assert "eastmoney" not in ui_kinds
    assert "akshare" not in ui_kinds


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_create_data_source_returns_expected_types():
    expected_mt5_type = MT5Source if factory.sys.platform == "win32" else TradingViewSource
    assert isinstance(create_data_source("mt5"), expected_mt5_type)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("eastmoney"), EastMoneySource)
    assert isinstance(create_data_source("tushare"), TushareSource)


def test_default_symbols_per_kind():
    expected_mt5_symbol = "XAUUSDm" if factory.sys.platform == "win32" else "XAUUSD"
    assert default_symbol_for_kind("mt5") == expected_mt5_symbol


def _fake_settings(username: str = "", password: str = "") -> object:
    return type("S", (), {"general": type("G", (), {
        "tradingview_username": username,
        "tradingview_password": password,
    })()})()


def test_tradingview_source_gets_login_credentials_from_settings():
    src = create_data_source("tradingview", settings=_fake_settings("tvuser", "tvpass"))
    assert isinstance(src, TradingViewSource)
    assert src._username == "tvuser"
    assert src._password == "tvpass"


def test_tradingview_credentials_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_USERNAME", "envuser")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "envpass")
    # settings 未提供凭据 -> 走环境变量
    src = create_data_source("tradingview", settings=_fake_settings())
    assert src._username == "envuser"
    assert src._password == "envpass"
    # 不传 settings 同样读环境变量
    src = create_data_source("tradingview")
    assert src._username == "envuser"


def test_tradingview_credentials_settings_beat_env(monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_USERNAME", "envuser")
    monkeypatch.setenv("TRADINGVIEW_PASSWORD", "envpass")
    src = create_data_source(
        "tradingview", settings=_fake_settings("cfguser", "cfgpass")
    )
    assert (src._username, src._password) == ("cfguser", "cfgpass")
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("eastmoney") == "000001"
    assert default_symbol_for_kind("tushare") == "000001"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "tradingview"
