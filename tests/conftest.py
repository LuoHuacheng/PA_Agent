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
import os

import pytest

# 测试进程一律无窗口。必须在任何 Qt 导入之前落定: Qt 一旦绑上 cocoa platform,
# 再改这个环境变量就失效。setdefault 保留手工覆盖 (如 QT_QPA_PLATFORM=minimal)。
# GUI 用例只验证逻辑与信号, 不需要真窗口; 开发跑 pytest 时也不再满屏弹窗抢焦点。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


#: 被 session 级隔离替换掉的真实发送函数 (只给需要验证发送逻辑的用例取回)。
_REAL_NOTIFIERS: dict[str, object] = {}


@pytest.fixture(autouse=True, scope="session")
def _silence_outbound_notifications():
    """禁止单测把任何通知真的发出去 (2026-09-19 假警报事故).

    `_alert_protection_lost` 读的是真实 settings.json(telegram.enabled=true 且有
    token), 而单测夹具用假价格(entry=100/stop0=90/mark=80)跑"放弃保护"分支 ——
    每跑一次 `pytest tests/unit` 就往聊天窗口灌一条"仓位裸奔", 与真实事故无法
    区分。告警本身是对的, 错在没有测试隔离。

    session 级: guard/runner 是 daemon 线程, 用例级 monkeypatch 在 teardown
    回滚后线程仍可能在跑(同 runtime-write-paths 的教训)。真实发送入口一律
    替换成记录器; 用例可以通过 `captured_notifications` 读它, 或通过
    `real_telegram_sender` 取回原函数。
    """
    from pa_agent.notify import telegram_notifier

    captured: list[str] = []

    def _record(text, *args, **kwargs):
        captured.append(str(text))
        return True

    original = telegram_notifier.send_telegram_message
    _REAL_NOTIFIERS["send_telegram_message"] = original
    telegram_notifier.send_telegram_message = _record
    try:
        yield captured
    finally:
        telegram_notifier.send_telegram_message = original


@pytest.fixture
def captured_notifications(monkeypatch):
    """本用例期间"发出"的通知文本 (配合 session 级记录器)."""
    from pa_agent.notify import telegram_notifier

    captured: list[str] = []

    def _record(text, *args, **kwargs):
        captured.append(str(text))
        return True

    monkeypatch.setattr(telegram_notifier, "send_telegram_message", _record)
    return captured


@pytest.fixture
def real_telegram_sender(monkeypatch):
    """取回真实的 `send_telegram_message` (仅给验证发送逻辑本身的用例).

    tests/unit/test_telegram_notifier.py 需要它; 那些用例自己 patch 了
    `requests`, 不会真的发出网络请求。
    """
    from pa_agent.notify import telegram_notifier

    monkeypatch.setattr(
        telegram_notifier,
        "send_telegram_message",
        _REAL_NOTIFIERS["send_telegram_message"],
    )


@pytest.fixture(autouse=True)
def _suppress_protection_alerts_by_default(monkeypatch):
    """干跑环境不推保护告警: 测试默认就该走这条静默路径.

    `_alert_protection_lost` 在真实启用(dry_run=false)的账户上才推送。测试里
    没有真实仓位, 默认按 dry_run 处理; 专门验证推送的用例显式再 monkeypatch
    `protection_alerts_enabled`。
    """
    try:
        from pa_agent.trading import position_manager
    except Exception:
        return
    monkeypatch.setattr(position_manager, "protection_alerts_enabled", lambda settings: False)


@pytest.fixture(autouse=True, scope="session")
def _silence_modal_dialogs():
    """测试进程里任何模态框都不得阻塞 (2026-09-22 开发弹窗事故).

    `MainWindow.showEvent` 会用 `QTimer.singleShot(0, _on_startup_api_key_check)`
    触发启动检查; e2e 夹具的 `AppContext.settings` 是 None, 于是每次都弹
    「需要配置 API Key」模态框, 紧接着再开一个 `SettingsDialog.exec()` ——
    原生平台表现为"跑 pytest 一直弹窗等你去点", offscreen 平台则永久阻塞
    (pytest-qt teardown 的 `_process_events` 卡死在 `QMessageBox.information`)。

    这里把模态框压成不阻塞的桩: 消息框一律返回默认按钮, 启动设置弹窗只记一笔。
    QDialog.exec 也一并压掉 —— 自定义弹窗 (validation_debug_dialog 的调试框等)
    同样是模态阻塞, 分析失败时它会弹出来等人点「关闭」。
    专测弹窗行为的用例自己 monkeypatch 同名属性, 会覆盖本夹具。
    """
    try:
        from PyQt6.QtWidgets import QDialog, QMessageBox

        from pa_agent.gui.main_window import MainWindow
    except ImportError:  # 无 Qt 的无头安装: 根本没有弹窗可压
        yield []
        return

    calls: list[str] = []
    _names = ("information", "warning", "critical", "question")
    originals = {name: getattr(QMessageBox, name) for name in _names}
    original_dialog_exec = QDialog.exec
    original_open_settings = MainWindow._open_settings_dialog
    original_open_ai_settings = MainWindow._open_ai_model_settings_dialog

    def _static_stub(name, result):
        def _stub(*args, **kwargs):
            calls.append(name)
            return result

        return staticmethod(_stub)

    QMessageBox.information = _static_stub("information", QMessageBox.StandardButton.Ok)
    QMessageBox.warning = _static_stub("warning", QMessageBox.StandardButton.Ok)
    QMessageBox.critical = _static_stub("critical", QMessageBox.StandardButton.Ok)
    # question 默认给 No: 没有任何测试该在无人的机器上点"确认".
    QMessageBox.question = _static_stub("question", QMessageBox.StandardButton.No)

    def _exec_stub(self, *args, **kwargs):
        calls.append("exec")
        return 0

    # QMessageBox 继承自 QDialog, 所以这一个补丁同时管住消息框实例与所有自定义
    # 弹窗 (AI 模型设置、校验调试框...). 返回 0 == 用户点了「拒绝/关闭」.
    QDialog.exec = _exec_stub

    def _open_stub(self, *args, **kwargs):
        calls.append("settings")

    MainWindow._open_settings_dialog = _open_stub
    MainWindow._open_ai_model_settings_dialog = _open_stub

    try:
        yield calls
    finally:
        for name, value in originals.items():
            setattr(QMessageBox, name, value)
        QDialog.exec = original_dialog_exec
        MainWindow._open_settings_dialog = original_open_settings
        MainWindow._open_ai_model_settings_dialog = original_open_ai_settings


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
