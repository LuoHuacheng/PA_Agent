"""tvDatafeed websocket: the proxy wrapper installed by the TradingView source.

tvDatafeed calls the module-level ``create_connection`` it imported from
websocket-client, which never reads the macOS network settings. The data source
wraps it so both market-data and Binance sockets follow the same route.
"""
from __future__ import annotations

import sys
import types

import pytest

from pa_agent.data import tradingview


@pytest.fixture()
def fake_tv(monkeypatch):
    """A stand-in tvDatafeed.main whose create_connection records its kwargs."""
    calls: list[dict] = []
    module = types.ModuleType("tvDatafeed.main")

    def create_connection(url, *args, **kwargs):
        calls.append({"url": url, "args": args, "kwargs": kwargs})
        return "socket"

    module.create_connection = create_connection  # type: ignore[attr-defined]
    parent = types.ModuleType("tvDatafeed")
    parent.main = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tvDatafeed", parent)
    monkeypatch.setitem(sys.modules, "tvDatafeed.main", module)
    return module, calls


def test_wrapper_is_installed_once_and_injects_the_proxy(fake_tv, monkeypatch) -> None:
    module, calls = fake_tv
    monkeypatch.setattr(
        tradingview,
        "websocket_proxy_kwargs",
        lambda _url: {
            "http_proxy_host": "127.0.0.1",
            "http_proxy_port": 7897,
            "proxy_type": "http",
        },
    )
    tradingview._enable_tvdatafeed_proxy()
    wrapper = module.create_connection
    tradingview._enable_tvdatafeed_proxy()  # idempotent: same wrapper, no double wrap
    assert module.create_connection is wrapper

    assert wrapper("wss://data.tradingview.com/socket.io/websocket", timeout=5.0) == "socket"
    assert calls == [
        {
            "url": "wss://data.tradingview.com/socket.io/websocket",
            "args": (),
            "kwargs": {
                "timeout": 5.0,
                "http_proxy_host": "127.0.0.1",
                "http_proxy_port": 7897,
                "proxy_type": "http",
            },
        }
    ]


def test_explicit_caller_kwargs_win(fake_tv, monkeypatch) -> None:
    module, calls = fake_tv
    monkeypatch.setattr(
        tradingview,
        "websocket_proxy_kwargs",
        lambda _url: {"http_proxy_host": "resolved", "http_proxy_port": 1},
    )
    tradingview._enable_tvdatafeed_proxy()
    module.create_connection("wss://example.test/ws", http_proxy_host="explicit")
    assert calls[0]["kwargs"]["http_proxy_host"] == "explicit"


def test_no_proxy_leaves_the_call_untouched(fake_tv, monkeypatch) -> None:
    module, calls = fake_tv
    monkeypatch.setattr(tradingview, "websocket_proxy_kwargs", lambda _url: {})
    tradingview._enable_tvdatafeed_proxy()
    module.create_connection("wss://example.test/ws", timeout=1.0)
    assert calls[0]["kwargs"] == {"timeout": 1.0}


def test_missing_create_connection_is_tolerated(monkeypatch) -> None:
    module = types.ModuleType("tvDatafeed.main")  # no create_connection at all
    parent = types.ModuleType("tvDatafeed")
    parent.main = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tvDatafeed", parent)
    monkeypatch.setitem(sys.modules, "tvDatafeed.main", module)
    tradingview._enable_tvdatafeed_proxy()  # must not raise
    assert not getattr(module, "_pa_proxy_patched", False)
