"""入口门控: 回放必须与实盘同一套开关, 生效日之前一律放行."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from tools.shadow_replay import (
    UNKNOWN_CYCLE,
    EntryGates,
    apply_entry_gates,
    gate_reasons,
    load_entry_gates,
)
from tools.shadow_replay import Plan as ReplayPlan


def make_plan(
    *,
    symbol="BTCUSDT",
    direction="做多",
    cycle_position="broad_channel",
    diag_direction="up",
    entry=100.0,
    stop=99.0,
    target=103.0,
    at_utc=None,
):
    """造一条计划单; at_utc 用 UTC datetime 指定决策时刻."""
    moment = at_utc or datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    return ReplayPlan(
        path="p.json",
        symbol=symbol,
        timeframe="30m",
        record_ms=int(moment.timestamp() * 1000),
        order_type="限价单",
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        target2=None,
        cycle_position=cycle_position,
        diag_direction=diag_direction,
        trade_conf=60.0,
        diag_conf=60.0,
        est_win_rate=50.0,
    )


def test_no_gates_admits_everything():
    gates = EntryGates(symbols=frozenset())
    assert gate_reasons(make_plan(), gates) == ()


def test_symbol_whitelist_blocks_outsider():
    gates = EntryGates(symbols=frozenset({"ETHUSDT"}))
    assert gate_reasons(make_plan(symbol="BTCUSDT"), gates) == ("symbol_block",)
    assert gate_reasons(make_plan(symbol="ethusdt"), gates) == ()


def test_short_block_uses_chinese_direction_label():
    gates = EntryGates(symbols=frozenset(), block_short=True)
    assert gate_reasons(make_plan(direction="做空"), gates) == ("short_block",)
    assert gate_reasons(make_plan(direction="做多"), gates) == ()


def test_cycle_allowlist_ignores_unknown_cycle():
    gates = EntryGates(symbols=frozenset(), cycle_allowlist=("broad_channel",))
    assert gate_reasons(make_plan(cycle_position="trending_tr"), gates) == ("cycle_block",)
    assert gate_reasons(make_plan(cycle_position=UNKNOWN_CYCLE), gates) == ()
    assert gate_reasons(make_plan(cycle_position="broad_channel"), gates) == ()


def test_enabled_from_uses_local_day_and_lets_earlier_plans_through():
    """生效日按 UTC+8 本地日判, 与 _cutoff_from_days 同一坐标系."""
    gates = EntryGates(
        symbols=frozenset({"ETHUSDT"}),
        block_short=True,
        block_session=True,
        enabled_from="2026-09-13",
    )
    # 09-12 02:00 UTC = 09-12 10:00 UTC+8 -> 生效日之前, 全部放行
    early = make_plan(
        symbol="BTCUSDT", direction="做空", at_utc=datetime(2026, 9, 12, 2, tzinfo=UTC)
    )
    assert gate_reasons(early, gates) == ()
    # 09-12 16:30 UTC = 09-13 00:30 UTC+8 -> 已跨到生效日, 门控生效(且不在禁开仓时段)
    boundary = make_plan(
        symbol="BTCUSDT", direction="做空", at_utc=datetime(2026, 9, 12, 16, 30, tzinfo=UTC)
    )
    assert gate_reasons(boundary, gates) == ("symbol_block", "short_block")
    # 09-13 22:00 UTC = 09-14 06:00 UTC+8 -> 门控生效且落在禁开仓时段
    late = make_plan(
        symbol="BTCUSDT", direction="做空", at_utc=datetime(2026, 9, 13, 22, tzinfo=UTC)
    )
    assert gate_reasons(late, gates) == ("symbol_block", "short_block", "session_block")


def test_session_block_wraps_midnight():
    gates = EntryGates(
        symbols=frozenset(),
        block_session=True,
        session_start_utc=20,
        session_end_utc=4,
    )
    blocked = [21, 23, 0, 3]
    free = [4, 12, 19]
    for hour in blocked:
        plan = make_plan(at_utc=datetime(2026, 9, 15, hour, 30, tzinfo=UTC))
        assert gate_reasons(plan, gates) == ("session_block",), hour
    for hour in free:
        plan = make_plan(at_utc=datetime(2026, 9, 15, hour, 30, tzinfo=UTC))
        assert gate_reasons(plan, gates) == (), hour


def test_session_window_with_equal_bounds_never_blocks():
    gates = EntryGates(
        symbols=frozenset(), block_session=True, session_start_utc=5, session_end_utc=5
    )
    assert gate_reasons(make_plan(at_utc=datetime(2026, 9, 15, 5, 0, tzinfo=UTC)), gates) == ()


def test_rr_block_uses_plan_prices():
    gates = EntryGates(symbols=frozenset(), min_plan_rr=1.5)
    assert gate_reasons(make_plan(entry=100.0, stop=99.0, target=101.0), gates) == ("rr_block",)
    assert gate_reasons(make_plan(entry=100.0, stop=99.0, target=103.0), gates) == ()


def test_rr_block_rejects_degenerate_plan():
    gates = EntryGates(symbols=frozenset(), min_plan_rr=1.5)
    plan = make_plan(entry=100.0, stop=100.0, target=103.0)
    assert gate_reasons(plan, gates) == ("rr_block",)


def test_load_entry_gates_reads_section(tmp_path):
    cfg = {
        "binance_usdm_testnet": {
            "symbol_whitelist": ["btcusdt", " ", "LINKUSDT"],
            "block_short_entry": True,
            "block_cycle_allowlist": ["broad_channel"],
            "block_session_utc_start_hour": 1,
            "min_plan_reward_risk": 1.5,
        }
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    gates = load_entry_gates(path, enabled_from="2026-09-13")
    assert gates.symbols == frozenset({"BTCUSDT", "LINKUSDT"})
    assert gates.block_short is True
    assert gates.cycle_allowlist == ("broad_channel",)
    assert gates.session_start_utc == 1
    assert gates.min_plan_rr == 1.5
    assert gates.enabled_from == "2026-09-13"


def test_load_entry_gates_missing_file_disables_everything(tmp_path):
    gates = load_entry_gates(tmp_path / "nope.json")
    assert gates.symbols == frozenset()
    assert gates.block_short is False
    assert gates.cycle_allowlist == ()
    assert gates.min_plan_rr == 0.0


def test_apply_entry_gates_splits_and_counts():
    gates = EntryGates(symbols=frozenset(), block_short=True, min_plan_rr=1.5)
    plans = [
        make_plan(),
        make_plan(direction="做空"),
        make_plan(entry=100.0, stop=99.0, target=101.0),
        make_plan(direction="做空", entry=100.0, stop=99.0, target=101.0),
    ]
    admitted, counts = apply_entry_gates(plans, gates)
    assert len(admitted) == 1
    assert counts["short_block"] == 1
    assert counts["rr_block"] == 1
    assert counts["short_block+rr_block"] == 1
