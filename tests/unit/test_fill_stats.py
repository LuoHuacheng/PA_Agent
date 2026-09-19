"""fill_stats 成交率遥测 + service 接线 的回归测试."""
from __future__ import annotations

import json

from pa_agent.monitoring import fill_stats


def test_bump_and_fill_rate():
    stats: dict = {}
    fill_stats.bump(stats, "btcusdt", "placed")
    fill_stats.bump(stats, "BTCUSDT", "placed")
    fill_stats.bump(stats, "BTCUSDT", "fills")
    fill_stats.bump(stats, "BTCUSDT", "timeouts", by=3)
    assert stats == {"BTCUSDT": {"placed": 2, "fills": 1, "timeouts": 3}}
    assert fill_stats.fill_rate(stats["BTCUSDT"]) == 0.25


def test_breakout_shadow_is_counted_separately():
    """突破单影子样本单独计数: 它没上交易所, 绝不能稀释成交率."""
    stats: dict = {}
    fill_stats.bump(stats, "BTCUSDT", "placed")
    fill_stats.bump(stats, "BTCUSDT", "fills")
    fill_stats.bump(stats, "BTCUSDT", "breakout_shadow", by=7)
    # 成交率只看 fills / timeouts: 影子单不参与。
    assert fill_stats.fill_rate(stats["BTCUSDT"]) == 1.0
    summary = fill_stats.summarize(stats)
    assert "成交率100%" in summary
    assert stats["BTCUSDT"]["breakout_shadow"] == 7


def test_bump_ignores_bad_input():
    stats: dict = {}
    fill_stats.bump(stats, "", "placed")
    fill_stats.bump(stats, "BTCUSDT", "unknown_field")
    assert stats == {}


def test_fill_rate_none_without_samples():
    assert fill_stats.fill_rate(None) is None
    assert fill_stats.fill_rate({}) is None
    assert fill_stats.fill_rate({"fills": 0, "timeouts": 0}) is None


def test_normalize_drops_junk():
    raw = {
        "btcusdt": {"placed": "3", "fills": -1, "timeouts": 2, "junk": 9},
        "": {"fills": 1},
        "ETHUSDT": "not-a-dict",
        "SOLUSDT": {"fills": 0, "timeouts": 0},
    }
    assert fill_stats.normalize(raw) == {
        "BTCUSDT": {"placed": 3, "fills": 0, "timeouts": 2, "breakout_shadow": 0}
    }


def test_summarize_text():
    text = fill_stats.summarize({"BTCUSDT": {"placed": 4, "fills": 1, "timeouts": 3}})
    assert "挂牌4" in text and "成交1" in text and "超时3" in text and "25%" in text
    assert "尚无样本" in fill_stats.summarize({})


def test_timeout_sink_roundtrip():
    seen: list[str] = []
    try:
        fill_stats.set_timeout_sink(seen.append)
        fill_stats.notify_timeout("linkusdt")
        assert seen == ["LINKUSDT"]
        # sink 抛异常不得打断撤单流程
        fill_stats.set_timeout_sink(lambda _s: (_ for _ in ()).throw(RuntimeError("boom")))
        fill_stats.notify_timeout("LINKUSDT")
    finally:
        fill_stats.set_timeout_sink(None)
    fill_stats.notify_timeout("BTCUSDT")
    assert seen == ["LINKUSDT"]


def test_service_observe_execution_persists(tmp_path, monkeypatch):
    """pending → placed, Previously-filled → fills, 其它状态忽略。"""
    from types import SimpleNamespace

    from pa_agent.monitoring import service as service_mod

    svc = object.__new__(service_mod.MultiSymbolMonitor)
    svc._fill_stats = {}
    svc._observe_execution("ETHUSDT", SimpleNamespace(status="pending", reason=""))
    svc._observe_execution("ETHUSDT", SimpleNamespace(status="rejected", reason="Stop too close"))
    svc._observe_execution(
        "ETHUSDT",
        SimpleNamespace(status="skipped", reason="Previously filled limit entry now protected"),
    )
    svc._bump_fill_timeout("ETHUSDT")
    assert svc._fill_stats == {"ETHUSDT": {"placed": 1, "fills": 1, "timeouts": 1}}


def test_state_roundtrip_keeps_fill_stats(tmp_path):
    """_load_state/_save_state 必须保留 fill_stats, 且容忍旧文件的缺失。"""
    from pa_agent.monitoring import service as service_mod

    state = tmp_path / "monitoring_state.json"
    state.write_text(
        json.dumps({"direction_gate_stats": {"short_block": 2}}, ensure_ascii=False),
        encoding="utf-8",
    )
    svc = object.__new__(service_mod.MultiSymbolMonitor)
    svc._state_path = state
    svc._direction_gate_stats = {}
    svc._fill_stats = {}
    svc._persisted_closed_ts = {}
    svc._structure_exit_streak = {}
    svc._load_state()
    assert svc._fill_stats == {}
    assert svc._direction_gate_stats == {"short_block": 2}

    svc._fill_stats = {"BTCUSDT": {"placed": 2, "fills": 1, "timeouts": 1}}
    svc._save_state()
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["fill_stats"] == {"BTCUSDT": {"placed": 2, "fills": 1, "timeouts": 1}}
