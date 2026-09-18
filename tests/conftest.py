"""测试进程的落盘隔离 (2026-09-18 事故回归).

背景: `order_ledger.DEFAULT_LEDGER_DIR`, `binance_usdm_testnet._RUNTIME_STATE_PATH`,
`trade_logger.TRADE_RECORDS_DIR` 等入口都是**相对 cwd** 的路径。在仓库根目录跑 pytest
时, 单测的假单会写进真实 `records/orders` 账本, 生产 `trade_records/*_state.json`
也会被覆盖 —— guards/pending 记录蒸发, 重启后挂单 watcher 无人恢复:
BTC 裸仓 6 分 41 秒、ETH 挂单被遗弃 3 小时。

这个 autouse fixture 把上述入口一律指向当前测试的 tmp_path, 与
`tests/unit/test_prod_path_isolation.py` 的断言配套。
"""
from __future__ import annotations

import importlib

import pytest

_TARGETS = (
    ("pa_agent.trading.order_ledger", "DEFAULT_LEDGER_DIR", "records/orders"),
    (
        "pa_agent.trading.binance_usdm_testnet",
        "_RUNTIME_STATE_PATH",
        "trade_records/binance_usdm_testnet_state.json",
    ),
    ("pa_agent.trading.position_manager", "_TRADE_RECORDS_DIR", "trade_records"),
    ("pa_agent.records.trade_logger", "TRADE_RECORDS_DIR", "trade_records"),
    (
        "pa_agent.feedback.base_rate_injector",
        "OUTCOMES_CSV_PATH",
        "trade_records/outcomes.csv",
    ),
    ("pa_agent.records.cancel_log", "CANCEL_LOG_DIR", "records/cancels"),
    ("pa_agent.config.paths", "RECORDS_DIR", "records"),
    ("pa_agent.config.paths", "RECORDS_PENDING_DIR", "records/pending"),
)


@pytest.fixture(scope="session", autouse=True)
def _isolate_runtime_write_paths_session(tmp_path_factory):
    """会话级重定向, **不随用例回滚** —— leak 线程的最后一道隔离.

    2026-09-18 探针实测: 单测启动的 `_stop_watchdog_loop` 是 daemon 线程,
    函数级 monkeypatch 在 teardown 回滚后, 它的下一次 `_drop_guard` 会落到
    真实 state 文件上(真实 guards 被写成 [])。会话级直接把模块属性指向
    临时目录并**永不还原**, 这样任何活过用例的线程也只能写到临时目录。
    仓库根的 conftest 另有一道"写生产路径即报错"的硬闸门兜底。
    """
    root = tmp_path_factory.mktemp("runtime-write-paths-session")
    for module_name, attr, relative in _TARGETS:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        current = getattr(module, attr, None)
        if current is None:
            continue
        target = root / relative
        setattr(module, attr, str(target) if isinstance(current, str) else target)
    yield


@pytest.fixture(autouse=True)
def _isolate_runtime_write_paths(tmp_path, monkeypatch):
    """把运行时落盘入口重定向到 tmp_path, 单测永不碰仓库真实文件."""
    root = tmp_path.resolve()
    for module_name, attr, relative in _TARGETS:
        module = pytest.importorskip(module_name)
        target = root / relative
        current = getattr(module, attr, None)
        if isinstance(current, str):
            monkeypatch.setattr(module, attr, str(target), raising=False)
        else:
            monkeypatch.setattr(module, attr, target, raising=False)
    yield


@pytest.fixture(autouse=True)
def _restore_runtime_environment():
    """用例结束把执行环境复位, 避免测试间串台."""
    yield
    try:
        from pa_agent.trading.binance_usdm_testnet import configure_binance_environment

        # None => 回到 settings 未提供时的默认(测试网)环境, 见 configure_binance_environment.
        configure_binance_environment(None)
    except Exception:
        pass
