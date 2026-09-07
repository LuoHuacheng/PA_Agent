"""Account-snapshot poller: batched Binance reads replace per-symbol polls."""
from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from pa_agent.trading import binance_usdm_testnet as bn
from pa_agent.trading.binance_usdm_testnet import (
    BinanceAPIError,
    BinanceUSDMTestnetClient,
    current_mark_price,
    current_position,
)


class _CM:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _client_answering(payload) -> BinanceUSDMTestnetClient:
    raw = json.dumps(payload).encode()

    def opener(request, timeout):
        return _CM(raw)

    return BinanceUSDMTestnetClient("k", "s", opener=opener)


class FakeBatchedClient:
    """Client stub exposing only the batched read methods."""

    def __init__(self, marks=None, positions=None) -> None:
        self.marks = marks or {"BTCUSDT": "100.5", "ETHUSDT": "3.2"}
        self.positions = positions or {
            "BTCUSDT": {"positionAmt": "0.01", "entryPrice": "99.0"},
            "ETHUSDT": {"positionAmt": "-1.0", "entryPrice": "3.1"},
        }
        self.batch_calls = 0

    def all_mark_prices(self) -> dict[str, Decimal]:
        self.batch_calls += 1
        return {s: Decimal(str(v)) for s, v in self.marks.items()}

    def all_positions(self) -> dict[str, dict]:
        self.batch_calls += 1
        out = {}
        for symbol, row in self.positions.items():
            out[symbol] = {
                "amount": Decimal(str(row["positionAmt"])),
                "entry": Decimal(str(row["entryPrice"])),
            }
        return out


@pytest.fixture(autouse=True)
def _no_singleton(monkeypatch):
    monkeypatch.setattr(bn, "_snapshot_poller", None)
    monkeypatch.setattr(bn, "_snapshot_poller_started", False)


# ---- 批量客户端方法 ---------------------------------------------------------


def test_client_all_mark_prices_parses_batch() -> None:
    client = _client_answering(
        [
            {"symbol": "BTCUSDT", "markPrice": "100.5"},
            {"symbol": "ETHUSDT", "markPrice": "3.2"},
            {"symbol": "BROKEN", "markPrice": "not-a-number"},
        ]
    )
    prices = client.all_mark_prices()
    assert prices == {"BTCUSDT": Decimal("100.5"), "ETHUSDT": Decimal("3.2")}


def test_client_all_mark_prices_raises_when_empty() -> None:
    client = _client_answering([])
    with pytest.raises(BinanceAPIError):
        client.all_mark_prices()


def test_client_all_positions_parses_batch() -> None:
    client = _client_answering(
        [
            {"symbol": "BTCUSDT", "positionAmt": "0.01", "entryPrice": "99.0"},
            {"symbol": "ETHUSDT", "positionAmt": "-1.0", "entryPrice": "3.1"},
        ]
    )
    positions = client.all_positions()
    assert positions["BTCUSDT"]["amount"] == Decimal("0.01")
    assert positions["BTCUSDT"]["entry"] == Decimal("99.0")
    assert positions["ETHUSDT"]["amount"] == Decimal("-1.0")


# ---- 快照轮询器 -------------------------------------------------------------


def test_poller_publishes_snapshot_after_refresh() -> None:
    poller = bn.AccountSnapshotPoller(FakeBatchedClient(), clock=lambda: 1_000.0)
    poller.refresh()
    assert poller.snapshot_ready(now=1_001.0) is True
    assert poller.position("BTCUSDT") == {"amount": Decimal("0.01"), "entry": Decimal("99.0")}
    assert poller.position("SOLUSDT") is None  # 快照无此仓位 = flat
    assert poller.mark_price("ETHUSDT") == Decimal("3.2")
    assert poller.mark_price("SOLUSDT") is None


