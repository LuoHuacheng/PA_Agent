"""Integration tests for Orchestrator + PreflightDataGate (Task 3).

Property 1b: data insufficient → zero AI calls, record.exception.type=="insufficient_data".
"""
from __future__ import annotations

from unittest.mock import MagicMock, call

import httpx
import pytest

from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken, OrchestratorEvent


def _make_bar(seq: int) -> KlineBar:
    return KlineBar(
        seq=seq, ts_open=float(1_000_000 - seq * 60_000),
        open=2000.0, high=2010.0, low=1990.0, close=2005.0,
        volume=1.0, closed=True,
    )


def _insufficient_frame_19bars() -> KlineFrame:
    n = 19
    bars = tuple(_make_bar(i + 1) for i in range(n))
    return KlineFrame(
        symbol="TEST", timeframe="1h", bars=bars, snapshot_ts_local_ms=1,
        indicators=IndicatorBundle(ema20=tuple([2000.0] * n), atr14=tuple([10.0] * n)),
    )


def _insufficient_frame_empty() -> KlineFrame:
    return KlineFrame(
        symbol="TEST", timeframe="1h", bars=(), snapshot_ts_local_ms=1,
        indicators=IndicatorBundle(ema20=(), atr14=()),
    )


def _insufficient_frame_all_nan() -> KlineFrame:
    n = 20
    bars = tuple(_make_bar(i + 1) for i in range(n))
    return KlineFrame(
        symbol="TEST", timeframe="1h", bars=bars, snapshot_ts_local_ms=1,
        indicators=IndicatorBundle(
            ema20=tuple([float("nan")] * n),
            atr14=tuple([float("nan")] * n),
        ),
    )


def _make_orchestrator():
    """Build orchestrator with mocked AI client and writer."""
    client = MagicMock()
    assembler = MagicMock()
    assembler.build_stage1.return_value = [{"role": "user", "content": "test"}]
    router = MagicMock(return_value=[])
    validator = MagicMock()
    writer = MagicMock()
    exp_reader = MagicMock()
    exp_reader.read_for_stage2.return_value = []
    exp_reader.read_top5.return_value = []

    orch = TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=router,
        validator=validator,
        pending_writer=writer,
        exp_reader=exp_reader,
        settings=None,
    )
    return orch, client, assembler, writer


@pytest.mark.parametrize("frame_factory,expected_check", [
    (_insufficient_frame_19bars, "bar_count_lt_20"),
    (_insufficient_frame_empty, "bars_empty_or_bad_ohlc"),
    (_insufficient_frame_all_nan, "indicators_all_nan"),
])
def test_insufficient_data_zero_ai_calls(frame_factory, expected_check):
    """Property 1b: insufficient data → zero AI calls, record.exception.type == insufficient_data."""
    orch, client, assembler, writer = _make_orchestrator()
    frame = frame_factory()
    cancel_token = CancelToken()
    events = []

    record = orch.submit(frame, cancel_token, lambda e: events.append(e))

    # Zero AI calls
    client.stream_chat.assert_not_called()
    assembler.build_stage1.assert_not_called()

    # Record has correct exception
    assert record.exception is not None
    assert record.exception["type"] == "insufficient_data"
    assert record.exception["failed_check"] == expected_check
    assert record.stage1_response is None
    assert record.stage1_diagnosis is None

    # Event emitted
    assert OrchestratorEvent.InsufficientData in events

    # save_partial called with "insufficient_data"
    writer.save_partial.assert_called()
    args = writer.save_partial.call_args[0]
    assert args[1] == "insufficient_data"


def test_insufficient_data_no_stage2_ai_call():
    """Stage2 AI is also not called for insufficient data."""
    orch, client, assembler, writer = _make_orchestrator()
    frame = _insufficient_frame_19bars()
    cancel_token = CancelToken()

    record = orch.submit(frame, cancel_token, lambda e: None)

    # No stage2 calls either
    client.stream_chat.assert_not_called()
    assert record.stage2_decision is None
    assert record.stage2_response is None


