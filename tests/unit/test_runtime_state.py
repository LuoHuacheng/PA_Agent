"""RuntimeStateStore 语义与并发安全(与 binance_usdm_testnet 旧手写四步等价)."""
from __future__ import annotations

import threading

import pytest

from pa_agent.trading.runtime_state import RuntimeStateStore


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "state" / "runtime_state.json"
    return RuntimeStateStore(lambda: str(path))


def test_load_missing_file_returns_empty_dict(store):
    assert store.load() == {}


def test_save_then_load_roundtrip(store):
    store.save({"seen": {"abc": 1.0}})
    assert store.load() == {"seen": {"abc": 1.0}}


def test_pending_put_get_drop(store):
    store.pending_put("BTCUSDT", {"client_id": "c1", "entry": "100"})
    assert store.pending_get("BTCUSDT") == {"client_id": "c1", "entry": "100"}
    # 另一笔订单的记录不得被误删
    store.pending_drop("BTCUSDT", client_id="other")
    assert store.pending_get("BTCUSDT") is not None
    store.pending_drop("BTCUSDT", client_id="c1")
    assert store.pending_get("BTCUSDT") is None
    # 无 client_id 时无条件删
    store.pending_put("ETHUSDT", {"client_id": "c2"})
    store.pending_drop("ETHUSDT")
    assert store.pending_get("ETHUSDT") is None


def test_guard_patch_and_drop(store):
    assert store.guard_patch("BTCUSDT", moved=True) is False
    store.guard_put("BTCUSDT", {"qty": "1"})
    assert store.guard_patch("BTCUSDT", moved=True) is True
    assert store.guard_get("BTCUSDT") == {"qty": "1", "moved": True}
    store.guard_drop("BTCUSDT")
    assert store.guard_get("BTCUSDT") is None


def test_concurrent_seen_put_never_loses_writes(store):
    def _put(i: int) -> None:
        store.seen_put(f"sig-{i}", float(i))

    threads = [threading.Thread(target=_put, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seen = store.load().get("seen", {})
    assert len(seen) == 24
