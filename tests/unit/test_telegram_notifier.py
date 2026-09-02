"""Tests for Telegram Bot order-signal notification."""

from __future__ import annotations

from pa_agent.config.settings import Settings
from pa_agent.notify.telegram_notifier import (
    _build_order_text,
    send_telegram_message,
    telegram_is_active,
)


def _decision() -> dict:
    return {
        "order_type": "限价单",
        "order_direction": "做多",
        "entry_price": 103.02,
        "stop_loss_price": 102.57,
        "take_profit_price": 103.47,
        "take_profit_price_2": 103.62,
        "trade_confidence": 60,
        "estimated_win_rate": 52,
        "reasoning": "上升通道回撤接多",
    }


def test_telegram_inactive_without_credentials() -> None:
    settings = Settings()
    settings.telegram.enabled = True
    assert telegram_is_active(settings) is False


def test_telegram_active_with_credentials() -> None:
    settings = Settings()
    settings.telegram.enabled = True
    settings.telegram.bot_token = "123:abc"
    settings.telegram.chat_id = "987654321"
    assert telegram_is_active(settings) is True


def test_build_order_text_contains_key_fields() -> None:
    text = _build_order_text(
        decision_inner=_decision(),
        stage2_full={},
        symbol="SOLUSDT",
        timeframe="15m",
    )
    assert "SOLUSDT" in text
    assert "限价单" in text
    assert "做多" in text
    assert "103.02" in text
    assert "102.57" in text


def test_send_telegram_message_missing_config_returns_false() -> None:
    assert send_telegram_message("hi", settings=Settings()) is False


def test_send_telegram_message_http_failure_returns_false(monkeypatch) -> None:
    class _Resp:
        status_code = 400
        text = "Bad Request: chat not found"

    class _FakeRequests:
        @staticmethod
        def post(*_args, **_kw) -> _Resp:
            return _Resp()

    import sys

    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)
    assert (
        send_telegram_message(
            "hi", token="123:abc", chat_id="1", settings=Settings()
        )
        is False
    )


def test_send_telegram_message_success(monkeypatch) -> None:
    class _Resp:
        status_code = 200
        text = "ok"

    sent: list[dict] = []

    class _FakeRequests:
        @staticmethod
        def post(url, json, timeout):
            sent.append({"url": url, "json": json})
            return _Resp()

    import sys

    monkeypatch.setitem(sys.modules, "requests", _FakeRequests)
    assert send_telegram_message("hi", token="123:abc", chat_id="1") is True
    assert sent[0]["json"] == {"chat_id": "1", "text": "hi"}
    assert "123:abc" in sent[0]["url"]
