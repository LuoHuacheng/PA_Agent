"""Headless terminal entry point and lifecycle controls for monitoring."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_STOP_TIMEOUT_SECONDS = 15.0
# Hard deadline for signal-driven shutdown. The main thread can block in an SSL
# read with no effective timeout (startup auto-discovery validation, or a
# stalled TradingView websocket during graceful disconnect), so stop_event is
# only observed once that read returns. The watchdog force-exits the process
# after the deadline so SIGTERM/SIGINT always stop it.
_STOP_WATCHDOG_SECONDS = 4.0


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip()


def _process_command(pid: int) -> str:
    if sys.platform == "win32":
        return _windows_process_command(pid)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _process_cwd(pid: int) -> Path | None:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:]).resolve()
    return None


def _is_monitor_process(
    command: str, project_root: Path | None = None, *, pid: int | None = None
) -> bool:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    normalized = command.replace("\\", "/")
    expected_script = str(root / "run.py").replace("\\", "/")
    entry_marker = (
        "run.py" in normalized
        or "pa-monitor" in normalized
        or "pa_agent.monitoring.cli" in normalized
    )
    has_entry = expected_script in normalized
    if pid is not None and not has_entry:
        cwd = _process_cwd(pid)
        if sys.platform == "win32":
            # Windows has no portable cwd lookup; the command line is the only evidence.
            has_entry = entry_marker
        else:
            has_entry = entry_marker and cwd is not None and cwd == root
    return has_entry and (
        "--monitor" in normalized or "pa-monitor" in normalized or "monitoring.cli" in normalized
    )


def _read_pid_record(pid_path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(pid_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        pid = int(raw["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    return raw if pid > 0 else None


def _read_pid(pid_path: Path) -> int | None:
    record = _read_pid_record(pid_path)
    return int(record["pid"]) if record is not None else None


def _is_stale_pid_record(pid_path: Path) -> bool:
    record = _read_pid_record(pid_path)
    if record is None:
        return True
    pid = int(record["pid"])
    if not _pid_exists(pid):
        return True
    command = _process_command(pid)
    # An unidentifiable live process (missing ps/lsof) must count as live so two
    # monitors cannot run under one PID file.
    return bool(command) and not _is_monitor_process(command, pid=pid)


def _acquire_monitor_pid(pid_path: Path) -> bool:
    """Atomically claim the PID file; never take it over while our monitor runs."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(pid_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if not _is_stale_pid_record(pid_path):
            return False
        try:
            pid_path.unlink()
        except FileNotFoundError:
            return False
        return _acquire_monitor_pid(pid_path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {"pid": os.getpid(), "script": str(Path(__file__).resolve().parents[2] / "run.py")},
            handle,
        )
    return True


def _release_monitor_pid(pid_path: Path) -> None:
    try:
        if _read_pid(pid_path) == os.getpid():
            pid_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("无法清理监控 PID 文件: %s", pid_path)


def _arm_stop_watchdog(delay: float, pid_path: Path | None = None) -> threading.Thread:
    """Arm a daemon hard-exit fallback for signal-driven shutdown.

    Graceful teardown can block forever (e.g. the main thread stuck in a
    TradingView SSL read while auto-discovery validates symbols during
    startup); the watchdog force-exits so SIGTERM/SIGINT always stop the
    process within the deadline. Releases the PID file first when given.
    """

    def _force_exit() -> None:
        time.sleep(delay)
        if pid_path is not None:
            _release_monitor_pid(pid_path)
        logger.error("监控未在信号关停时限内退出，强制结束进程。")  # noqa: RUF001
        os._exit(0)

    watchdog = threading.Thread(
        target=_force_exit, daemon=True, name="monitor-stop-watchdog"
    )
    watchdog.start()
    return watchdog


