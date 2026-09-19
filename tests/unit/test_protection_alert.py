"""守护线程"放弃保护"时必须让人看见(2026-09-19)。

现场: monitor_console 13:29:44-13:30:53 ETHUSDT 保本挂单被 -2021
"Order would immediately trigger" 拒了两次后 ``gave up ...; original stop stays
in place`` —— 只有一行 WARNING, 操作者看不到, 仓位继续裸扛。
"""
from __future__ import annotations

import time

import pa_agent.trading.position_manager as pm
from pa_agent.notify import telegram_notifier as tg


def _patch_sender(monkeypatch, *, boom: bool = False) -> list:
    sent: list = []

    def _send(text, settings=None):
        if boom:
            raise RuntimeError("telegram down")
        sent.append(text)
        return True

    monkeypatch.setattr(tg, "send_telegram_message", _send)
    monkeypatch.setattr(
        "pa_agent.config.settings.load_settings", lambda _p: object()
    )
    pm._PROTECTION_ALERT_LAST.clear()
    return sent


def test_protection_lost_alerts_once(monkeypatch) -> None:
    sent = _patch_sender(monkeypatch)
    assert pm._alert_protection_lost("BTCUSDT", "stop 123 gone, no candidate")
    assert len(sent) == 1
    assert "BTCUSDT" in sent[0]
    assert "stop 123 gone" in sent[0]


def test_protection_lost_is_rate_limited_per_symbol(monkeypatch) -> None:
    sent = _patch_sender(monkeypatch)
    assert pm._alert_protection_lost("BTCUSDT", "reason one") is True
    # 30 分钟内同品种不重复: 返回 False 表示"没发", 不是失败。
    assert pm._alert_protection_lost("BTCUSDT", "reason two") is False
    assert len(sent) == 1
    # 另一个品种不受影响
    assert pm._alert_protection_lost("ETHUSDT", "reason one")
    assert len(sent) == 2
    # 窗口过后可再发
    key = ("BTCUSDT", "protection")
    assert key in pm._PROTECTION_ALERT_LAST
    pm._PROTECTION_ALERT_LAST[key] = time.time() - pm._PROTECTION_ALERT_COOLDOWN_S - 1
    assert pm._alert_protection_lost("BTCUSDT", "reason three")
    assert len(sent) == 3


def test_protection_alert_never_raises(monkeypatch) -> None:
    _patch_sender(monkeypatch, boom=True)
    # 告警失败绝不能反噬交易线程
    assert pm._alert_protection_lost("BTCUSDT", "boom") is False
