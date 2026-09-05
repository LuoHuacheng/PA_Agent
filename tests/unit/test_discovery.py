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


def _mock_urlopen_capture(payload: object) -> list[str]:
    """Stub urlopen, recording requested URLs; returns the URL list."""
    import pa_agent.monitoring.discovery as discovery

    urls: list[str] = []

    class _Resp:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake(req, timeout):
        urls.append(str(req.full_url))
        return _Resp()

    discovery.urllib.request.urlopen = _fake
    return urls


_MARKET_CAP_PAYLOAD = [
    {"symbol": "btc", "market_cap": 1600000000000},
    {"symbol": "eth", "market_cap": 300000000000},
    {"symbol": "usdt", "market_cap": 120000000000},
    {"symbol": "xrp", "market_cap": 90000000000},
    {"symbol": "usdc", "market_cap": 30000000000},
    {"symbol": "figr_heloc", "market_cap": 40000000000},
    {"symbol": "bnb", "market_cap": None},
]


def test_market_cap_ranks_maps_usdt_and_skips_stablecoins() -> None:
    """rank_by=market_cap: sort by market cap desc, map to USDT perp
    symbols, excluding stablecoins and malformed rows."""
    _mock_urlopen(_MARKET_CAP_PAYLOAD)
    symbols = fetch_usdm_top_n(rank_by="market_cap", top_n=2, stablecoin_only=True)
    assert symbols == ["BTCUSDT", "ETHUSDT"]


def test_market_cap_fetches_coingecko_endpoint() -> None:
    urls = _mock_urlopen_capture(_MARKET_CAP_PAYLOAD)
    fetch_usdm_top_n(rank_by="market_cap", top_n=1, stablecoin_only=True)
    assert len(urls) == 1
    assert "api.coingecko.com" in urls[0]
    assert "market_cap_desc" in urls[0]


def test_market_cap_network_error_raises(monkeypatch) -> None:
    import pa_agent.monitoring.discovery as discovery

    def boom(*_args, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(discovery.urllib.request, "urlopen", boom)
    with pytest.raises(DiscoveryError):
        fetch_usdm_top_n(rank_by="market_cap")


def test_market_cap_non_list_payload_raises() -> None:
    _mock_urlopen({"oops": 1})
    with pytest.raises(DiscoveryError):
        fetch_usdm_top_n(rank_by="market_cap")


def test_rank_by_literal_accepts_market_cap() -> None:
    """The settings model must accept market_cap so option B can be configured"""
    from pa_agent.config.settings import AutoDiscoverSettings

    cfg = AutoDiscoverSettings(rank_by="market_cap", enabled=False)
    assert cfg.rank_by == "market_cap"
