"""Order ledger: placements must outlive the exchange's bounded order history.

/fapi/v1/allOrders answered from 09-11 14:00 onward on a 14-day request, and a
consumed algo order has no queryable history at all, so without this ledger an
old exit cannot be attributed to the order that closed it.
"""
from __future__ import annotations

import pytest

from pa_agent.trading.order_ledger import (
    attribute_exit,
    exit_placements,
    index_by_order_id,
    iter_ledger,
    ledger_file,
    ledger_paths,
    merge_into_orders,
    order_family,
    record_order,
)

SYM = "BTCUSDT"
TS = 1_700_000_000_000
DAY_MS = 24 * 3600 * 1000


@pytest.mark.parametrize(
    ("cid", "expected"),
    [
        ("pa-entry-abc", "pa-entry"),
        ("pa-sl-abc", "pa-sl"),
        ("pa-tp-abc", "pa-tp"),
        ("pa-rollback-abc", "pa-rollback"),
        ("x-cvGenerated", "external"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_order_family_reads_the_our_own_prefixes(cid, expected) -> None:
    assert order_family(cid) == expected


def test_record_order_writes_one_line_per_placement(tmp_path) -> None:
    assert record_order(
        SYM,
        {"orderId": 11, "clientOrderId": "pa-sl-1", "type": "STOP_MARKET", "status": "NEW"},
        ledger_dir=tmp_path,
        now_ms=TS,
    )
    rows = list(iter_ledger(ledger_dir=tmp_path))
    assert len(rows) == 1
    assert rows[0]["orderId"] == 11
    assert rows[0]["family"] == "pa-sl"
    assert rows[0]["symbol"] == SYM
    assert rows[0]["recorded_ms"] == TS


def test_algo_placement_is_recorded_from_the_request(tmp_path) -> None:
    """POST /fapi/v1/algoOrder answers {"code":200,"msg":"success"} -- no ids."""
    assert record_order(
        SYM,
        {"code": 200, "msg": "success"},
        request={
            "symbol": SYM,
            "side": "SELL",
            "type": "STOP_MARKET",
            "clientAlgoId": "pa-sl-9",
            "closePosition": "true",
            "triggerPrice": "99.5",
        },
        ledger_dir=tmp_path,
        now_ms=TS,
    )
    row = next(iter(iter_ledger(ledger_dir=tmp_path)))
    assert row["clientAlgoId"] == "pa-sl-9"
    assert row["family"] == "pa-sl"
    assert row["triggerPrice"] == "99.5"


def test_record_order_returns_false_instead_of_raising(tmp_path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    assert record_order(SYM, {"orderId": 1, "clientOrderId": "pa-sl-1"}, ledger_dir=blocked) is False


def test_days_get_their_own_file_and_since_ms_filters(tmp_path) -> None:
    record_order(SYM, {"orderId": 1, "clientOrderId": "pa-sl-1"}, ledger_dir=tmp_path, now_ms=TS)
    record_order(
        SYM, {"orderId": 2, "clientOrderId": "pa-tp-2"}, ledger_dir=tmp_path, now_ms=TS + 2 * DAY_MS
    )
    assert len(ledger_paths(ledger_dir=tmp_path)) == 2
    assert len(list(iter_ledger(ledger_dir=tmp_path))) == 2
    fresh = list(iter_ledger(ledger_dir=tmp_path, since_ms=TS + DAY_MS))
    assert [row["orderId"] for row in fresh] == [2]


def test_corrupt_lines_and_blank_lines_are_skipped(tmp_path) -> None:
    record_order(SYM, {"orderId": 1, "clientOrderId": "pa-sl-1"}, ledger_dir=tmp_path, now_ms=TS)
    with ledger_file(TS, ledger_dir=tmp_path).open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n\n")
    assert len(list(iter_ledger(ledger_dir=tmp_path))) == 1


def test_index_by_order_id_maps_fills_back_to_our_client_ids(tmp_path) -> None:
    record_order(SYM, {"orderId": 5, "clientOrderId": "pa-entry-5"}, ledger_dir=tmp_path, now_ms=TS)
    record_order(SYM, {"orderId": 6, "clientOrderId": "pa-sl-6"}, ledger_dir=tmp_path, now_ms=TS + 1)
    # an algo order has no orderId, so it cannot be indexed this way
    record_order(SYM, {"clientAlgoId": "pa-sl-7"}, ledger_dir=tmp_path, now_ms=TS + 2)
    assert index_by_order_id(ledger_dir=tmp_path) == {(SYM, 5): "pa-entry-5", (SYM, 6): "pa-sl-6"}


def test_merge_into_orders_only_fills_gaps(tmp_path) -> None:
    record_order(SYM, {"orderId": 1, "clientOrderId": "pa-entry-1"}, ledger_dir=tmp_path, now_ms=TS)
    record_order(SYM, {"orderId": 2, "clientOrderId": "pa-sl-2"}, ledger_dir=tmp_path, now_ms=TS + 1)
    orders = {SYM: [{"orderId": 1, "clientOrderId": "exchange-wins", "time": 1}]}
    assert merge_into_orders(orders, ledger_dir=tmp_path) == 1
    assert [row["clientOrderId"] for row in orders[SYM]] == ["exchange-wins", "pa-sl-2"]
    # a symbol the account never traded is not invented
    assert merge_into_orders({"ETHUSDT": []}, ledger_dir=tmp_path) == 0


def test_attribute_exit_picks_the_latest_resting_exit_order(tmp_path) -> None:
    record_order(SYM, {"clientOrderId": "pa-sl-early"}, ledger_dir=tmp_path, now_ms=TS)
    record_order(SYM, {"clientOrderId": "pa-sl-late"}, ledger_dir=tmp_path, now_ms=TS + 60_000)
    placements = exit_placements(ledger_dir=tmp_path)
    assert placements[SYM] == [(TS, "pa-sl-early"), (TS + 60_000, "pa-sl-late")]
    assert attribute_exit(SYM, TS + 120_000, placements) == "pa-sl-late"
    assert attribute_exit(SYM, TS + 30_000, placements) == "pa-sl-early"
    assert attribute_exit(SYM, TS - 1, placements) == ""
    assert attribute_exit("ETHUSDT", TS + 120_000, placements) == ""
    # outside the corridor: too old to be the order that fired
    assert attribute_exit(SYM, TS + 60_000 + 2 * DAY_MS, placements) == ""


def test_entry_placements_are_not_exit_candidates(tmp_path) -> None:
    record_order(SYM, {"clientOrderId": "pa-entry-1"}, ledger_dir=tmp_path, now_ms=TS)
    assert exit_placements(ledger_dir=tmp_path) == {}


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _client(body: bytes):
    from pa_agent.trading import binance_usdm_testnet as mod

    client = mod.BinanceUSDMTestnetClient("key", "secret")
    client._opener = lambda request, timeout=None: _Resp(body)
    return mod, client


def test_successful_order_post_hits_the_ledger(monkeypatch) -> None:
    mod, client = _client(b'{"orderId": 7, "clientOrderId": "pa-entry-x"}')
    seen: list[tuple] = []
    monkeypatch.setattr(mod, "record_order", lambda sym, result, **kw: seen.append((sym, result, kw)))
    client._request_once("POST", "/fapi/v1/order", {"symbol": SYM, "side": "BUY"})
    assert len(seen) == 1
    assert seen[0][0] == SYM
    assert seen[0][1]["orderId"] == 7


def test_read_only_requests_do_not_touch_the_ledger(monkeypatch) -> None:
    mod, client = _client(b'{"symbols": []}')
    seen: list[tuple] = []
    monkeypatch.setattr(mod, "record_order", lambda *a, **kw: seen.append(a))
    client._request_once("GET", "/fapi/v1/exchangeInfo")
    assert seen == []
