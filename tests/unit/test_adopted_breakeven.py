"""收养(对账)仓位的保本移动止损接线。

背景(2026-09-19): _adopt_position 无条件写 moved=True, 而 _arm_adopted_managers
只 arm watchdog/time-stop/TP runner —— 任何"重启后或对账时才被收养"的仓位,
移动止损通道开局即关(当前 state 里的 LINKUSDT 正是这一类)。
"""
from __future__ import annotations

import time

import pa_agent.trading.position_manager as pm
from pa_agent.config.settings import Settings
from pa_agent.trading.binance_usdm_testnet import _guard_enabled


def _cfg(**flags):
    settings = Settings()
    cfg = settings.binance_usdm_testnet
    for key, value in flags.items():
        setattr(cfg, key, value)
    return cfg


# ── _guard_enabled ───────────────────────────────────────────────────────────

def test_guard_enabled_off_trigger_wins() -> None:
    assert _guard_enabled(_cfg(breakeven_stop_trigger="off", breakeven_min_confidence=0), None) is False
    assert _guard_enabled(_cfg(breakeven_stop_trigger="off", breakeven_min_confidence=55), 99) is False


def test_guard_enabled_unknown_conf_counts_as_enabled_for_adopted() -> None:
    # 方案 A: 收养仓 conf=None; 老逻辑 (min_conf <= 0) 让它们永远拿不到守护。
    cfg = _cfg(breakeven_stop_trigger="1r", breakeven_min_confidence=55)
    assert _guard_enabled(cfg, None, unknown_conf_enabled=True) is True


def test_guard_enabled_unknown_conf_stays_strict_for_new_signals() -> None:
    # 新信号读不到 trade_confidence 属异常, 不能借此放宽保护策略。
    cfg = _cfg(breakeven_stop_trigger="1r", breakeven_min_confidence=55)
    assert _guard_enabled(cfg, None) is False
    assert _guard_enabled(_cfg(breakeven_stop_trigger="1r", breakeven_min_confidence=0), None) is True


def test_guard_enabled_still_filters_known_low_conf() -> None:
    cfg = _cfg(breakeven_stop_trigger="1r", breakeven_min_confidence=55)
    assert _guard_enabled(cfg, 54) is False
    assert _guard_enabled(cfg, 55) is True


# ── 收养止损位的 moved 判定 ──────────────────────────────────────────────────

def test_stop_is_at_breakeven_long() -> None:
    assert pm._stop_is_at_breakeven("BUY", 100.0, 100.0) is True
    assert pm._stop_is_at_breakeven("BUY", 100.0, 101.0) is True
    assert pm._stop_is_at_breakeven("BUY", 100.0, 95.0) is False


def test_stop_is_at_breakeven_short() -> None:
    assert pm._stop_is_at_breakeven("SELL", 100.0, 100.0) is True
    assert pm._stop_is_at_breakeven("SELL", 100.0, 99.0) is True
    assert pm._stop_is_at_breakeven("SELL", 100.0, 105.0) is False


def test_stop_is_at_breakeven_missing_price_is_false() -> None:
    assert pm._stop_is_at_breakeven("BUY", 100.0, None) is False
    assert pm._stop_is_at_breakeven("BUY", None, 95.0) is False


# ── _arm_adopted_managers 接线 ───────────────────────────────────────────────

def _stub_managers(monkeypatch) -> list:
    started: list = []

    def _rec(name):
        def _fn(**_kw):
            started.append(name)

        return _fn

    for name in (
        "_stop_watchdog_loop",
        "_timestop_loop",
        "_tp_runner_loop",
        "_breakeven_guard_loop",
    ):
        monkeypatch.setattr(pm, name, _rec(name))
    return started


def _wait(started: list, name: str, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while name not in started and time.time() < deadline:
        time.sleep(0.01)
    return name in started


def _record(**overrides):
    record = {
        "stop_algo_id": "pa-sl-1",
        "stop0": "95",
        "target": "110",
        "side": "BUY",
        "conf": None,
        "ts": 0.0,
        "moved": False,
        "adopted": True,
    }
    record.update(overrides)
    return record


def test_adopted_position_gets_breakeven_guard(monkeypatch) -> None:
    started = _stub_managers(monkeypatch)
    monkeypatch.setattr(pm, "_read_guard", lambda _s: _record())
    cfg = _cfg(breakeven_stop_trigger="1r", breakeven_min_confidence=55, time_stop_minutes=0)
    pm._arm_adopted_managers(object(), symbol="BTCUSDT", config=cfg, runner=False)
    assert _wait(started, "_breakeven_guard_loop")
    assert "_stop_watchdog_loop" in started


def test_adopted_position_already_moved_skips_breakeven(monkeypatch) -> None:
    started = _stub_managers(monkeypatch)
    monkeypatch.setattr(pm, "_read_guard", lambda _s: _record(moved=True))
    cfg = _cfg(breakeven_stop_trigger="1r", breakeven_min_confidence=55, time_stop_minutes=0)
    pm._arm_adopted_managers(object(), symbol="BTCUSDT", config=cfg, runner=False)
    assert _wait(started, "_stop_watchdog_loop")
    time.sleep(0.05)
    assert "_breakeven_guard_loop" not in started


def test_adopted_position_skips_breakeven_when_trigger_off(monkeypatch) -> None:
    started = _stub_managers(monkeypatch)
    monkeypatch.setattr(pm, "_read_guard", lambda _s: _record())
    cfg = _cfg(breakeven_stop_trigger="off", breakeven_min_confidence=0, time_stop_minutes=0)
    pm._arm_adopted_managers(object(), symbol="BTCUSDT", config=cfg, runner=False)
    assert _wait(started, "_stop_watchdog_loop")
    time.sleep(0.05)
    assert "_breakeven_guard_loop" not in started
