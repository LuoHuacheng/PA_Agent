"""Tests for Binance USDⓈ-M 24h ticker auto-discovery."""

from __future__ import annotations

import json

import pytest

from pa_agent.monitoring.discovery import DiscoveryError, fetch_usdm_top_n

_TICKERS = [
    {"symbol": "BTCUSDT", "quoteVolume": "5000", "priceChangePercent": "1.5"},
    {"symbol": "ETHUSDT", "quoteVolume": "9000", "priceChangePercent": "-2.0"},
    {"symbol": "SOLUSDT", "quoteVolume": "3000", "priceChangePercent": "8.0"},
    {"symbol": "BTCUSDC", "quoteVolume": "99999", "priceChangePercent": "50.0"},
    {"symbol": "RAREBTC", "quoteVolume": "100", "priceChangePercent": "30.0"},
    {"symbol": "DOGEUSDT", "quoteVolume": "2000", "priceChangePercent": "4.0"},
]


def _mock_urlopen(payload: object) -> None:
    import pa_agent.monitoring.discovery as discovery

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    discovery.urllib.request.urlopen = lambda _req, timeout: _Resp()


def test_ranks_by_quote_volume_with_usdt_only() -> None:
    _mock_urlopen(_TICKERS)
    symbols = fetch_usdm_top_n(rank_by="quote_volume", top_n=3, stablecoin_only=True)
    # USDC pair excluded; ETHUSDT (9000) > BTCUSDT (5000) > SOLUSDT (3000)
    assert symbols == ["ETHUSDT", "BTCUSDT", "SOLUSDT"]


def test_ranks_by_abs_price_change_pct() -> None:
    _mock_urlopen(_TICKERS)
    symbols = fetch_usdm_top_n(rank_by="price_change_pct", top_n=2, stablecoin_only=False)
    assert symbols == ["BTCUSDC", "RAREBTC"]


def test_stablecoin_only_false_includes_usdc() -> None:
    _mock_urlopen(_TICKERS)
    symbols = fetch_usdm_top_n(rank_by="quote_volume", top_n=2, stablecoin_only=False)
    assert symbols == ["BTCUSDC", "ETHUSDT"]


def test_raises_on_network_error(monkeypatch) -> None:
    import pa_agent.monitoring.discovery as discovery

    def boom(*_args, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(discovery.urllib.request, "urlopen", boom)
    with pytest.raises(DiscoveryError):
        fetch_usdm_top_n()


def test_raises_on_non_list_payload() -> None:
    _mock_urlopen({"oops": 1})
    with pytest.raises(DiscoveryError):
        fetch_usdm_top_n()


def test_empty_list_when_no_valid_rows() -> None:
    _mock_urlopen([{"symbol": "", "quoteVolume": "x"}, {"symbol": "NOPE"}])
    assert fetch_usdm_top_n(rank_by="quote_volume", stablecoin_only=True) == []
