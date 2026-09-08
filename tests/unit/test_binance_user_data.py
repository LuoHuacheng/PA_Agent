
"""Binance user-data stream: event dispatch, lifecycle, reconnect.

The stream module talks to websocket-client; every test here replaces the
websocket module with a fake WebSocketApp so no network is touched.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import ClassVar

import pytest

from pa_agent.trading import binance_user_data as bud
from pa_agent.trading.binance_user_data import (
    BinanceUserDataStream,
    UserDataEventHandlers,
)

# ---------------------------------------------------------------------------
# fake websocket module
# ---------------------------------------------------------------------------


class _FakeApp:
    """WebSocketApp lookalike driven by the test."""

    instances: ClassVar[list[_FakeApp]] = []
    #: first N connections drop instantly (simulates a flaky socket)
    auto_drop_first_n: ClassVar[int] = 0

    def __init__(self, url, on_open, on_message, on_error, on_close) -> None:
        self.url = url
        self.on_open = on_open
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.closed = threading.Event()
        self.drop_immediately = False
        _FakeApp.instances.append(self)

    def run_forever(self, ping_interval=None, ping_timeout=None) -> None:
        self.on_open(self)
        if len(_FakeApp.instances) <= _FakeApp.auto_drop_first_n:
            return  # simulate an instant disconnect
        self.closed.wait(timeout=5.0)
        self.on_close(self)

    def close(self) -> None:
        self.closed.set()

    # test helpers
    def push(self, payload: dict) -> None:
        self.on_message(self, json.dumps(payload))


class _FakeWsModule:
    WebSocketApp = _FakeApp


@pytest.fixture(autouse=True)
def _fake_websocket(monkeypatch):
    _FakeApp.instances = []
    _FakeApp.auto_drop_first_n = 0
    monkeypatch.setitem(sys.modules, "websocket", _FakeWsModule())
    monkeypatch.setattr(bud, "_DEFAULT_WS_BASE", "wss://fake.example")
    yield
    monkeypatch.delitem(sys.modules, "websocket", raising=False)


@pytest.fixture()
def stream():
    return BinanceUserDataStream(
        create_listen_key=lambda: "KEY-1",
        keepalive_listen_key=lambda k: None,
        close_listen_key=lambda k: None,
    )


# ---------------------------------------------------------------------------
# event dispatch (no threads)
# ---------------------------------------------------------------------------


def test_dispatches_order_and_account_updates(stream) -> None:
    got: list[tuple[str, str]] = []
    stream._handlers = UserDataEventHandlers(
        on_order_update=lambda m: got.append(("order", m["o"]["s"])),
        on_account_update=lambda m: got.append(("acct", m["a"]["B"][0]["a"])),
    )
    stream._on_message(None, json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "X": "FILLED"}}))
    stream._on_message(None, json.dumps({"e": "ACCOUNT_UPDATE", "a": {"B": [{"a": "USDT"}]}}))
    assert got == [("order", "BTCUSDT"), ("acct", "USDT")]


def test_ignores_non_json_and_non_dict_messages(stream) -> None:
    seen: list[str] = []
    stream._handlers = UserDataEventHandlers(on_other_event=lambda e, m: seen.append(e))
    stream._on_message(None, "garbage")
    stream._on_message(None, "[1,2]")
    assert seen == []


def test_unknown_event_goes_to_on_other(stream) -> None:
    seen: list[tuple[str, str]] = []
    stream._handlers = UserDataEventHandlers(
        on_other_event=lambda e, m: seen.append((e, str(m.get("x"))))
    )
    stream._on_message(None, json.dumps({"e": "MARGIN_CALL", "x": "1"}))
    assert seen == [("MARGIN_CALL", "1")]


def test_listen_key_expired_clears_key_and_notifies(stream) -> None:
    events: list[str] = []
    stream._handlers = UserDataEventHandlers(
        on_listen_key_expired=lambda: events.append("expired"),
        on_other_event=lambda e, m: events.append("other:" + e),
    )
    stream._listen_key = "KEY-1"
    stream._on_message(None, json.dumps({"e": "listenKeyExpired"}))
    assert stream._listen_key is None
    assert events == ["expired"]


def test_handler_exception_does_not_escape(stream) -> None:
    def boom(_msg) -> None:
        raise RuntimeError("handler boom")

    stream._handlers = UserDataEventHandlers(on_order_update=boom)
    stream._on_message(None, json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {}}))  # must not raise


def test_connected_hook_fires_on_open(stream) -> None:
    n = []
    stream._handlers = UserDataEventHandlers(on_connected=lambda: n.append(1))
    stream._on_open(None)
    assert n == [1]

# ---------------------------------------------------------------------------
# lifecycle / reconnect (threaded, fake socket)
# ---------------------------------------------------------------------------


def test_reconnect_uses_backoff_then_succeeds(monkeypatch) -> None:
    """首次连接即断 -> 退避 1s 后重连, on_connected 每连必触发(对账时机)."""
    created: list[str] = []
    connected = []
    slept: list[float] = []
    key_seq = ["K-A", "K-B"]

    def _create() -> str:
        created.append(1)
        return key_seq[len(created) - 1]

    s = BinanceUserDataStream(
        create_listen_key=_create,
        keepalive_listen_key=lambda k: None,
        handlers=UserDataEventHandlers(on_connected=lambda: connected.append(1)),
        sleep=lambda d: slept.append(d),
    )
    _FakeApp.auto_drop_first_n = 1  # first socket drops instantly
    s.start()
    try:
        deadline = time.time() + 3
        while len(_FakeApp.instances) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(_FakeApp.instances) == 2, "must reconnect once after instant drop"
        assert _FakeApp.instances[0].url.endswith("/ws/K-A")
        assert _FakeApp.instances[1].url.endswith("/ws/K-A")  # same key reused
        assert slept[0] == 1.0  # first backoff rung
        assert len(connected) == 2  # on_open on both connections
    finally:
        s.stop()


def test_start_is_idempotent(stream) -> None:
    assert stream.start() is True
    assert stream.start() is False
    stream.stop()
    assert stream.start() is True  # restarts after stop
    stream.stop()


def test_stop_closes_socket_and_listen_key() -> None:
    closed_key = []

    def _close(k: str) -> None:
        closed_key.append(k)

    s = BinanceUserDataStream(
        create_listen_key=lambda: "K-C",
        keepalive_listen_key=lambda k: None,
        close_listen_key=_close,
    )
    s.start()
    try:
        deadline = time.time() + 2
        while not _FakeApp.instances and time.time() < deadline:
            time.sleep(0.01)
        assert _FakeApp.instances
        s.stop()
        assert closed_key == ["K-C"]
    finally:
        s.stop()


def test_keepalive_runs_periodically_and_forces_rekey_on_failure() -> None:
    kept: list[str] = []
    created: list[str] = []

    def _keep(k: str) -> None:
        kept.append(k)
        if len(kept) >= 2:
            raise RuntimeError("keepalive network error")

    s = BinanceUserDataStream(
        create_listen_key=lambda: (created.append("new") or "K-N" + str(len(created))),
        keepalive_listen_key=_keep,
        close_listen_key=lambda k: None,
        keepalive_interval_seconds=0.02,
        sleep=lambda d: None,  # keep reconnect loop from really sleeping
    )
    s.start()
    try:
        deadline = time.time() + 3
        # keepalive #1 ok; #2 fails -> key cleared -> reconnect path re-creates
        while time.time() < deadline and not (len(kept) >= 3 and len(created) >= 2):
            time.sleep(0.01)
        assert len(kept) >= 2
        assert len(created) >= 2, "failed keepalive must force a fresh listenKey"
    finally:
        s.stop()


def test_listen_key_property_reflects_created_key(stream) -> None:
    assert stream.listen_key() is None
    stream.start()
    try:
        assert stream.listen_key() == "KEY-1"
    finally:
        stream.stop()

