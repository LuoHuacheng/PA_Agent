"""TradingView login fallback (auto-degrade to anonymous on signin blocks).

Guards the fix for TradingView's rate-limited legacy signin endpoint: while a
block is active the source must NOT re-probe on every reconnect (that makes the
ban worse and spams "error while signin"), and connect() must run an anonymous
TvDatafeed instead, healing automatically once the cooldown lapses.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pa_agent.data.tradingview as tvmod
from pa_agent.data.tradingview import TradingViewSource


def _reset_health() -> None:
    tvmod._tv_login_health.update(
        state="unknown", token="", until=0.0, reason=""
    )


def _fake_resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def test_blocked_signin_probes_once_per_cooldown() -> None:
    _reset_health()
    try:
        payload = {
            "error": "We're having a little trouble with that request. "
            "Try again a little later.",
            "code": "rate_limit",
        }
        with patch("requests.post", return_value=_fake_resp(payload)) as post:
            assert tvmod._tv_auth_token("u", "p") == ""
            # Second call within the cooldown must NOT hit the network again.
            assert tvmod._tv_auth_token("u", "p") == ""
        assert post.call_count == 1
    finally:
        _reset_health()


def test_healthy_signin_reuses_cached_token() -> None:
    _reset_health()
    try:
        payload = {"user": {"auth_token": "tok-123"}}
        with patch("requests.post", return_value=_fake_resp(payload)) as post:
            assert tvmod._tv_auth_token("u", "p") == "tok-123"
            # Cached token is reused within _TV_SIGNIN_TOKEN_TTL_S.
            assert tvmod._tv_auth_token("u", "p") == "tok-123"
        assert post.call_count == 1
    finally:
        _reset_health()


def test_no_credentials_never_probes() -> None:
    _reset_health()
    try:
        with patch("requests.post") as post:
            assert tvmod._tv_auth_token("", "") == ""
        post.assert_not_called()
    finally:
        _reset_health()


def test_connect_degrades_to_anonymous_when_login_blocked() -> None:
    _reset_health()
    try:
        src = TradingViewSource(username="tvuser", password="tvpass")
        with (
            patch("pa_agent.data.tradingview._tv_auth_token", return_value=""),
            patch("tvDatafeed.TvDatafeed") as tv_cls,
        ):
            src.connect()

        tv_cls.assert_called_once_with()  # anonymous — no credential args
        assert src._connected is True
        assert src._tv_mode == "anonymous"
    finally:
        _reset_health()


def test_connect_upgrades_anonymous_instance_with_token() -> None:
    _reset_health()
    try:
        src = TradingViewSource(username="tvuser", password="tvpass")
        with (
            patch("pa_agent.data.tradingview._tv_auth_token", return_value="tok-abc"),
            patch("tvDatafeed.TvDatafeed") as tv_cls,
        ):
            src.connect()

        tv_cls.assert_called_once_with()  # anonymous constructor (no 2nd POST)
        assert src._tv.token == "tok-abc"
        assert src._tv_mode == "login"
    finally:
        _reset_health()
