"""回归: 单测不得写生产落盘文件 (2026-09-18 事故).

事故: `pytest` 在仓库根目录跑时, `order_ledger.DEFAULT_LEDGER_DIR`(records/orders)
与 `binance_usdm_testnet._RUNTIME_STATE_PATH`(trade_records/...) 都是**相对 cwd** 的
路径, 于是单测把假单写进真实账本、把生产 state 文件覆盖掉(guards/pending 蒸发),
导致重启后挂单 watcher 无人恢复 —— BTC 裸仓 6 分钟、ETH 挂单被遗弃。

这里的断言是"测试进程里, 这些落盘入口一律不许指向仓库真实目录"。
"""
from __future__ import annotations

import json
from pathlib import Path

from pa_agent.config.paths import PROJECT_ROOT
from pa_agent.feedback import base_rate_injector
from pa_agent.records import cancel_log, trade_logger
from pa_agent.trading import binance_usdm_testnet, order_ledger, position_manager


def _resolved(target: object) -> Path:
    path = Path(str(target))
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def _inside_repo(target: object) -> bool:
    try:
        _resolved(target).relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    return True


def test_write_targets_are_redirected_out_of_repo() -> None:
    """所有生产落盘入口在测试里都必须被重定向到仓库之外."""
    targets = {
        "order_ledger.DEFAULT_LEDGER_DIR": order_ledger.DEFAULT_LEDGER_DIR,
        "binance_usdm_testnet._RUNTIME_STATE_PATH": binance_usdm_testnet._RUNTIME_STATE_PATH,
        "trade_logger.TRADE_RECORDS_DIR": trade_logger.TRADE_RECORDS_DIR,
        "position_manager._TRADE_RECORDS_DIR": position_manager._TRADE_RECORDS_DIR,
        "base_rate_injector.OUTCOMES_CSV_PATH": base_rate_injector.OUTCOMES_CSV_PATH,
        "cancel_log.CANCEL_LOG_DIR": cancel_log.CANCEL_LOG_DIR,
    }
    leaked = sorted(name for name, target in targets.items() if _inside_repo(target))
    assert leaked == [], f"测试进程仍在写仓库真实目录: {leaked}"


def test_record_order_never_touches_real_ledger() -> None:
    """写一条订单账本行, 真实 records/orders 目录的 mtime/文件集不变."""
    real_dir = PROJECT_ROOT / "records" / "orders"
    before = sorted(p.name for p in real_dir.glob("orders-*.jsonl"))
    before_mtime = {p.name: p.stat().st_mtime_ns for p in real_dir.glob("orders-*.jsonl")}

    written = order_ledger.record_order(
        "BTCUSDT",
        {"orderId": 1, "clientOrderId": "pa-sl-isolation-check", "side": "SELL"},
    )
    assert written is True

    after = sorted(p.name for p in real_dir.glob("orders-*.jsonl"))
    after_mtime = {p.name: p.stat().st_mtime_ns for p in real_dir.glob("orders-*.jsonl")}
    assert after == before, f"单测新增了真实账本文件: {set(after) - set(before)}"
    assert after_mtime == before_mtime, "单测改写了真实账本文件"


def test_state_write_never_touches_real_state() -> None:
    """写一次运行时 state, 真实 trade_records state 文件内容不变."""
    real_state = PROJECT_ROOT / "trade_records" / binance_usdm_testnet._RUNTIME_STATE_PATH
    fallback = PROJECT_ROOT / "trade_records" / "binance_usdm_testnet_state.json"
    target = real_state if real_state.is_file() else fallback
    before = target.read_bytes() if target.is_file() else None

    binance_usdm_testnet._STATE_STORE.pending_put(
        "BTCUSDT", {"client_id": "pa-entry-isolation-check", "entry": "100"}
    )

    after = target.read_bytes() if target.is_file() else None
    assert after == before, "单测改写了生产 state 文件"
    if before is not None:
        payload = json.loads(after.decode("utf-8"))
        assert "pa-entry-isolation-check" not in json.dumps(payload)
