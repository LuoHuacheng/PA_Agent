"""守护线程"放弃保护"时必须让人看见(2026-09-19)。

现场: monitor_console 13:29:44-13:30:53 ETHUSDT 保本挂单被 -2021
"Order would immediately trigger" 拒了两次后 ``gave up ...; original stop stays
in place`` —— 只有一行 WARNING, 操作者看不到, 仓位继续裸扛。

2026-09-19 二次回归: 告警接上后, 单测夹具(entry=100/stop0=90/mark=80)跑完
give-up 分支也发真 Telegram, 每跑一次 `pytest tests/unit` 一条假警报。本文件
同时钉住两条边界: **真实启用账户才推送**, **限流桶按阶段区分**。
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

import pa_agent.trading.position_manager as pm
from pa_agent.notify import telegram_notifier as tg

#: 模块导入时(autouse 夹具打补丁之前)抓到的真实闸门函数。
_REAL_GATE = pm.protection_alerts_enabled


def _settings_stub(*, enabled: bool = True, dry_run: bool = False):
    cfg = SimpleNamespace(enabled=enabled, dry_run=dry_run)
    return SimpleNamespace(binance_usdm_environment="testnet", binance_usdm_testnet=cfg)


def _patch_sender(monkeypatch, *, boom: bool = False) -> list:
    """把 `_alert_protection_lost` 放到"真实启用账户"下并记录发出的文本."""
    sent: list = []

    def _send(text, settings=None):
        if boom:
            raise RuntimeError("telegram down")
        sent.append(text)
        return True

    monkeypatch.setattr(tg, "send_telegram_message", _send)
    monkeypatch.setattr(pm, "protection_alerts_enabled", _REAL_GATE)
    monkeypatch.setattr(
        "pa_agent.config.settings.load_settings", lambda _p: _settings_stub()
    )
    pm._PROTECTION_ALERT_LAST.clear()
    return sent


# ── 闸门: dry_run / 未启用账户绝不推送 ─────────────────────────────────


def test_gate_only_opens_for_enabled_live_account() -> None:
    assert _REAL_GATE(_settings_stub()) is True
    assert _REAL_GATE(_settings_stub(enabled=False)) is False
    assert _REAL_GATE(_settings_stub(dry_run=True)) is False
    assert _REAL_GATE(None) is False
    # 结构不认识的 settings 一律关闸, 不许因为读不到字段就默认放行
    assert _REAL_GATE(object()) is False


def test_alert_suppressed_without_live_account(monkeypatch, caplog) -> None:
    """默认 autouse 闸门(等价 dry_run)下: 记日志、返回 False、不碰推送入口."""
    sent: list = []
    monkeypatch.setattr(
        tg, "send_telegram_message", lambda *a, **k: sent.append(a) or True
    )
    pm._PROTECTION_ALERT_LAST.clear()

    with caplog.at_level(logging.ERROR):
        assert pm._alert_protection_lost("BTCUSDT", "no candidate") is False

    assert sent == []
    assert "suppressed" in caplog.text


# ── 推送内容与限流桶 ───────────────────────────────────────────────────


def test_protection_lost_alerts_once(monkeypatch) -> None:
    sent = _patch_sender(monkeypatch)
    assert pm._alert_protection_lost("BTCUSDT", "stop 123 gone, no candidate")
    assert len(sent) == 1
    assert "BTCUSDT" in sent[0]
    assert "stop 123 gone" in sent[0]


def test_alert_carries_kind_and_detail_without_guessing(monkeypatch) -> None:
    """文案带上阶段与细节; 不再硬写"只有原始止损兜底"这种臆断."""
    sent = _patch_sender(monkeypatch)
    pm._alert_protection_lost(
        "ETHUSDT",
        "re-hang failed: -2021",
        kind="runner-verify",
        detail="记录止损单 pa-sl-abc",
    )
    text = sent[0]
    assert "阶段: runner-verify" in text
    assert "细节: 记录止损单 pa-sl-abc" in text
    assert "已停止自动补挂/核验" in text
    assert "只有原始止损兜底" not in text


def test_protection_lost_is_rate_limited_per_symbol(monkeypatch) -> None:
    sent = _patch_sender(monkeypatch)
    assert pm._alert_protection_lost("BTCUSDT", "reason one") is True
    # 30 分钟内同品种同阶段不重复: 返回 False 表示"没发", 不是失败。
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


def test_different_kinds_have_separate_cooldown_buckets(monkeypatch) -> None:
    """同一品种的"补挂失败"和"看护线程读盘失败"是两起事故, 不许互相吞掉."""
    sent = _patch_sender(monkeypatch)
    assert pm._alert_protection_lost("BTCUSDT", "runner", kind="runner-verify")
    assert pm._alert_protection_lost("BTCUSDT", "watchdog", kind="watchdog")
    assert pm._alert_protection_lost("BTCUSDT", "breakeven", kind="breakeven-swap")
    assert len(sent) == 3
    assert ("BTCUSDT", "runner-verify") in pm._PROTECTION_ALERT_LAST
    # 同阶段第二次仍被限流
    assert (
        pm._alert_protection_lost("BTCUSDT", "runner again", kind="runner-verify")
        is False
    )
    assert len(sent) == 3


def test_stop_absence_reason_is_readable() -> None:
    """失活原因必须是可读中文, 而不是让操作者自己去交易所查状态码。"""
    f = pm._stop_absence_reason
    assert "撤销" in f("CANCELED")
    assert "撤销" in f("CANCELLED")
    assert "触发" in f("FILLED")
    assert "过期" in f("EXPIRED")
    assert "查无此单" in f("", missing=True)
    # 未知状态原样带上, 不吞掉信息
    assert "WEIRD_STATUS" in f("weird_status")


def test_alert_reason_carries_stop_absence_state(monkeypatch) -> None:
    """真实事故形态: 记录止损已 CANCELED 且无候选可补 -> 原因里带状态。"""
    sent = _patch_sender(monkeypatch)
    note = (
        f"{pm._stop_absence_reason('CANCELED')}; "
        "no placeable re-hang candidate (mark=80)"
    )
    pm._alert_protection_lost(
        "BTCUSDT",
        f"TP runner 无法核验保护单, 可能裸奔: {note}",
        kind="runner-verify",
        detail="记录止损单 pa-sl-old0001",
    )
    text = sent[0]
    assert "已被撤销" in text
    assert "no placeable re-hang candidate" in text
    assert "记录止损单 pa-sl-old0001" in text


def test_protection_alert_never_raises(monkeypatch) -> None:
    _patch_sender(monkeypatch, boom=True)
    # 告警失败绝不能反噬交易线程
    assert pm._alert_protection_lost("BTCUSDT", "boom") is False


def test_missing_settings_never_raises(monkeypatch) -> None:
    """读配置本身炸掉时也必须安静返回 False (告警绝不反噬看护线程)."""
    monkeypatch.setattr(pm, "protection_alerts_enabled", _REAL_GATE)

    def _boom(_p):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr("pa_agent.config.settings.load_settings", _boom)
    assert pm._alert_protection_lost("BTCUSDT", "boom") is False


@pytest.mark.parametrize("dry_run", [True, False])
def test_gate_reads_dry_run_from_active_config(dry_run: bool) -> None:
    assert _REAL_GATE(_settings_stub(dry_run=dry_run)) is (not dry_run)