def test_poller_failed_refresh_keeps_old_snapshot_and_goes_stale() -> None:
    class Flaky(FakeBatchedClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail = False

        def all_positions(self) -> dict:
            if self.fail:
                raise BinanceAPIError("Binance HTTP 418: banned until 999")
            return super().all_positions()

    flaky = Flaky()
    poller = bn.AccountSnapshotPoller(flaky, clock=lambda: 1_000.0)
    poller.refresh()
    assert poller.snapshot_ready(now=1_001.0) is True
    flaky.fail = True
    poller.refresh()
    # 旧快照仍在但已不新鲜
    assert poller.position("BTCUSDT") is not None
    assert poller.snapshot_ready(now=1_060.0) is False  # 超过 max_age


def test_poller_loop_refreshes_until_stopped() -> None:
    client = FakeBatchedClient()
    poller = bn.AccountSnapshotPoller(client, poll_seconds=0.02)
    poller.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and client.batch_calls < 2:
            time.sleep(0.01)
        assert client.batch_calls >= 2, "background loop must refresh repeatedly"
    finally:
        poller.stop()


def test_start_stop_singleton_is_idempotent() -> None:
    assert bn.start_account_snapshot_poller(api_key="k", api_secret="s", poll_seconds=0.02) is True
    assert bn.start_account_snapshot_poller(api_key="k", api_secret="s", poll_seconds=0.02) is False
    poller = bn.account_snapshot_poller()
    assert poller is not None
    bn.stop_account_snapshot_poller()
    assert bn.account_snapshot_poller() is None
    bn.stop_account_snapshot_poller()  # 再停不炸


# ---- 守护读取: 快照优先, 回退直查 -------------------------------------------


class PlainClient:
    """Current REST-shape client used when no snapshot is ready."""

    def __init__(self) -> None:
        self.rest_calls: list[str] = []

    def position_info(self, symbol: str) -> dict:
        self.rest_calls.append(f"position:{symbol}")
        return {"amount": Decimal("0"), "entry": None}

    def mark_price(self, symbol: str) -> Decimal:
        self.rest_calls.append(f"mark:{symbol}")
        return Decimal("100.0")


class ReadyPoller:
    def snapshot_ready(self, now=None) -> bool:
        return True

    def position(self, symbol: str) -> dict | None:
        return {"amount": Decimal("0.5"), "entry": Decimal("99.0")}

    def mark_price(self, symbol: str) -> Decimal:
        return Decimal("101.0")


def test_current_position_uses_snapshot_when_ready(monkeypatch) -> None:
    client = PlainClient()
    monkeypatch.setattr(bn, "_snapshot_poller", ReadyPoller())
    info = current_position(client, "BTCUSDT")
    assert info == {"amount": Decimal("0.5"), "entry": Decimal("99.0")}
    assert client.rest_calls == [], "snapshot-ready path must not hit REST"


def test_current_position_falls_back_to_rest_when_no_poller() -> None:
    client = PlainClient()
    info = current_position(client, "BTCUSDT")
    assert info["amount"] == Decimal("0")
    assert client.rest_calls == ["position:BTCUSDT"]


def test_current_mark_price_uses_snapshot_when_ready(monkeypatch) -> None:
    client = PlainClient()
    monkeypatch.setattr(bn, "_snapshot_poller", ReadyPoller())
    assert current_mark_price(client, "BTCUSDT") == Decimal("101.0")
    assert client.rest_calls == []


def test_current_mark_price_falls_back_when_snapshot_missing_symbol(monkeypatch) -> None:
    class PartialPoller(ReadyPoller):
        def mark_price(self, symbol: str) -> Decimal | None:
            return None

    client = PlainClient()
    monkeypatch.setattr(bn, "_snapshot_poller", PartialPoller())
    assert current_mark_price(client, "BTCUSDT") == Decimal("100.0")
    assert client.rest_calls == ["mark:BTCUSDT"]


class ActionClient:
    """Guard action client: records algo cancel/place calls."""

    def __init__(self) -> None:
        self.cancels: list[str] = []
        self.places: list[dict] = []

    def cancel_algo_order(self, *, client_algo_id: str) -> None:
        self.cancels.append(client_algo_id)

    def place_close_algo_order(self, **kwargs: object) -> None:
        self.places.append(kwargs)


class SnapshotWithTrigger(ReadyPoller):
    """Fresh snapshot: open long at 100, mark 111 (1.1R fires the move)."""

    def position(self, symbol: str) -> dict | None:
        return {"amount": Decimal("0.5"), "entry": Decimal("100.0")}

    def mark_price(self, symbol: str) -> Decimal:
        return Decimal("111.0")


def test_guard_triggers_breakeven_move_from_snapshot(tmp_path, monkeypatch) -> None:
    """守护通过快照拿到持仓/mark 并完成移损, 全程零逐品种 REST。"""
    monkeypatch.setattr(bn, "_RUNTIME_STATE_PATH", str(tmp_path / "state.json"))
    bn._register_guard(
        "BTCUSDT",
        {"stop_algo_id": "pa-sl-old0001", "stop0": "90", "target": "120",
         "side": "BUY", "conf": 60, "ts": time.time(), "moved": False},
    )
    monkeypatch.setattr(bn, "_snapshot_poller", SnapshotWithTrigger())
    client = ActionClient()

    bn._breakeven_guard_loop(client=client, symbol="BTCUSDT", trigger="1r", poll_seconds=1.0)

    assert client.cancels == ["pa-sl-old0001"]
    assert client.places and client.places[0]["order_type"] == "STOP_MARKET"
    assert client.places[0]["stop_price"] == Decimal("100.0")
    record = bn._read_guard("BTCUSDT")
    assert record and record["moved"] is True


def test_poller_skips_refresh_while_banned(monkeypatch) -> None:
    """封禁期间轮询器不再请求（熔断器到期后自然恢复）。"""
    client = FakeBatchedClient()

    class BannedLimiter:
        def is_banned(self) -> bool:
            return True

    poller = bn.AccountSnapshotPoller(client, poll_seconds=0.02)
    monkeypatch.setattr(bn, "rate_limiter", BannedLimiter())
    poller.refresh()  # 启动前先拉一次成功快照
    before = client.batch_calls
    poller.start()
    try:
        time.sleep(0.15)
        assert client.batch_calls == before, "banned poller must not issue requests"
    finally:
        poller.stop()


