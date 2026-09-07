"""Process-wide observation of Binance Testnet shared-IP rate-limit bans.

Binance Testnet shares public egress IPs and frequently answers HTTP 418
(code -1003, "IP banned until <epoch_ms>") or HTTP 429 for a few minutes.
Every Binance request funnels through one place (the client request layer),
so this module keeps a single breaker that records the ban window. The
monitoring scheduler consults it to pause K-line analysis and signal pushes
while the ban lasts, and the position-guard loops use it to sleep through
the ban instead of hammering the API and abandoning their guard after a few
consecutive errors.
"""

from __future__ import annotations

import re
import threading
import time

_BANNED_UNTIL_RE = re.compile(r"banned until (\d+)", re.IGNORECASE)
_RATE_LIMIT_MARKERS = ("http 418", "http 429", "-1003", "too many requests")
DEFAULT_NO_UNTIL_SECONDS = 60


def is_rate_limit_text(text: str) -> bool:
    """True when an API error message/body indicates a rate-limit ban."""
    low = (text or "").lower()
    return any(marker in low for marker in _RATE_LIMIT_MARKERS)


def parse_banned_until_ms(text: str) -> int | None:
    """Extract the epoch-ms ban window from an error body, if present."""
    match = _BANNED_UNTIL_RE.search(text or "")
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class RateLimitBreaker:
    """Thread-safe record of the current Binance ban window.

    A successful request does not need to clear the state: the ban ends when
    the wall clock passes banned_until_ms, so callers need no "confirm
    recovery" round-trip.
    """

    def __init__(self, no_until_seconds: int = DEFAULT_NO_UNTIL_SECONDS) -> None:
        self._no_until_seconds = int(no_until_seconds)
        self._until_ms: int | None = None
        self._lock = threading.Lock()

    def record_ban(self, *, until_ms: int | None = None, now_ms: int | None = None) -> None:
        """Record a ban ending at until_ms (or now + the fallback window)."""
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        candidate = until_ms if until_ms is not None else now + self._no_until_seconds * 1000
        with self._lock:
            if self._until_ms is not None:
                candidate = max(candidate, self._until_ms)
            self._until_ms = candidate

    def banned_until_ms(self) -> int | None:
        with self._lock:
            return self._until_ms

    def is_banned(self, now_ms: int | None = None) -> bool:
        until = self.banned_until_ms()
        if until is None:
            return False
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return now < until

    def remaining_seconds(self, now_ms: int | None = None) -> float | None:
        """Seconds until the ban ends; None when not currently banned."""
        until = self.banned_until_ms()
        if until is None:
            return None
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return max(0.0, (until - now) / 1000.0)

    def clear(self) -> None:
        with self._lock:
            self._until_ms = None


#: Process-wide singleton consulted by the Binance client and the monitor.
rate_limiter = RateLimitBreaker()