def stop_monitor(pid_path: Path) -> int:
    pid = _read_pid(pid_path)
    if pid is None:
        pid_path.unlink(missing_ok=True)
        print("[监控未运行] 未找到有效 PID 文件。")
        return 1
    if not _pid_exists(pid):
        # Process already gone; the PID file is stale.
        pid_path.unlink(missing_ok=True)
        print("[监控已停止] 进程已退出。")
        return 0
    command = _process_command(pid)
    if not _is_monitor_process(command, pid=pid):
        print("[监控未停止] PID 文件对应的进程不是本项目监控，未发送信号。")  # noqa: RUF001
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        if _read_pid(pid_path) == pid:
            pid_path.unlink(missing_ok=True)
        print("[监控已停止] 进程已退出。")
        return 0
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_exists(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        finally:
            if _read_pid(pid_path) == pid:
                pid_path.unlink(missing_ok=True)
        kill_deadline = time.monotonic() + 2.0
        while _pid_exists(pid) and time.monotonic() < kill_deadline:
            time.sleep(0.1)
        print("[监控已强制停止] 优雅停止超时。")
        return 0
    if _read_pid(pid_path) == pid:
        pid_path.unlink(missing_ok=True)
    print("[监控已停止]")
    return 0


def monitor_status(pid_path: Path) -> int:
    pid = _read_pid(pid_path)
    if pid is not None and _pid_exists(pid) and _is_monitor_process(_process_command(pid), pid=pid):
        print(f"[监控运行中] PID={pid}")
        return 0
    print("[监控未运行]")
    return 1


def format_decision_result(frame: Any, decision: dict | None) -> str:
    prefix = f"[决策] {frame.symbol} {frame.timeframe}"
    if not isinstance(decision, dict):
        return f"{prefix} 分析未产生有效阶段二决策"
    inner = decision.get("decision") or {}
    values = (
        inner.get("order_type", "—"),
        inner.get("order_direction", "—"),
        inner.get("trade_confidence", "—"),
        inner.get("entry_price", "—"),
        inner.get("stop_loss_price", "—"),
        inner.get("take_profit_price", "—"),
        inner.get("take_profit_price_2", "—"),
        inner.get("estimated_win_rate", "—"),
    )
    reasoning = str(inner.get("reasoning") or "").strip()
    if len(reasoning) > 180:
        reasoning = f"{reasoning[:180]}…"
    result = [
        f"{prefix} 类型={values[0]} 方向={values[1]} 置信度={values[2]} 入场={values[3]} "
        f"止损={values[4]} TP1={values[5]} TP2={values[6]} 胜率={values[7]}"
    ]
    if reasoning:
        result.append(f"理由={reasoning}")
    return "\n".join(result)


def _shutdown_monitor_process(
    monitor: Any,
    pid_path: Path,
    *,
    timeout: float,
    stop_requested: bool,
) -> None:
    finished = monitor.stop(timeout=timeout)
    _release_monitor_pid(pid_path)
    if stop_requested and not finished:
        logger.error("监控任务未能在停止超时内退出，强制结束进程。")  # noqa: RUF001
        os._exit(0)


def run_monitor() -> int:
    from pa_agent.app_context import AppContext
    from pa_agent.config.paths import MONITORING_PID_PATH, MONITORING_STATE_PATH
    from pa_agent.monitoring.service import MultiSymbolMonitor, _default_validate_symbols
    from pa_agent.util.crash_diagnostics import enable_crash_diagnostics, log_startup_diagnostics
    from pa_agent.util.logging import configure_logging

    enable_crash_diagnostics()
    configure_logging()
    log_startup_diagnostics()
    ctx = AppContext.bootstrap(connect_data_source=False, create_event_bus=False)
    settings = ctx.settings
    if settings is None or not settings.monitoring.enabled:
        logger.error("monitoring.enabled=false。请在 config/settings.json 中开启后重试。")
        return 2
    enabled_targets = [target for target in settings.monitoring.targets if target.enabled]
    if not enabled_targets and not settings.monitoring.auto_discover.enabled:
        logger.error("monitoring.targets 中没有启用的品种，且 auto_discover 未开启。")  # noqa: RUF001
        return 2
    if not _acquire_monitor_pid(MONITORING_PID_PATH):
        logger.error("监控已在运行，拒绝启动第二个实例。")  # noqa: RUF001
        return 1

    stop_event = threading.Event()
    watchdog_armed = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()
        if watchdog_armed.is_set():
            return
        watchdog_armed.set()
        _arm_stop_watchdog(_STOP_WATCHDOG_SECONDS, MONITORING_PID_PATH)

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    monitor: MultiSymbolMonitor | None = None
    try:
        monitor = MultiSymbolMonitor(
            ctx=ctx,
            settings=settings,
            state_path=MONITORING_STATE_PATH,
            validate_symbols=(
                lambda symbols: _default_validate_symbols(symbols, settings)
            ),
            on_result=lambda frame, decision: logger.info(format_decision_result(frame, decision)),
            on_status=lambda _message: None,
        )
        monitor.start()
        while monitor.is_running and not stop_event.wait(0.5):
            pass
        return 0
    finally:
        if monitor is not None:
            _shutdown_monitor_process(
                monitor,
                MONITORING_PID_PATH,
                timeout=2.0,
                stop_requested=stop_event.is_set(),
            )
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from pa_agent.config.paths import MONITORING_PID_PATH

    command = args[0] if args else "start"
    if command == "start":
        return run_monitor()
    if command == "stop":
        return stop_monitor(MONITORING_PID_PATH)
    if command == "status":
        return monitor_status(MONITORING_PID_PATH)
    if command == "pnl":
        return _pnl_command(args[1:])
    print("用法: pa-monitor [start|stop|status|pnl]", file=sys.stderr)
    return 2


def _pnl_command(args: list[str]) -> int:
    """pa-monitor pnl [--days N] [--csv PATH] [--tz H] — read-only P&L report."""
    days = 10
    csv_path: str | None = None
    tz_hours = 8.0
    index = 0
    while index < len(args):
        flag = args[index]
        if flag == "--days" and index + 1 < len(args):
            days = max(1, int(args[index + 1]))
            index += 2
        elif flag == "--csv" and index + 1 < len(args):
            csv_path = args[index + 1]
            index += 2
        elif flag == "--tz" and index + 1 < len(args):
            tz_hours = float(args[index + 1])
            index += 2
        else:
            print(f"未知参数: {flag}", file=sys.stderr)
            print("用法: pa-monitor pnl [--days N] [--csv PATH] [--tz H]", file=sys.stderr)
            return 2
    try:
        from pa_agent.config.paths import SETTINGS_JSON_PATH
        from pa_agent.config.settings import load_settings
        from pa_agent.trading.binance_usdm_testnet import report_daily_pnl

        settings = load_settings(SETTINGS_JSON_PATH)
        report_daily_pnl(days=days, tz_hours=tz_hours, csv_path=csv_path, settings=settings)
    except Exception as exc:
        print(f"[盈亏统计失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
