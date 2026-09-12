# ruff: noqa: RUF002, RUF003 - Chinese product copy
"""执行运行时状态的唯一持久化入口。

历史上 binance_usdm_testnet 的每个调用点都各自手写
"``_STATE_LOCK`` 下 load → isinstance 检查 → 改 → save"四步（29 处），
原子性与文件格式知识散落一地。RuntimeStateStore 把三者收进一个 module：

- 一把锁（调用方传入，默认自建）；
- 一种文件格式（load/原子 replace 保存，含环境状态文件名解析）；
- 一组命名空间化操作（seen/pending/guards/last_canceled_entries/halt）。

调用方只表达意图（"记下这笔挂单"），不再关心锁与格式。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class RuntimeStateStore:
    """命名空间化的运行时状态存储；所有读写自带锁与原子落盘。"""

    def __init__(
        self,
        path_provider: Callable[[], str],
        *,
        lock: threading.RLock | None = None,
    ) -> None:
        self._path_provider = path_provider
        self._lock = lock if lock is not None else threading.RLock()

    # ── 文件格式（load / 原子保存） ─────────────────────────────────────

    def _path(self) -> str:
        return self._path_provider()

    def load(self) -> dict[str, Any]:
        """Load the runtime execution state file (empty dict when absent)."""
        from pa_agent.trading.binance_usdm_testnet import BinanceAPIError

        path = self._path()
        try:
            with open(path, encoding="utf-8") as file:
                state = json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise BinanceAPIError(
                f"Cannot read execution state file {os.path.basename(path)}"
            ) from exc
        return state if isinstance(state, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        """Atomically persist the runtime execution state file."""
        from pa_agent.trading.binance_usdm_testnet import BinanceAPIError

        path = self._path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = f"{path}.tmp"
        try:
            with open(temp, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False)
            os.replace(temp, path)
        except OSError as exc:
            # Fail closed: an unavailable state must not allow new orders.
            raise BinanceAPIError(
                f"Cannot persist execution state file {os.path.basename(path)}"
            ) from exc

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """Read-modify-write under the lock; mutator mutates the state in place."""
        with self._lock:
            state = self.load()
            mutator(state)
            self.save(state)

    # ── 命名空间操作 ───────────────────────────────────────────────────

    @staticmethod
    def _namespace(state: dict[str, Any], key: str) -> dict[str, Any]:
        value = state.get(key)
        if not isinstance(value, dict):
            value = {}
            state[key] = value
        return value

    # -- seen（已成功提交的 signal_id → ts，信号去重/冷却） --

    def seen_get(self, signal_id: str) -> float | None:
        with self._lock:
            seen = self.load().get("seen")
        value = seen.get(signal_id) if isinstance(seen, dict) else None
        return value if isinstance(value, (int, float)) else None

    def seen_put(self, signal_id: str, ts: float) -> None:
        self.update(lambda state: self._namespace(state, "seen").__setitem__(signal_id, ts))

    # -- pending（symbol → 挂单中的限价入场记录） --

    def pending_get(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            pending = self.load().get("pending")
        record = pending.get(symbol) if isinstance(pending, dict) else None
        return dict(record) if isinstance(record, dict) else None

    def pending_all(self) -> dict[str, Any]:
        with self._lock:
            pending = self.load().get("pending")
        return dict(pending) if isinstance(pending, dict) else {}

    def pending_put(self, symbol: str, entry: dict[str, Any]) -> None:
        self.update(lambda state: self._namespace(state, "pending").__setitem__(symbol, entry))

    def pending_drop(self, symbol: str, client_id: str | None = None) -> None:
        """Remove the pending record for *symbol* unless it belongs to another order."""

        def _drop(state: dict[str, Any]) -> None:
            pending = state.get("pending")
            if not isinstance(pending, dict) or symbol not in pending:
                return
            record = pending[symbol]
            if client_id is not None and (
                not isinstance(record, dict) or record.get("client_id") != client_id
            ):
                return
            del pending[symbol]

        self.update(_drop)

    # -- guards（symbol → 持仓看护记录） --

    def guard_put(self, symbol: str, record: dict[str, Any]) -> None:
        self.update(lambda state: self._namespace(state, "guards").__setitem__(symbol, record))

    def guard_get(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            guards = self.load().get("guards")
        record = guards.get(symbol) if isinstance(guards, dict) else None
        return dict(record) if isinstance(record, dict) else None

    def guards_all(self) -> dict[str, Any]:
        with self._lock:
            guards = self.load().get("guards")
        return dict(guards) if isinstance(guards, dict) else {}

    def guard_patch(self, symbol: str, **patch: Any) -> bool:
        """Merge *patch* into the guard record; False when there is nothing to patch."""

        def _patch(state: dict[str, Any]) -> None:
            guards = state.get("guards")
            if not isinstance(guards, dict):
                return
            record = guards.get(symbol)
            if isinstance(record, dict):
                record.update(patch)

        with self._lock:
            before = self.guard_get(symbol)
            if before is None:
                return False
            self.update(_patch)
            return True

    def guard_drop(self, symbol: str) -> None:
        """Remove the guard/runner record for *symbol* (position is gone)."""

        def _drop(state: dict[str, Any]) -> None:
            guards = state.get("guards")
            if isinstance(guards, dict):
                guards.pop(symbol, None)

        self.update(_drop)

    # -- last_canceled_entries（symbol → 撤单锚点，限价重挂冷却） --

    def canceled_entries_all(self) -> dict[str, Any]:
        with self._lock:
            entries = self.load().get("last_canceled_entries")
        return dict(entries) if isinstance(entries, dict) else {}

    def canceled_entry_put(
        self, symbol: str, *, entry: str, reason: str, ts: float
    ) -> None:
        def _put(state: dict[str, Any]) -> None:
            entries = self._namespace(state, "last_canceled_entries")
            entries[symbol] = {"entry": entry, "reason": reason, "ts": ts}

        self.update(_put)
