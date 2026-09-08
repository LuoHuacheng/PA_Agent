"""Unit tests for the A2 outcome store (merge, rows, audit, idempotency).

Covers the v2 contract in docs/superpowers/plans/2026-09-08-A2-outcome-store-review.md:
S1 account cache -> S2 pending JSON -> S3 CSV fallback; uid idempotency;
outcome classification (net==0 -> loss); close_reason best-effort prefix
attribution; audit counts incl. cross-source drift.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pa_agent.feedback.outcome_store import (
    build_audit_summary,
    build_outcome_rows,
    export_experience_cases,
    load_csv_fallback,
    load_decision_records,
    merge_all,
    prefer_pending_over_csv,
    symbol_income_diff,
    symbols_scope,
    write_audit_json,
    write_outcome_csv,
)
from pa_agent.trading.replay_pairing import cid_variants

SYM = "BTCUSDT"
TF = "15m"
# local +8 reference used by record filenames/timestamps in the fixtures
_TZ8 = timezone(timedelta(hours=8))
NOW = 1_800_000_000_000  # arbitrary fixed instant for loader windows


def _ms(days_ago: float, hours: float = 0) -> int:
    return int(NOW - days_ago * 86400_000 - hours * 3_600_000)


def _inner(entry=65000.0, stop=64976.0, target=65014.0, conf=60, otype="限价单") -> dict:
    return {
        "order_direction": "做多",
        "order_type": otype,
        "entry_price": entry,
        "stop_loss_price": stop,
        "take_profit_price": target,
        "trade_confidence": conf,
        "diagnosis_confidence": 65,
    }


def _pending_record(ts_ms: int, *, inner: dict | None = None, sym: str = SYM) -> dict:
    inner = inner if inner is not None else _inner()
    iso = "2026-09-08T10:00:00"
    return {
        "meta": {
            "timestamp_local_iso": iso,
            "timestamp_local_ms": ts_ms,
            "symbol": sym,
            "timeframe": TF,
            "bar_count": 60,
            "ai_provider": {"model": "DeepSeek-V4-Flash-0731"},
            "decision_stance": "conservative",
        },
        "stage1_diagnosis": {
            "cycle_position": "trading_range",
            "direction": "neutral",
            "detected_patterns": ["ais", "failed_breakout"],
        },
        "stage2_decision": {"decision": inner},
        "strategy_files_used": ["震荡区间交易策略.txt", "震荡区间分析识别.txt"],
        "experience_loaded": [],
    }


def _name(ts_ms: int, sym: str = SYM, tf: str = TF) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=_TZ8)
    return dt.strftime("%Y-%m-%d_%H-%M-%S") + f"_{sym}_{tf}.json"


def _write_pending(root: Path, ts_ms: int, *, sym: str = SYM) -> Path:
    rec = _pending_record(ts_ms, sym=sym)
    fp = root / _name(ts_ms, sym=sym)
    fp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return fp


def _csv_dir(tmp: Path) -> Path:
    d = tmp / "csv"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _csv_row(ts_ms: int, *, sym: str = SYM, inner: dict | None = None) -> dict:
    inner = inner if inner is not None else _inner()
    return {
        "record_time": datetime.fromtimestamp(ts_ms / 1000, tz=_TZ8).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": sym,
        "timeframe": TF,
        "order_direction": inner["order_direction"],
        "order_type": inner["order_type"],
        "entry_price": str(inner["entry_price"]),
        "stop_loss_price": str(inner["stop_loss_price"]),
        "take_profit_price": str(inner["take_profit_price"]),
        "trade_confidence": str(inner["trade_confidence"]),
        "diag_cycle_position": "trading_range",
        "diag_direction": "neutral",
    }


def _write_csv(d: Path, rows: list[dict]) -> None:
    fp = d / f"{SYM}_{TF}.csv"
    fields = sorted({k for r in rows for k in r})
    with open(fp, "w", encoding="utf-8-sig", newline="") as fh:
        import csv

        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def _orders(close_cid: str = "pa-sl-close1") -> dict[str, list[dict]]:
    return {
        SYM: [
            {"symbol": SYM, "orderId": 1, "clientOrderId": "pa-entry-abc", "time": 1000},
            {"symbol": SYM, "orderId": 2, "clientOrderId": close_cid, "time": 2000},
        ]
    }


def _fills() -> dict[str, list[dict]]:
    return {
        SYM: [
            {"symbol": SYM, "orderId": 1, "id": 1, "time": 1000, "side": "BUY",
             "qty": "1.0", "price": "100.0", "realizedPnl": "0", "commission": "0.02"},
            {"symbol": SYM, "orderId": 2, "id": 2, "time": 2000, "side": "SELL",
             "qty": "1.0", "price": "110.0", "realizedPnl": "10.0", "commission": "0.02"},
        ]
    }


def _decision_row(ts_ms: int, *, sym: str = SYM) -> dict:
    return {
        "record_ts": ts_ms,
        "symbol": sym,
        "timeframe": TF,
        "cycle_position": "trading_range",
        "direction": "neutral",
        "strategy_files": ("震荡区间交易策略.txt",),
        "patterns": ("ais", "failed_breakout"),
        "stance": "conservative",
        "model": "DeepSeek-V4-Flash-0731",
        "source": "pending",
        "record_file": "records/pending/x.json",
        "decision": _inner(),
    }


def _attached_matched(decision_idx: int = 0, *, opened_at: int = 2500,
                      closed_at: int | None = 3000) -> dict:
    return {
        "sym": SYM, "side": 1, "qty": 1.0, "entry": 100.0, "realized": 10.0,
        "fees": -0.04, "cid": "pa-entry-" + "a" * 27, "opened_at": opened_at,
        "closed_at": closed_at, "close_px": 110.0, "close_order_id": 2,
        "status": "matched", "decision_idx": decision_idx,
        "conf": 60, "stop": 99.0, "target": 103.0,
    }


# ---------------------------------------------------------------------------
# symbols_scope
# ---------------------------------------------------------------------------


def test_symbols_scope_unions_whitelist_and_csv(tmp_path):
    d = _csv_dir(tmp_path)
    (d / "ETHUSDT_15m.csv").write_text("a,b\n", encoding="utf-8")
    (d / "XRPUSDT_1m.csv").write_text("a,b\n", encoding="utf-8")
    # the outcome store artifact must never become a symbol source
    (d / "outcomes.csv").write_text("a,b\n", encoding="utf-8")
    got = symbols_scope(whitelist=["BTCUSDT"], csv_dir=d, default=["AAAUSDT"])
    assert got == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]


def test_symbols_scope_without_whitelist_uses_default_plus_csv(tmp_path):
    d = _csv_dir(tmp_path)
    (d / "ETHUSDT_15m.csv").write_text("a,b\n", encoding="utf-8")
    got = symbols_scope(whitelist=None, csv_dir=d, default=["AAAUSDT", "BBBUSDT"])
    assert got == ["AAAUSDT", "BBBUSDT", "ETHUSDT"]


def test_symbols_scope_empty_everywhere_returns_default(tmp_path):
    got = symbols_scope(whitelist=None, csv_dir=_csv_dir(tmp_path), default=["AAAUSDT"])
    assert got == ["AAAUSDT"]


# ---------------------------------------------------------------------------
# load_decision_records (S2, filename prefilter)
# ---------------------------------------------------------------------------


def test_load_decision_records_prefilters_by_date_symbol_and_type(tmp_path):
    root = tmp_path / "pending"
    root.mkdir()
    fresh = _write_pending(root, _ms(1), sym=SYM)  # 1 day old: inside window
    _write_pending(root, _ms(40), sym=SYM)         # 40 days old: excluded
    _write_pending(root, _ms(1), sym="ETHUSDT")    # other symbol: excluded
    # 不下单 record: excluded (distinct ts so it does not overwrite fresh)
    no_trade = _pending_record(_ms(2), inner={**_inner(), "order_type": "不下单"})
    (root / _name(_ms(2), sym=SYM)).write_text(json.dumps(no_trade), encoding="utf-8")
    (root / "garbage.txt").write_text("{}", encoding="utf-8")

    rows = load_decision_records(root, symbols=[SYM], days=30, now_ms=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "pending"
    assert r["record_ts"] == _ms(1)
    assert r["symbol"] == SYM and r["timeframe"] == TF
    assert r["cycle_position"] == "trading_range"
    assert r["strategy_files"] == ("震荡区间交易策略.txt", "震荡区间分析识别.txt")
    assert r["patterns"] == ("ais", "failed_breakout")
    assert r["stance"] == "conservative"
    assert r["model"] == "DeepSeek-V4-Flash-0731"
    assert r["decision"]["order_type"] == "限价单"
    assert fresh.exists()


# ---------------------------------------------------------------------------
# load_csv_fallback (S3)
# ---------------------------------------------------------------------------


def test_load_csv_fallback_normalizes_and_marks_csv_source(tmp_path):
    d = _csv_dir(tmp_path)
    _write_csv(d, [_csv_row(_ms(1)), _csv_row(_ms(40))])  # one stale row
    rows = load_csv_fallback(d, symbols=[SYM], days=30, now_ms=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "csv"
    assert r["cycle_position"] == "trading_range"
    assert r["direction"] == "neutral"
    assert r["strategy_files"] == ()
    assert r["patterns"] == ()
    assert r["decision"]["entry_price"] == 65000.0
    assert r["record_file"].startswith("BTCUSDT_15m.csv:")


# ---------------------------------------------------------------------------
# build_outcome_rows
# ---------------------------------------------------------------------------


def test_build_outcome_rows_closed_win_and_open(tmp_path):
    rows = [_decision_row(1000)]
    attached = [
        _attached_matched(0, opened_at=2000, closed_at=3000),  # realized +10 -> win
        dict(_attached_matched(0, opened_at=2500, closed_at=None)),  # open
    ]
    out = build_outcome_rows(attached, rows, _orders())
    assert len(out) == 2
    win, open_row = out
    assert win["outcome"] == "win"
    assert win["net_usdt"] == pytest.approx(9.96)  # 10.0 + (-0.04)
    assert win["risk_usdt"] == pytest.approx(1.0)  # qty 1 * |100 - 99|
    assert win["win_r"] == pytest.approx(9.96)
    assert win["close_reason"] == "stop"
    assert win["cycle_position"] == "trading_range"
    assert win["strategy_files"] == ("震荡区间交易策略.txt",)
    assert win["ts_record"] == 1000 and win["ts_open"] == 2000 and win["ts_close"] == 3000
    assert open_row["outcome"] == "open"
    assert open_row["close_reason"] is None
    # uid is deterministic and idempotent
    assert win["uid"] == hashlib.sha256(
        ("pa-entry-" + "a" * 27 + "|2000|3000").encode()
    ).hexdigest()


def test_build_outcome_rows_zero_net_counts_loss_and_close_reason_variants():
    rows = [_decision_row(1000)]

    def orders_with(close_cid: str | None) -> dict[str, list[dict]]:
        base = [{"symbol": SYM, "orderId": 1, "clientOrderId": "pa-entry-abc", "time": 1000}]
        if close_cid is not None:
            base.append({"symbol": SYM, "orderId": 2, "clientOrderId": close_cid, "time": 2000})
        return {SYM: base}

    cases = [
        (0.0, "pa-tp-x"),            # exact zero net -> loss (Q3); close via TP
        (-2.0, ""),                  # close order present without pa- prefix
        (-2.0, None),                # closing order not found at all
    ]
    built = []
    for i, (net, close_cid) in enumerate(cases):
        t = _attached_matched(0, opened_at=1000 * (i + 3), closed_at=1000 * (i + 4))
        t["realized"] = net
        t["fees"] = 0.0
        orders = orders_with(close_cid)
        built.append(build_outcome_rows([t], rows, orders)[0])
    assert built[0]["outcome"] == "loss"
    assert built[0]["close_reason"] == "tp"
    assert built[1]["close_reason"] == "structure_or_manual"
    assert built[2]["close_reason"] == "unknown"


def test_build_outcome_rows_risk_none_when_stop_missing_or_equal():
    rows = [_decision_row(1000)]
    t = _attached_matched(0, opened_at=2000, closed_at=None)
    t["stop"] = None
    out = build_outcome_rows([t], rows, _orders())
    assert out[0]["risk_usdt"] is None and out[0]["win_r"] is None
    t2 = _attached_matched(0, opened_at=3000, closed_at=4000)
    t2["stop"] = t2["entry"]  # zero risk
    out2 = build_outcome_rows([t2], rows, _orders())
    assert out2[0]["risk_usdt"] is None and out2[0]["win_r"] is None


# ---------------------------------------------------------------------------
# csv / audit writers
# ---------------------------------------------------------------------------


def test_write_outcome_csv_is_uid_idempotent(tmp_path):
    rows = [_decision_row(1000)]
    out = build_outcome_rows([_attached_matched(0)], rows, _orders())
    fp = tmp_path / "outcomes.csv"
    write_outcome_csv(out, fp)
    first = fp.read_text(encoding="utf-8")
    write_outcome_csv(out, fp)  # same rows again: no duplicates
    second = fp.read_text(encoding="utf-8")
    assert first == second
    import csv as _csv
    data_rows = list(_csv.DictReader(first.splitlines()))
    assert len(data_rows) == 1
    assert data_rows[0]["outcome"] == "win"


def test_write_audit_json(tmp_path):
    audit = {"matched": 3, "ambiguous_rows": [{"cid": "x", "candidates": 2}]}
    fp = tmp_path / "outcome_audit.json"
    write_audit_json(audit, fp)
    loaded = json.loads(fp.read_text(encoding="utf-8"))
    assert loaded == audit


# ---------------------------------------------------------------------------
# audit summary + income diff
# ---------------------------------------------------------------------------


def test_symbol_income_diff():
    income = [
        {"symbol": SYM, "incomeType": "REALIZED_PNL", "income": 10.0},
        {"symbol": SYM, "incomeType": "COMMISSION", "income": -0.04},
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": 5.0},
        # foreign symbol without any trade in this window must be ignored
        {"symbol": "FOREIGNUSDT", "incomeType": "REALIZED_PNL", "income": 99.0},
    ]
    trades = [
        {"sym": SYM, "realized": 10.0, "fees": -0.04},
        {"sym": "ETHUSDT", "realized": 5.0, "fees": 0.0},
    ]
    diff = symbol_income_diff(income, trades)
    assert diff[SYM] == pytest.approx(0.0)
    assert diff["ETHUSDT"] == pytest.approx(0.0)
    assert "FOREIGNUSDT" not in diff
    assert symbol_income_diff(income, []) == {}


def test_build_audit_summary_counts_drift(tmp_path):
    pending = [_decision_row(1000)]
    csv_rows = [_decision_row(1000)]
    csv_rows[0]["source"] = "csv"
    attach_audit = {"matched": 1, "manual": 0, "missing_decision": 1,
                    "ambiguous": 1, "ambiguous_rows": [{"cid": "y", "candidates": 1}]}
    income = [{"symbol": SYM, "incomeType": "REALIZED_PNL", "income": 10.0},
              {"symbol": SYM, "incomeType": "COMMISSION", "income": -0.04}]
    summary = build_audit_summary(
        attach_audit, pending, csv_rows,
        trades=[{"sym": SYM, "realized": 10.0, "fees": -0.04}],
        income_rows=income,
        csv_only=3, pending_only=2,
    )
    assert summary["matched"] == 1
    assert summary["ambiguous"] == 1
    assert summary["missing_decision"] == 1
    assert summary["csv_only_rows"] == 3
    assert summary["pending_only_materials"] == 2
    assert summary["income_diff_by_symbol"][SYM] == pytest.approx(0.0)
    assert summary["decisions_pending"] == 1 and summary["decisions_csv"] == 1



def test_export_experience_cases_writes_success_and_failure(tmp_path):
    rows = [_decision_row(1000)]
    orders = _orders()
    out = build_outcome_rows(
        [
            _attached_matched(0, opened_at=2000, closed_at=3000),  # +9.96R win
            dict(_attached_matched(0, opened_at=4000, closed_at=5000)),
        ],
        rows,
        orders,
    )
    out[1]["win_r"] = -1.5
    out[1]["outcome"] = "loss"
    out[1]["close_reason"] = "stop"
    exp = tmp_path / "experience"
    written = export_experience_cases(out, exp, min_abs_r=1.0)
    assert len(written) == 2
    ok = exp / "trading_range" / "success_cases"
    bad = exp / "trading_range" / "failure_cases"
    succ = list(ok.glob("*.json"))
    fail = list(bad.glob("*.json"))
    assert len(succ) == 1 and len(fail) == 1
    import json as _json

    content = _json.loads(succ[0].read_text(encoding="utf-8"))
    assert content["direction"] == "neutral"
    assert content["detected_patterns"] == ["ais", "failed_breakout"]
    assert content["result"].startswith("win +")
    # idempotent: rerun adds nothing
    assert export_experience_cases(out, exp, min_abs_r=1.0) == []


def test_export_skips_open_and_below_threshold(tmp_path):
    rows = [_decision_row(1000)]
    open_row = dict(_attached_matched(0, opened_at=2000, closed_at=None))
    weak = dict(_attached_matched(0, opened_at=3000, closed_at=4000))
    out = build_outcome_rows([open_row, weak], rows, _orders())
    out[1]["win_r"] = 0.5  # below the |1R| threshold
    assert export_experience_cases(out, tmp_path / "exp", min_abs_r=1.0) == []


def test_export_prunes_old_entries(tmp_path):
    rows = [_decision_row(1000)]
    exp = tmp_path / "exp"
    out = build_outcome_rows(
        [_attached_matched(0, opened_at=2000, closed_at=3000)], rows, _orders()
    )
    export_experience_cases(out, exp, min_abs_r=1.0, keep_per_dir=1)
    newer = build_outcome_rows(
        [_attached_matched(0, opened_at=7000, closed_at=8000)], rows, _orders()
    )
    export_experience_cases(newer, exp, min_abs_r=1.0, keep_per_dir=1)
    files = list((exp / "trading_range" / "success_cases").glob("*.json"))
    assert len(files) == 1  # the oldest was pruned



# ---------------------------------------------------------------------------
# source preference
# ---------------------------------------------------------------------------


def test_prefer_pending_over_csv_drops_near_duplicate_csv_rows():
    pending = [_decision_row(1000)]
    csv_dup = [_decision_row(1000 + 1000)]  # same material, +1s (save lag)
    csv_other = [dict(_decision_row(2000))]  # different material/time
    csv_other[0]["decision"] = dict(_decision_row(2000)["decision"])
    csv_other[0]["decision"]["entry_price"] = 66000.0
    merged = prefer_pending_over_csv(pending, csv_dup + csv_other)
    assert len(merged) == 2  # pending + the genuinely different csv row
    assert merged[0]["source"] == "pending"
    assert merged[1]["decision"]["entry_price"] == 66000.0


def test_prefer_pending_keeps_csv_outside_tolerance():
    pending = [_decision_row(1000)]
    old_csv = [_decision_row(1000 - 3600_000)]  # one hour earlier: real retry
    merged = prefer_pending_over_csv(pending, old_csv, tolerance_ms=300_000)
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# merge_all end-to-end
# ---------------------------------------------------------------------------


def test_merge_all_end_to_end(tmp_path):
    inner = _inner(entry=100.0, stop=99.0, target=103.0, otype="市价单")
    entry_cid = cid_variants(SYM, "做多", "市价单", 100.0, 99.0, 103.0)[0]
    # decision recorded shortly before the fills (attach needs record_ts <= opened_at)
    rec_ts = NOW - 120_000
    open_ts = NOW - 119_000
    close_ts = NOW - 118_000
    root = tmp_path / "pending"
    root.mkdir()
    rec = _pending_record(rec_ts, inner=inner, sym=SYM)
    (root / _name(rec_ts, sym=SYM)).write_text(json.dumps(rec), encoding="utf-8")
    cache = {
        "orders": {
            SYM: [
                {"symbol": SYM, "orderId": 1, "clientOrderId": entry_cid, "time": open_ts},
                {"symbol": SYM, "orderId": 2, "clientOrderId": "pa-sl-x", "time": close_ts},
            ]
        },
        "user_trades": {
            SYM: [
                {"symbol": SYM, "orderId": 1, "id": 1, "time": open_ts, "side": "BUY",
                 "qty": "1.0", "price": "100.0", "realizedPnl": "0", "commission": "0.02"},
                {"symbol": SYM, "orderId": 2, "id": 2, "time": close_ts, "side": "SELL",
                 "qty": "1.0", "price": "103.0", "realizedPnl": "3.0", "commission": "0.02"},
            ]
        },
        "income": [
            {"symbol": SYM, "incomeType": "REALIZED_PNL", "income": 3.0},
            {"symbol": SYM, "incomeType": "COMMISSION", "income": -0.04},
        ],
    }
    rows, audit = merge_all(
        cache, records_root=root, csv_dir=_csv_dir(tmp_path),
        symbols=[SYM], days=30, now_ms=NOW,
    )
    assert audit["matched"] == 1
    assert audit["missing_decision"] == 0 and audit["ambiguous"] == 0
    assert audit["rows_outcome"] == 1
    assert rows[0]["outcome"] == "win"
    assert rows[0]["close_reason"] == "stop"
    assert rows[0]["source"] == "pending"
    assert rows[0]["strategy_files"] == ("震荡区间交易策略.txt", "震荡区间分析识别.txt")
    assert audit["income_diff_by_symbol"][SYM] == pytest.approx(0.0)
