"""RuntimeStateStore 语义与并发安全(与 binance_usdm_testnet 旧手写四步等价)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

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


# ── 写保护: 上一次落盘留 .bak, 主文件被清空/损坏时从备份恢复 ───────────────
# 背景(2026-09-17): state 文件 11:46 前后被重写成空, ZEC 的 guards 记录随之
# 蒸发, 04:56 SSL 事故里"record kept for restart resume"的兜底彻底失效。


def test_save_keeps_previous_state_as_backup(store, tmp_path):
    store.save({"seen": {"a": 1.0}})
    store.save({"seen": {"a": 2.0}})
    assert (tmp_path / "state" / "runtime_state.json.bak").exists()


def test_load_recovers_from_backup_when_main_file_removed(store, caplog):
    store.save({"guards": {"BTCUSDT": {"side": "BUY"}}})
    store.save({"guards": {}})
    os.remove(store._path())
    with caplog.at_level("WARNING"):
        assert store.load() == {"guards": {"BTCUSDT": {"side": "BUY"}}}
    assert "backup" in caplog.text.lower() or "备份" in caplog.text


def test_load_recovers_from_backup_when_main_file_corrupt(store):
    store.save({"guards": {"BTCUSDT": {"side": "BUY"}}})
    store.save({"guards": {}})
    with open(store._path(), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    assert store.load() == {"guards": {"BTCUSDT": {"side": "BUY"}}}


def test_load_still_raises_without_backup(store):
    path = Path(store._path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception):
        store.load()
