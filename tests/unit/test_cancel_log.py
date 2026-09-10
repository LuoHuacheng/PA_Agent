"""Tests for the append-only cancel audit log."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from pa_agent.records import cancel_log


def _read_records(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_record_cancel_appends_one_json_object_per_cancel(tmp_path: Path) -> None:
    first = cancel_log.record_cancel(
        symbol="SOLUSDT",
        client_id="pa-entry-aaa",
        reason=cancel_log.REASON_LIMIT_ENTRY_TIMEOUT,
        detail="timed out",
        environment="Testnet",
        entry_price="101.75",
        base_dir=tmp_path,
    )
    cancel_log.record_cancel(
        symbol="ETHUSDT",
        client_id="pa-entry-bbb",
        reason=cancel_log.REASON_PLAN_REPLACED,
        detail="Replaced stale limit entry",
        base_dir=tmp_path,
    )

    assert first is not None
    records = _read_records(first)
    assert len(records) == 2
    assert records[0]["symbol"] == "SOLUSDT"
    assert records[0]["client_id"] == "pa-entry-aaa"
    assert records[0]["reason"] == "limit_entry_timeout"
    assert records[0]["detail"] == "timed out"
    assert records[0]["environment"] == "Testnet"
    assert records[0]["entry_price"] == "101.75"
    assert isinstance(records[0]["ts_ms"], int)
    assert records[0]["ts_iso"].startswith("20")
    assert records[1]["reason"] == "plan_replaced"


def test_record_cancel_writes_daily_file_named_for_the_event_date(tmp_path: Path) -> None:
    stamp = dt.datetime(2026, 9, 10, 9, 32, 40).timestamp()

    path = cancel_log.record_cancel(
        symbol="SOLUSDT",
        client_id="pa-entry-aaa",
        reason=cancel_log.REASON_LIMIT_ENTRY_TIMEOUT,
        base_dir=tmp_path,
        now=stamp,
    )

    expected = dt.datetime.fromtimestamp(stamp).strftime("cancels-%Y-%m-%d.jsonl")
    assert path is not None
    assert path.name == expected
    assert path.parent == tmp_path


def test_record_cancel_rejects_unknown_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cancel_log.record_cancel(
            symbol="SOLUSDT",
            client_id="pa-entry-aaa",
            reason="because-i-said-so",
            base_dir=tmp_path,
        )


def test_record_cancel_defaults_to_configured_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cancel_log, "CANCEL_LOG_DIR", tmp_path)

    path = cancel_log.record_cancel(
        symbol="SOLUSDT",
        client_id="pa-entry-aaa",
        reason=cancel_log.REASON_CANCEL_FAILED,
    )

    assert path is not None
    assert path.parent == tmp_path


def test_record_cancel_never_breaks_the_trading_path(tmp_path: Path) -> None:
    """Audit failures must degrade to a warning: never abort an order flow."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    result = cancel_log.record_cancel(
        symbol="SOLUSDT",
        client_id="pa-entry-aaa",
        reason=cancel_log.REASON_CANCEL_FAILED,
        base_dir=blocked,
    )

    assert result is None
