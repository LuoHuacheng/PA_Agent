"""Regression tests for the record filename format (PendingWriter).

Bug (found 2026-09-21): the basename was built with
strftime("%Y-%m-%d_%H-%m-%S") where %m is the **month**, not the minute.
Filenames therefore lost the minute, so two analysis rounds inside the same
hour whose meta timestamp happened to share the same second produced
byte-identical paths and the later save silently overwrote the earlier record.

Observed damage between 2026-09-20 20:41 and 2026-09-21 11:00: 130 record files
for 145 completed rounds - 15 records overwritten, all of them in the
first-wave symbols (BTC/ETH/SOL) whose poll starts at a deterministic second.

These tests pin the format: hour **and minute** must be encoded.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from pa_agent.records.pending_writer import PendingWriter, build_record_basename


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _meta(dt: datetime, symbol: str = "BTCUSDT", timeframe: str = "30m"):
    return SimpleNamespace(
        timestamp_local_ms=_ms(dt),
        timestamp_local_iso=dt.isoformat(),
        symbol=symbol,
        timeframe=timeframe,
    )


class _StubRecord:
    """Minimal duck-typed record: PendingWriter only needs meta + model_dump()."""

    def __init__(self, meta) -> None:
        self.meta = meta

    def model_dump(self) -> dict:
        return {
            "meta": {
                "timestamp_local_ms": self.meta.timestamp_local_ms,
                "symbol": self.meta.symbol,
                "timeframe": self.meta.timeframe,
            }
        }


class TestBasenameEncodesMinute:
    def test_basename_matches_iso_like_format(self):
        dt = datetime(2026, 9, 21, 3, 30, 9, 639000)
        assert build_record_basename(_meta(dt)) == "2026-09-21_03-30-09_BTCUSDT_30m"

    def test_minute_field_is_the_minute_not_the_month(self):
        # A September timestamp made the old format look plausible: the month
        # digit (09) sat exactly where the minute belonged.
        early = datetime(2026, 9, 21, 3, 2, 9, 639000)
        late = datetime(2026, 9, 21, 3, 47, 9, 639000)
        assert build_record_basename(_meta(early)).endswith("_03-02-09_BTCUSDT_30m")
        assert build_record_basename(_meta(late)).endswith("_03-47-09_BTCUSDT_30m")


class TestTwoRoundsNeverCollide:
    def test_same_hour_same_second_rounds_are_distinct(self):
        first = _meta(datetime(2026, 9, 21, 3, 0, 9, 639000))
        second = _meta(datetime(2026, 9, 21, 3, 30, 9, 639000))
        assert build_record_basename(first) != build_record_basename(second)

    def test_saving_both_rounds_keeps_both_files(self, tmp_path: Path):
        writer = PendingWriter(pending_dir=tmp_path)
        writer.save_full(_StubRecord(_meta(datetime(2026, 9, 21, 3, 0, 9, 639000))))
        writer.save_full(_StubRecord(_meta(datetime(2026, 9, 21, 3, 30, 9, 639000))))
        assert sorted(p.name for p in tmp_path.glob("*.json")) == [
            "2026-09-21_03-00-09_BTCUSDT_30m.json",
            "2026-09-21_03-30-09_BTCUSDT_30m.json",
        ]
