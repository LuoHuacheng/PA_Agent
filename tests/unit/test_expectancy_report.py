"""实盘期望判定的样本切分: T6 的 R2/R3 只能用闸门上线后的样本."""
from __future__ import annotations

import csv

from tools.expectancy_report import (
    GATE_START,
    account_drawdown_pct,
    evaluate,
    filter_since,
    loss_bound_ok,
    main,
    max_drawdown,
    max_drawdown_usdt,
    parse_cutoff,
    rollback_alerts,
)


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["uid", "ts_close", "net_usdt"])
        for uid, ts, net in rows:
            w.writerow([uid, ts, net])
    return path


def test_parse_cutoff_iso8601_with_tz():
    # 2026-09-18T15:11:00+08:00 == 2026-09-18T07:11:00Z
    assert parse_cutoff("2026-09-18T15:11:00+08:00") == 1789715460000


def test_parse_cutoff_accepts_epoch_ms():
    assert parse_cutoff("1789715460000") == 1789715460000
    assert parse_cutoff(" 2000 ") == 2000


def test_gate_start_is_a_valid_iso_string():
    # GATE_START = 最近一次配置变更时刻(2026-09-20 14:09:32+08:00, 甲+乙组合改动后重启)。
    # 改线上配置时必须同步更新这里 —— 判定口径不能跨配置混样本。
    assert parse_cutoff(GATE_START) == 1789884572000


def test_filter_since_keeps_only_newer_rows():
    rows = [
        {"uid": "old", "ts_close": "1000", "net_usdt": "1"},
        {"uid": "edge", "ts_close": "2000", "net_usdt": "2"},
        {"uid": "new", "ts_close": "3000", "net_usdt": "3"},
    ]
    kept = [r["uid"] for r in filter_since(rows, 2000)]
    assert kept == ["edge", "new"]


def test_filter_since_drops_unparsable_timestamps():
    rows = [{"uid": "bad", "ts_close": "", "net_usdt": "1"}]
    assert filter_since(rows, 0) == []


def test_max_drawdown_of_losing_streak():
    assert max_drawdown([-1.0, -1.0, -1.0]) == 0.0  # 曲线从未在峰值之上
    assert abs(max_drawdown([10.0, -5.0]) - 0.5) < 1e-9


def test_max_drawdown_usdt_matches_absolute_peak_to_trough():
    assert max_drawdown_usdt([10.0, -5.0]) == 5.0
    # 纯亏序列: 峰值就是起始权益 0, 谷底 -2 => 回撤 2U。
    # (比值口径 max_drawdown() 在这种序列上返回 0.0 —— 一直亏却不报回撤, 这是它的盲点。)
    assert max_drawdown_usdt([-1.0, -1.0]) == 2.0
    assert max_drawdown([-1.0, -1.0]) == 0.0


def test_evaluate_reports_both_drawdown_flavours():
    res = evaluate([10.0, -5.0] * 15)
    assert res["dd"] > 0
    assert res["dd_usdt"] > 0


def test_account_drawdown_pct_uses_capital_as_denominator():
    assert account_drawdown_pct([10.0, -5.0], 100.0) == 0.05
    assert account_drawdown_pct([10.0, -5.0], 0.0) == 0.0
    assert account_drawdown_pct([10.0, -5.0], None) == 0.0


def test_evaluate_only_reports_account_dd_when_capital_given():
    assert evaluate([1.0] * 30)["dd_account"] is None
    assert evaluate([1.0] * 30, capital=100.0)["dd_account"] == 0.0


def test_loss_bound_ok_compares_lower_bound_to_negative_bound():
    assert loss_bound_ok(0.5, 2.0) is True     # -0.5 > -2
    assert loss_bound_ok(-1.5, 2.0) is True    # -1.5 > -2
    assert loss_bound_ok(-2.5, 2.0) is False
    assert loss_bound_ok(-3.0, -2.0) is False  # bound 取绝对值


def test_evaluate_exposes_loss_ok_only_when_bound_given():
    assert evaluate([1.0] * 30)["loss_ok"] is None
    assert evaluate([1.0] * 30, loss_bound=2.0)["loss_ok"] is True


def test_rollback_not_evaluable_below_r2_threshold():
    alerts, hit = rollback_alerts(10, -5.0, 0.9)
    assert hit is False
    assert "样本不足" in alerts[0]


def test_rollback_triggers_on_non_positive_ci_lower():
    alerts, hit = rollback_alerts(30, -0.01, 0.0)
    assert hit is True
    assert any("90%CI" in a for a in alerts)


def test_rollback_triggers_on_drawdown():
    alerts, hit = rollback_alerts(30, 1.0, 0.30)
    assert hit is True
    assert any("回撤" in a for a in alerts)


def test_rollback_clean_when_ci_positive_and_dd_small():
    alerts, hit = rollback_alerts(30, 1.0, 0.10)
    assert hit is False and alerts == []


def test_rollback_prefers_account_drawdown_when_given():
    # 峰值口径 90% 看着要回滚, 但账户口径只有 10% => 不回滚
    assert rollback_alerts(30, 1.0, 0.90, dd_account=0.10)[1] is False


def test_main_loss_bound_gives_verdict_on_tiny_sample(tmp_path, capsys):
    """路B 的意义就是小样本也能出结论(证明正期望要 261 笔)。"""
    path = _write_csv(tmp_path / "o.csv", [("a", "1000", "+1"), ("b", "2000", "-1")])
    rc = main([str(path), "--loss-bound", "3"])
    out = capsys.readouterr().out
    assert "路B判定" in out
    assert rc == 0


def test_evaluate_empty_is_insufficient():
    res = evaluate([])
    assert res["n"] == 0
    assert res["stage"] == "样本不足"
    assert res["ok"] is False


def test_evaluate_all_positive_passes_r2():
    res = evaluate([1.0] * 30)
    assert res["n"] == 30
    assert res["stage"] == "R2"
    assert res["ok"] is True


def test_evaluate_negative_sample_fails():
    res = evaluate([-1.0] * 30)
    assert res["ok"] is False
    assert res["mean"] < 0


def test_main_since_flag_splits_samples(tmp_path, capsys):
    path = _write_csv(
        tmp_path / "outcomes.csv",
        [
            ("old1", "1000", "-5"),
            ("old2", "1500", "-5"),
            ("new1", "3000", "+7"),
            ("new2", "4000", "+7"),
        ],
    )
    rc = main([str(path), "--since", "2000"])
    out = capsys.readouterr().out
    assert "n=2" in out
    assert "均+7.00U" in out
    assert rc == 1  # n=2 未达 R2 门槛, 但样本确实只剩上线后两条


def test_main_default_keeps_all_rows(tmp_path, capsys):
    path = _write_csv(
        tmp_path / "outcomes.csv",
        [("a", "1000", "+1"), ("b", "2000", "-1")],
    )
    main([str(path)])
    out = capsys.readouterr().out
    assert "n=2" in out
