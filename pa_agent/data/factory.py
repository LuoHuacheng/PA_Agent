"""Construct :class:`DataSource` implementations by kind id."""
from __future__ import annotations

import os
import sys
from typing import Literal

from pa_agent.data.base import DataSource
from pa_agent.data.market_defaults import GOLD_MT5_SYMBOL, GOLD_TV_SYMBOL

DataSourceKind = Literal["mt5", "tradingview"]

# UI-visible sources — 可在界面下拉框直接选择。
# MetaTrader5's Python package and terminal integration are Windows-only.
DATA_SOURCE_CHOICES: tuple[tuple[DataSourceKind, str], ...] = (
    (("tradingview", "TradingView"), ("mt5", "MT5"))
    if sys.platform == "win32"
    else (("tradingview", "TradingView"),)
)

_DEFAULT_SYMBOLS: dict[DataSourceKind, str] = {
    "mt5": GOLD_MT5_SYMBOL,
    "tradingview": GOLD_TV_SYMBOL,
}


def default_tradingview_exchange() -> str:
    """Empty string = UI «（自动）» — probe all TV preset venues."""
    return ""


def normalize_data_source_kind(kind: str | None) -> DataSourceKind:
    """Return a supported data-source kind, defaulting to TradingView."""
    if kind == "mt5" and sys.platform != "win32":
        return "tradingview"
    supported = {k for k, _ in DATA_SOURCE_CHOICES}
    if kind in supported:
        return kind  # type: ignore[return-value]
    return "tradingview"


def data_source_label(kind: str | None) -> str:
    """Human-readable label for *kind*."""
    normalized = normalize_data_source_kind(kind)
    for key, label in DATA_SOURCE_CHOICES:
        if key == normalized:
            return label
    return "MT5"


def default_symbol_for_kind(kind: str | None) -> str:
    return _DEFAULT_SYMBOLS[normalize_data_source_kind(kind)]


def _tradingview_credentials(settings: object | None) -> tuple[str, str]:
    """TradingView login credentials: settings.general first, env fallback.

    Mirrors the telegram convention (config value wins, environment variable
    backs it up) so the password does not have to live in settings.json.
    """
    username = password = ""
    if settings is not None:
        general = getattr(settings, "general", None)
        username = str(getattr(general, "tradingview_username", "") or "").strip()
        password = str(getattr(general, "tradingview_password", "") or "").strip()
    if not username:
        username = os.environ.get("TRADINGVIEW_USERNAME", "").strip()
    if not password:
        password = os.environ.get("TRADINGVIEW_PASSWORD", "").strip()
    return username, password


def create_data_source(kind: str | None, settings: object | None = None) -> DataSource:
    """Instantiate a fresh data source for *kind* (not connected).

    *settings* is optional; for TradingView it supplies the login credentials
    (only used when both username and password are non-empty).
    """
    normalized = normalize_data_source_kind(kind)
    if normalized == "tradingview":
        from pa_agent.data.tradingview import TradingViewSource

        username, password = _tradingview_credentials(settings)
        return TradingViewSource(username=username, password=password)
    from pa_agent.data.mt5 import MT5Source

    return MT5Source()
