"""Auto-discover monitor targets from Binance USDⓈ-M 24h tickers.

Fetches ``fapi.binance.com/fapi/v1/ticker/24hr`` (public endpoint, no auth)
and returns the top-*N* contracts ranked by 24h quote volume (成交额) or by
the absolute 24h price change percentage (涨跌幅).
"""

from __future__ import annotations

import logging
import urllib.request
from typing import Literal

logger = logging.getLogger(__name__)

_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
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
    rank_by: Literal["quote_volume", "price_change_pct"] = "quote_volume",
    top_n: int = 10,
    stablecoin_only: bool = True,
    url: str = _TICKER_URL,
) -> list[str]:
    """Return the top-*N* USDⓈ-M contract symbols, most active first.

    Raises DiscoveryError on network failure or unparseable payload; the
    caller decides whether to fall back to the previous list.
    """
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
