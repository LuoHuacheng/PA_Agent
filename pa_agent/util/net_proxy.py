"""Proxy resolution for outbound WebSocket clients.

websocket-client only honours the conventional ``http_proxy``/``https_proxy``
environment variables plus its own explicit arguments; it never reads the macOS
network settings. A monitor process launched from the GUI therefore opens its
Binance/TradingView sockets *directly* while every REST call (urllib/httpx)
follows the browser proxy — the same gap Vibe-Trading closes for ccxt by
handing ``urllib.request.getproxies()`` to the exchange client.

TUN-mode proxies (fake-ip + auto-route) capture traffic below the socket layer
and need no help from here; this module only supplies explicit
``http_proxy_host``/``http_proxy_port`` when a proxy is really configured, and
returns empty kwargs otherwise. SOCKS-only configurations are out of scope:
websocket-client needs the optional ``socks`` extra for those.
"""
from __future__ import annotations

import logging
from typing import NamedTuple
from urllib.parse import urlparse
from urllib.request import getproxies

logger = logging.getLogger(__name__)

__all__ = ["resolve_proxy", "websocket_proxy_kwargs"]

#: Scheme preference when resolving a proxy for a websocket URL.
_PROXY_SCHEME_PRIORITY = ("https", "http")


class _ResolvedProxy(NamedTuple):
    host: str
    port: int
    auth: tuple[str, str] | None


def _configured_proxies() -> dict[str, str]:
    """Environment + macOS system proxy settings, lower-cased keys.

    The macOS ``no`` (exceptions) entry is a list on some Python builds, so
    every value is normalized to a comma-separated string.
    """
    try:
        raw = getproxies()
    except Exception:  # autodetection must never break a socket
        logger.debug("proxy autodetection failed", exc_info=True)
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            value = ",".join(str(item) for item in value)
        normalized[str(key).lower()] = str(value)
    return normalized


def _split_authority(value: str) -> _ResolvedProxy | None:
    """Parse ``host:port`` / ``scheme://[user:pass@]host:port`` into a proxy."""
    text = str(value).strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlparse(text)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        logger.debug("ignoring unparsable proxy entry", exc_info=True)
        return None
    if not host:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    auth: tuple[str, str] | None = None
    if parsed.username is not None:
        auth = (parsed.username, parsed.password or "")
    return _ResolvedProxy(host=host, port=int(port), auth=auth)


def _bypassed(host: str, entries: str | None) -> bool:
    """True when *host* matches the ``no_proxy`` exception list."""
    if not entries or not host:
        return False
    target = host.lower().rstrip(".")
    for raw in str(entries).split(","):
        entry = raw.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry == "*":
            return True
        # macOS exceptions arrive as "*.local"-style globs as well as ".foo.com"
        entry = entry.lstrip(".").removeprefix("*.").lstrip(".")
        if entry and (target == entry or target.endswith("." + entry)):
            return True
    return False


def resolve_proxy(url: str) -> _ResolvedProxy | None:
    """Proxy usable for *url*, or ``None`` when the dial should go direct."""
    parsed = urlparse(str(url))
    proxies = _configured_proxies()
    if not proxies:
        return None
    host = (parsed.hostname or "").strip()
    if _bypassed(host, proxies.get("no")):
        return None
    preferred = "https" if parsed.scheme == "wss" else "http"
    for scheme in (preferred, *[s for s in _PROXY_SCHEME_PRIORITY if s != preferred]):
        authority = proxies.get(scheme)
        if not authority:
            continue
        resolved = _split_authority(authority)
        if resolved is not None:
            return resolved
    return None


def websocket_proxy_kwargs(url: str) -> dict[str, object]:
    """Keyword arguments for ``WebSocketApp.run_forever``/``create_connection``.

    Empty when no proxy applies, so callers can always splat the result.
    """
    resolved = resolve_proxy(url)
    if resolved is None:
        return {}
    kwargs: dict[str, object] = {
        "http_proxy_host": resolved.host,
        "http_proxy_port": resolved.port,
        "proxy_type": "http",
    }
    if resolved.auth is not None:
        kwargs["http_proxy_auth"] = resolved.auth
    return kwargs