def test_insufficient_data_record_exception_type_distinct():
    """Verify insufficient_data record can be distinguished from other error types."""
    orch, client, assembler, writer = _make_orchestrator()
    frame = _insufficient_frame_19bars()

    record = orch.submit(frame, CancelToken(), lambda e: None)

    exc = record.exception
    assert exc["type"] == "insufficient_data"
    assert exc["type"] != "network_error"
    assert exc["type"] != "validation_error"
    assert exc["stage"] == "preflight"


def _ok_frame_20bars() -> KlineFrame:
    """Enough valid closed bars/indicators to pass the preflight data gate."""
    n = 20
    bars = tuple(_make_bar(i + 1) for i in range(n))
    return KlineFrame(
        symbol="TEST", timeframe="1h", bars=bars, snapshot_ts_local_ms=1,
        indicators=IndicatorBundle(ema20=tuple([2000.0] * n), atr14=tuple([10.0] * n)),
    )


def _s1_failed_previous_record():
    """Record shaped like a real Stage-1 network-error partial (reply None)."""
    from pa_agent.records.schema import AnalysisRecord, RecordMeta

    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-01T00:00:00.000",
            timestamp_local_ms=1,
            symbol="TEST",
            timeframe="1h",
            bar_count=20,
            ai_provider={},
        ),
        kline_data=[],
        htf_text="",
        stage1_messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        stage1_response=None,
        stage1_diagnosis=None,
        stage2_messages=[],
        stage2_response=None,
        stage2_decision=None,
        strategy_files_used=[],
        experience_loaded=[],
        exception={
            "type": "network_error",
            "stage": "stage1",
            "message": "incomplete chunked read",
        },
        usage_total={},
    )


def test_failed_previous_record_falls_back_to_full_stage1():
    """Regression: failed previous record must NOT trigger incremental Stage 1.

    Monitor poll after a network-error record previously raised
    "previous_record.stage1_response has no 'content' field" on every new bar,
    permanently stalling the symbol. The orchestrator must rebuild via full
    Stage 1 instead of chaining off the failed record.
    """
    orch, client, assembler, _ = _make_orchestrator()
    assembler.build_stage1.return_value = [{"role": "user", "content": "full"}]
    assembler.build_incremental_stage1.return_value = [
        {"role": "user", "content": "incr"},
    ]
    assembler.can_build_incremental_stage1.return_value = False  # failed record
    client.stream_chat.side_effect = httpx.RemoteProtocolError("incomplete chunked read")

    record = orch.submit(
        _ok_frame_20bars(),
        CancelToken(),
        lambda e: None,
        previous_record=_s1_failed_previous_record(),
        incremental_new_bar_count=1,
    )

    # Full Stage 1 rebuild was used, not incremental chaining
    assembler.build_stage1.assert_called_once()
    assembler.build_incremental_stage1.assert_not_called()
    # Network error still surfaces as a network_error partial record
    assert record.exception is not None
    assert record.exception["type"] == "network_error"


def test_successful_previous_record_keeps_incremental_stage1():
    """Successful previous record keeps the incremental fast path."""
    orch, client, assembler, _ = _make_orchestrator()
    assembler.build_stage1.return_value = [{"role": "user", "content": "full"}]
    assembler.build_incremental_stage1.return_value = [
        {"role": "user", "content": "incr"},
    ]
    assembler.can_build_incremental_stage1.return_value = True
    client.stream_chat.side_effect = httpx.RemoteProtocolError("incomplete chunked read")

    record = orch.submit(
        _ok_frame_20bars(),
        CancelToken(),
        lambda e: None,
        previous_record=_s1_failed_previous_record(),
        incremental_new_bar_count=1,
    )

    assembler.build_incremental_stage1.assert_called_once()
    assembler.build_stage1.assert_not_called()
    assert record.exception is not None
    assert record.exception["type"] == "network_error"
