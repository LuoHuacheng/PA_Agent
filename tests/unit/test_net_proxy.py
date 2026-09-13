"""net_proxy: proxy resolution for websocket clients.

websocket-client ignores the macOS network settings, so the monitor derives the
proxy itself (env vars first, then the system settings) and hands it to the
socket explicitly. These tests pin that resolution down without touching the
network.
"""
from __future__ import annotations

import pytest

from pa_agent.util import net_proxy

_ENV_KEYS = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _proxies(monkeypatch, mapping) -> None:
    monkeypatch.setattr(net_proxy, "getproxies", lambda: mapping)


def test_no_configuration_returns_empty_kwargs(monkeypatch) -> None:
    _proxies(monkeypatch, {})
    assert net_proxy.websocket_proxy_kwargs("wss://fstream.binancefuture.com/ws/KEY") == {}


def test_wss_prefers_the_https_entry(monkeypatch) -> None:
    _proxies(monkeypatch, {"http": "127.0.0.1:7890", "https": "http://127.0.0.1:7897"})
    kwargs = net_proxy.websocket_proxy_kwargs("wss://fstream.binancefuture.com/ws/KEY")
    assert kwargs == {
        "http_proxy_host": "127.0.0.1",
        "http_proxy_port": 7897,
        "proxy_type": "http",
    }


def test_ws_prefers_the_http_entry(monkeypatch) -> None:
    _proxies(monkeypatch, {"http": "127.0.0.1:7890", "https": "127.0.0.1:7897"})
    kwargs = net_proxy.websocket_proxy_kwargs("ws://example.test/socket")
    assert kwargs["http_proxy_port"] == 7890


def test_falls_back_to_the_other_scheme(monkeypatch) -> None:
    _proxies(monkeypatch, {"http": "127.0.0.1:7890"})
    kwargs = net_proxy.websocket_proxy_kwargs("wss://example.test/ws")
    assert kwargs["http_proxy_port"] == 7890


def test_proxy_auth_is_forwarded(monkeypatch) -> None:
    _proxies(monkeypatch, {"https": "http://user:secret@proxy.test:8080"})
    kwargs = net_proxy.websocket_proxy_kwargs("wss://example.test/ws")
    assert kwargs["http_proxy_auth"] == ("user", "secret")


def test_no_proxy_exceptions_bypass_the_proxy(monkeypatch) -> None:
    _proxies(monkeypatch, {"https": "127.0.0.1:7897", "no": "binancefuture.com,localhost"})
    assert net_proxy.websocket_proxy_kwargs("wss://fstream.binancefuture.com/ws/K") == {}
    # 非例外域名照常走代理
    assert net_proxy.websocket_proxy_kwargs("wss://data.tradingview.com/socket.io/websocket")


def test_no_proxy_list_values_are_normalized(monkeypatch) -> None:
    """macOS 的 exceptions 在部分 Python 版本上是 list, 不能炸. """
    _proxies(monkeypatch, {"https": "127.0.0.1:7897", "no": ["localhost", "binancefuture.com"]})
    assert net_proxy.websocket_proxy_kwargs("wss://fstream.binancefuture.com/ws/K") == {}


def test_no_proxy_domain_suffix_entries(monkeypatch) -> None:
    """后缀与 *.suffix 两种写法都要认(实测 macOS 就是 *.local 这种)."""
    _proxies(monkeypatch, {"https": "127.0.0.1:7897", "no": ".binancefuture.com,*.local"})
    assert net_proxy.websocket_proxy_kwargs("wss://fstream.binancefuture.com/ws/K") == {}
    assert net_proxy.websocket_proxy_kwargs("wss://printer.local/ws") == {}
    assert net_proxy.websocket_proxy_kwargs("wss://data.tradingview.com/ws")


def test_wildcard_no_proxy_disables_everything(monkeypatch) -> None:
    _proxies(monkeypatch, {"https": "127.0.0.1:7897", "no": "*"})
    assert net_proxy.websocket_proxy_kwargs("wss://example.test/ws") == {}


def test_socks_only_configuration_is_ignored(monkeypatch) -> None:
    """SOCKS 需要 websocket-client 的 socks extra, 不在本模块职责内."""
    _proxies(monkeypatch, {"socks": "socks5://127.0.0.1:1080"})
    assert net_proxy.websocket_proxy_kwargs("wss://example.test/ws") == {}


def test_unparsable_entry_is_skipped(monkeypatch) -> None:
    _proxies(monkeypatch, {"https": "://nonsense", "http": "127.0.0.1:7890"})
    kwargs = net_proxy.websocket_proxy_kwargs("wss://example.test/ws")
    assert kwargs["http_proxy_port"] == 7890


def test_environment_variables_are_honoured(monkeypatch) -> None:
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    kwargs = net_proxy.websocket_proxy_kwargs("wss://example.test/ws")
    assert kwargs["http_proxy_host"] == "127.0.0.1"
    assert kwargs["http_proxy_port"] == 7897


def test_autodetection_failure_is_swallowed(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("no system configuration")

    monkeypatch.setattr(net_proxy, "getproxies", _boom)
    assert net_proxy.websocket_proxy_kwargs("wss://example.test/ws") == {}
