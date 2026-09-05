"""Auto-discover monitor targets from Binance USDⓈ-M 24h tickers.

Fetches Binance USDⓈ-M 24h tickers (public endpoint, no auth) and
returns the top-*N* contracts ranked by 24h quote volume (成交额), by the
absolute 24h price change percentage (涨跌幅), or by CoinGecko market cap
rank (市值, via ``/coins/markets``, no auth key required for light use).
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Literal

logger = logging.getLogger(__name__)

_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
_MARKET_CAP_URL = (
    "https://api.coingecko.com/api/v3/coins/markets?"
    "vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false"
)
#: CoinGecko short symbols excluded when stablecoin_only=True (market-cap mode).
_STABLECOIN_SYMBOLS = {"usdt", "usdc", "dai", "fdusd", "tusd", "pyusd",
                       "usde", "busd", "usdp", "usdm", "eurc", "usd1"}
_TIMEOUT_S = 10.0
#: Settled in fiat or another stablecoin; excluded when stablecoin_only=True.
_NON_USDT_SETTLEMENTS = ("USDC", "FDUSD", "TUSD", "USDP", "EUR", "GBP", "BUSD")


class DiscoveryError(RuntimeError):
    """Raised when the 24h ticker feed cannot be fetched or parsed."""


def _is_usdt_settled(symbol: str) -> bool:
    return symbol.endswith("USDT") and not any(
        symbol.endswith(suffix) for suffix in _NON_USDT_SETTLEMENTS
    )


def fetch_usdm_top_n(
    *,
    rank_by: Literal["quote_volume", "price_change_pct", "market_cap"] = "quote_volume",
    top_n: int = 10,
    stablecoin_only: bool = True,
    url: str = _TICKER_URL,
) -> list[str]:
    """Return the top-*N* USDⓈ-M contract symbols, most active first.

    rank_by="market_cap" ranks CoinGecko market-cap leaders instead of
    Binance 24h activity: the candidate pool is stable (近似固定市值池),
    with downstream TradingView validation dropping contracts without data.

    Raises DiscoveryError on network failure or unparseable payload; the
    caller decides whether to fall back to the previous list.
    """
    if rank_by == "market_cap":
        return _fetch_market_cap_top_n(
            top_n=top_n, stablecoin_only=stablecoin_only
        )
    import json

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PA_Agent/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = resp.read()
    except Exception as exc:  # noqa: BLE001 - network errors vary
        raise DiscoveryError(f"Binance 24h ticker 拉取失败: {exc}") from exc

    try:
        tickers = json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        raise DiscoveryError(f"Binance 24h ticker 响应解析失败: {exc}") from exc
    if not isinstance(tickers, list):
        raise DiscoveryError("Binance 24h ticker 响应格式异常")

    rows: list[tuple[str, float]] = []
    for t in tickers:
        symbol = str(t.get("symbol") or "")
        if stablecoin_only and not _is_usdt_settled(symbol):
            continue
        if rank_by == "quote_volume":
            try:
                value = float(t.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                continue
        else:
            try:
                value = abs(float(t.get("priceChangePercent") or 0.0))
            except (TypeError, ValueError):
                continue
        rows.append((symbol, value))

    rows.sort(key=lambda row: row[1], reverse=True)
    return [symbol for symbol, _ in rows[:top_n]]


def _fetch_market_cap_top_n(*, top_n: int, stablecoin_only: bool) -> list[str]:
    """Fetch CoinGecko market-cap leaders mapped to ``<SYMBOL>USDT``."""
    import json

    req = urllib.request.Request(
        _MARKET_CAP_URL,
        headers={"User-Agent": "PA_Agent/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = resp.read()
    except Exception as exc:
        raise DiscoveryError(f"CoinGecko 市值排名拉取失败: {exc}") from exc

    try:
        coins = json.loads(payload)
    except Exception as exc:
        raise DiscoveryError(f"CoinGecko 市值排名响应解析失败: {exc}") from exc
    if not isinstance(coins, list):
        raise DiscoveryError("CoinGecko 市值排名响应格式异常")

    rows: list[tuple[str, float]] = []
    for coin in coins:
        symbol = str(coin.get("symbol") or "").strip().lower()
        if not symbol.isalnum():
            continue  # wrapped / composite assets (e.g. figr_heloc)
        if stablecoin_only and symbol in _STABLECOIN_SYMBOLS:
            continue
        try:
            cap = float(coin.get("market_cap") or 0.0)
        except (TypeError, ValueError):
            continue
        if cap <= 0:
            continue
        rows.append((symbol.upper() + "USDT", cap))

    rows.sort(key=lambda row: row[1], reverse=True)
    return [symbol for symbol, _ in rows[:top_n]]
