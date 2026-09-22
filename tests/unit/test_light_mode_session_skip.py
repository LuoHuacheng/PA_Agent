"""会话窗内跳过 stage2 的测试 (2026-09-22)."""
from __future__ import annotations

from types import SimpleNamespace

from pa_agent.monitoring.service import MultiSymbolMonitor

STAGE1 = {
    "gate_result": "proceed",
    "cycle_position": "trading_range",
    "direction": "neutral",
    "key_signals": ["边界清晰"],
    "program_features": {
        "breakout_quality": "failed",
        "zone": "middle_third",
        "swing_structure": "mixed",
        "barbwire_candidate": False,
        "spike_aftermath_hint": "none",
        "scale_conflict": False,
    },
}


def _cfg(**kw):
    base = dict(
        light_mode_enabled=True,
        light_mode_max_quiet_bars=3,
        light_mode_skip_blocked_session=True,
        block_session_entry=True,
        block_session_utc_start_hour=20,
        block_session_utc_end_hour=4,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _judge(monkeypatch, cfg, previous_record=None):
    monkeypatch.setattr("pa_agent.trading.binance_env.active_cfg", lambda _s: cfg)
    # 打补丁要打在真源头: light_gate 里的 _session_blocked_now 是函数内 import。
    monkeypatch.setattr(
        "pa_agent.trading.signal_pipeline._session_blocked_now", lambda *a, **k: True
    )
    monitor = object.__new__(MultiSymbolMonitor)
    monitor._cfg = cfg
    monitor._settings = object()
    monitor._light_quiet = {}
    return monitor._light_mode_judge("BTCUSDT::30m", previous_record)


def _submitted_plan_record():
    return SimpleNamespace(
        stage1_diagnosis=STAGE1,
        stage2_decision={
            "decision": {"order_type": "限价单", "entry_price": 1.0},
            "execution": {"status": "submitted"},
        },
    )

def test_session_blocked_skip_reason_only_inside_gated_window():
    # 闸门关着时放行状态绝不跳 stage2, 否则会错过真实下单机会。
    from pa_agent.monitoring.light_gate import session_blocked_skip_reason

    assert session_blocked_skip_reason(_cfg(block_session_entry=False), now_hour=20) == ""
    assert session_blocked_skip_reason(_cfg(), now_hour=12) == ""
    assert session_blocked_skip_reason(_cfg(), now_hour=19) == ""
    assert session_blocked_skip_reason(_cfg(), now_hour=20)
    assert session_blocked_skip_reason(_cfg(), now_hour=23)
    assert session_blocked_skip_reason(_cfg(), now_hour=0)
    assert session_blocked_skip_reason(_cfg(), now_hour=3)
    day = _cfg(block_session_utc_start_hour=8, block_session_utc_end_hour=12)
    assert session_blocked_skip_reason(day, now_hour=9)
    assert session_blocked_skip_reason(day, now_hour=13) == ""


def test_light_judge_skips_stage2_inside_blocked_window(monkeypatch):
    # 窗内无在场计划 -> 跳 stage2; stage1 照跑, 结构离场与诊断记录都保住。
    judge = _judge(monkeypatch, _cfg())
    assert judge is not None
    assert judge(STAGE1) != ""


def test_light_judge_keeps_stage2_when_gate_is_off(monkeypatch):
    # 回归护栏: 闸门关着时窗口逻辑不得生效。
    judge = _judge(monkeypatch, _cfg(block_session_entry=False))
    assert judge(STAGE1) == ""


def test_blocked_window_does_not_skip_stage2_while_plan_working(monkeypatch):
    # 有在场计划时不跳: 那张单还要 stage2 维护, 省调用不能省掉它。
    judge = _judge(monkeypatch, _cfg(), _submitted_plan_record())
    assert judge(STAGE1) == ""


def test_skip_blocked_session_setting_default_and_example():
    import json
    from pathlib import Path

    from pa_agent.config.settings import Settings

    assert Settings().monitoring.light_mode_skip_blocked_session is False
    raw = json.loads(Path("config/settings.example.json").read_text(encoding="utf-8"))
    assert raw["monitoring"]["light_mode_skip_blocked_session"] is False
