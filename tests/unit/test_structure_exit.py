"""Unit tests for structure-failure auto exit evaluation."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from pa_agent.config.settings import BinanceUSDMTestnetSettings
from pa_agent.trading.structure_exit import evaluate_structure_failure_exit


def _settings(mode: str, confirm: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        binance_usdm_testnet=BinanceUSDMTestnetSettings(
            enabled=True,
            structure_exit_mode=mode,
            structure_exit_confirm_bars=confirm,
        )
    )


class FakeClient:
    def __init__(self, amount: str = "8973", entry: str = "0.2229", fail_close: bool = False):
        self.amount = amount
        self.entry = entry
        self.fail_close = fail_close
        self.closed: list[tuple[str, str, Decimal]] = []

    def position_info(self, symbol: str) -> dict:
        return {"amount": Decimal(self.amount), "entry": Decimal(self.entry)}

    def close_market_position(self, *, symbol: str, side: str, quantity: Decimal) -> None:
        if self.fail_close:
            raise RuntimeError("boom")
        self.closed.append((symbol, side, quantity))


def _frame(close: float) -> SimpleNamespace:
    return SimpleNamespace(
        bars=[
            SimpleNamespace(
                seq=1,
                open=0.2228,
                high=0.2231,
                low=0.2201,
                close=close,
                volume=1,
                closed=True,
            )
        ]
    )


def _record(direction: str) -> SimpleNamespace:
    return SimpleNamespace(stage1_diagnosis={"direction": direction})


_ROW = {
    "record_time": "2026-09-06 12:00:01",
    "order_direction": "做多",
    "entry_price": "0.2229",
    "stop_loss_price": "0.2210",
}


def _run(
    *,
    mode: str,
    direction: str,
    close: float,
    counts: dict | None = None,
    fail_close: bool = False,
    row: dict | None = _ROW,
):
    if counts is None:
        counts = {}
    client = FakeClient(fail_close=fail_close)
    notes: list[str] = []
    verdict = evaluate_structure_failure_exit(
        symbol="ADAUSDT",
        timeframe="15m",
        record=_record(direction),
        frame=_frame(close),
        settings=_settings(mode),
        counts=counts,
        client=client,
        decision_row=row,
        notify=notes.append,
    )
    return verdict, client, counts, notes


def test_off_mode_never_acts():
    verdict, client, counts, _ = _run(mode="off", direction="bearish", close=0.2215)
    assert verdict["action"] == "none"
    assert client.closed == []
    assert counts == {}


def test_two_consecutive_negation_bars_required():
    counts: dict = {}
    v1, _, counts, _ = _run(
        mode="dry_run", direction="bearish", close=0.2215, counts=counts
    )
    assert v1["action"] == "none"  # streak 1 < confirm 2
    v2, client, counts, notes = _run(
        mode="dry_run", direction="bearish", close=0.2215, counts=counts
    )
    assert v2["action"] == "dry_exit"
    assert client.closed == []  # dry-run never trades
    assert notes and "结构否定" in notes[-1]
    assert counts["ADAUSDT"]["n"] == 2


def test_on_mode_closes_position_after_confirmation():
    counts: dict = {}
    _run(mode="on", direction="bearish", close=0.2215, counts=counts)
    verdict, client, _, _ = _run(
        mode="on", direction="bearish", close=0.2215, counts=counts
    )
    assert verdict["action"] == "exit"
    assert client.closed == [("ADAUSDT", "SELL", Decimal("8973"))]
    assert counts == {}  # streak cleared after close


def test_same_direction_resets_streak():
    counts = {"ADAUSDT": {"dir": "bearish", "n": 1}}
    verdict, _, counts, _ = _run(
        mode="dry_run", direction="bullish", close=0.2215, counts=counts
    )
    assert verdict["action"] == "none"
    assert counts["ADAUSDT"]["n"] == 0


def test_close_above_entry_no_exit_while_profitable_zone():
    counts = {"ADAUSDT": {"dir": "bearish", "n": 2}}
    verdict, client, _, _ = _run(
        mode="dry_run", direction="bearish", close=0.2235, counts=counts
    )
    assert verdict["action"] == "none"
    assert client.closed == []


def test_close_at_static_stop_left_to_protective_order():
    counts = {"ADAUSDT": {"dir": "bearish", "n": 2}}
    verdict, client, _, _ = _run(
        mode="dry_run", direction="bearish", close=0.2209, counts=counts
    )
    assert verdict["action"] == "none"  # close <= stop: static stop owns it
    assert client.closed == []


def test_flat_position_clears_streak():
    counts = {"ADAUSDT": {"dir": "bearish", "n": 2}}
    client = FakeClient(amount="0")
    verdict = evaluate_structure_failure_exit(
        symbol="ADAUSDT",
        timeframe="15m",
        record=_record("bearish"),
        frame=_frame(0.2215),
        settings=_settings("dry_run"),
        counts=counts,
        client=client,
        decision_row=_ROW,
    )
    assert verdict["action"] == "none"
    assert counts == {}


def test_unmatched_signal_row_skipped():
    counts = {"ADAUSDT": {"dir": "bearish", "n": 2}}
    row = dict(_ROW)
    row["order_direction"] = "做空"
    verdict, client, _, _ = _run(
        mode="on", direction="bearish", close=0.2215, counts=counts, row=row
    )
    assert verdict["action"] == "none"
    assert client.closed == []


def test_short_position_symmetric():
    counts: dict = {}
    row = {
        "record_time": "2026-09-06 12:00:01",
        "order_direction": "做空",
        "entry_price": "0.2198",
        "stop_loss_price": "0.2206",
    }
    client = FakeClient(amount="-4585", entry="0.2198")

    def run_short():
        return evaluate_structure_failure_exit(
            symbol="ADAUSDT",
            timeframe="15m",
            record=_record("bullish"),
            frame=_frame(0.2201),
            settings=_settings("on"),
            counts=counts,
            client=client,
            decision_row=row,
        )

    assert run_short()["action"] == "none"  # streak 1
    verdict = run_short()
    assert verdict["action"] == "exit"
    assert client.closed == [("ADAUSDT", "BUY", Decimal("4585"))]


def test_close_failure_reported():
    counts: dict = {}
    _run(mode="on", direction="bearish", close=0.2215, counts=counts)
    verdict, _, _, _ = _run(
        mode="on",
        direction="bearish",
        close=0.2215,
        counts=counts,
        fail_close=True,
    )
    assert verdict["action"] == "failed"
    assert "boom" in str(verdict["reason"])
