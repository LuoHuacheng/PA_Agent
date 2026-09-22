"""账户体检: 判定必须把"账户级"与"策略级"分开说."""

from __future__ import annotations

from tools.account_healthcheck import collect, render, verdict


def report(
    *,
    clean_count=1749,
    clean_net=-7.18,
    flagged_count=359,
    flagged_net=-2553.83,
    window_coverage=0.96,
    expired=817,
    net_coverage=-0.03,
):
    return {
        "days": 90,
        "bounds": {"notional_ceiling": 5000.0},
        "analyzed_symbols": ["BTCUSDT"],
        "income_symbols": ["BTCUSDT", "HEMIUSDT"],
        "attach_audit": {
            "matched": 24,
            "manual": 817,
            "missing_decision": 1,
            "ambiguous": 0,
        },
        "coverage": {
            "trades_matched": 24,
            "trades_total": 842,
            "window_trades_matched": 24,
            "window_trades_total": 25,
            "window_trade_coverage": window_coverage,
            "expired_cid_count": expired,
            "expired_cid_net": -399.55,
            "external_count": 0,
            "external_net": 0.0,
            "account_income_net": -1963.64,
            "strategy_income_net": -258.89,
            "off_scope_net": -1704.75,
            "off_scope_symbols": {"HEMIUSDT": -1670.06},
            "net_coverage": net_coverage,
        },
        "contamination": {
            "clean_count": clean_count,
            "clean_net": clean_net,
            "flagged_count": flagged_count,
            "flagged_net": flagged_net,
            "total_net": clean_net + flagged_net,
            "by_reason": {"oversized_notional": {"count": 8, "net": -2286.85}},
        },
        "outcome_rows": 24,
    }


def test_verdict_bans_account_level_when_contamination_dominates():
    items = verdict(report())
    assert any("账户级盈亏不可用于评价策略" in x for x in items)


def test_verdict_always_states_strategy_line():
    items = verdict(report())
    assert any("-7.18U" in x and "1749" in x for x in items)


def test_verdict_mentions_expired_manual():
    items = verdict(report())
    assert any("订单历史过期" in x for x in items)


def test_verdict_warns_on_low_window_coverage():
    items = verdict(report(window_coverage=0.5))
    assert any("归因率只有 50%" in x for x in items)


def test_verdict_flags_uncomputable_net_coverage():
    items = verdict(report(net_coverage=None))
    assert any("不可算" in x for x in items)


def test_verdict_stays_quiet_when_contamination_is_small():
    items = verdict(report(clean_count=100, clean_net=-50.0, flagged_count=1, flagged_net=-1.0))
    assert not any("不可用于评价策略" in x for x in items)


def test_render_includes_all_four_sections():
    text = render(report())
    for marker in (
        "[1] 污染诊断",
        "[2] 归因分层",
        "[3] 归因覆盖率",
        "[4] 账户真相",
        "=== 判定 ===",
    ):
        assert marker in text


def test_collect_uses_injected_bundle(tmp_path):
    bundle = {
        "legs": [],
        "income": [],
        "orders": {},
        "fills": {},
        "symbols": [],
        "start_ms": 0,
        "end_ms": 0,
    }
    got = collect(
        30,
        pending_dir=tmp_path,
        csv_dir=tmp_path,
        settings_path=tmp_path / "nope.json",
        bundle=bundle,
    )
    assert got["days"] == 30
    assert got["contamination"]["legs_total"] == 0
    assert got["outcome_rows"] == 0
    assert got["bounds"]["notional_ceiling"] == 0.0


def test_verdict_separates_symbol_scope_from_leg_scope():
    items = verdict(report())
    assert any("策略可能范围净额" in x for x in items)
    assert any("差额来自策略品种上的超标单" in x for x in items)
