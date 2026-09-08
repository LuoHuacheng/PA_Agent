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
    # --- Binance 执行环境 (testnet/live) ---
    # environment 决定 REST/WS 网关、运行时状态文件与通知标签。
    # 配置冲突(双环境同开 / 实盘缺密钥)直接拒绝启动, 不留半启动进程。
    from pa_agent.trading import binance_env
    from pa_agent.trading.binance_usdm_testnet import configure_binance_environment

    exec_env = binance_env.resolve_env(settings)
    binance_cfg = binance_env.active_cfg(settings)
    conflict = binance_env.env_conflicts(settings)
    if conflict:
        logger.error("Binance 环境配置错误: %s", conflict)
        return 2
    configure_binance_environment(settings)
    logger.info("Binance 执行环境: %s (%s)", exec_env.label_zh, exec_env.key)
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
    # WS 健康状态由下方 user-data 流回调维护; 快照轮询器据此切换周期:
    # 流在线 -> 对账放宽到 300s, 流断开 -> 自动回落基础周期(60s)兜底.
    ws_stream_health = {"connected": False}
    _WS_HEALTHY_POLL_SECONDS = 300.0

    try:
        from pa_agent.trading.binance_usdm_testnet import start_account_snapshot_poller

        # 账户快照轮询器先行: 守护线程/结构退出检查都从批量快照读取,
        # 每周期 2 个批量请求取代 N 个品种的逐符号轮询(共享 IP 限流缓解)。
        if binance_cfg.enabled and (binance_cfg.api_key or "").strip():
            base_poll = float(binance_cfg.breakeven_poll_seconds)

            def _snapshot_poll_period() -> float:
                return _WS_HEALTHY_POLL_SECONDS if ws_stream_health["connected"] else base_poll

            start_account_snapshot_poller(
                api_key=binance_cfg.api_key,
                api_secret=binance_cfg.api_secret,
                base_url=exec_env.rest_base,
                poll_seconds=base_poll,
                poll_period_provider=_snapshot_poll_period,
                stale_after_seconds=(
                    float(binance_cfg.snapshot_stale_seconds)
                    if binance_cfg.snapshot_stale_seconds > 0
                    else None
                ),
            )
    except Exception:
        logger.exception("启动账户快照轮询器失败")
    # A previous run may have died while a limit entry rested on the exchange:
    # re-arm its fill watcher so a later fill still gets SL/TP attached.
    try:
        from pa_agent.trading.binance_usdm_testnet import (
            resume_breakeven_guards,
            resume_pending_limit_watchers,
            resume_time_stops,
            resume_tp_runners,
        )

        resume_pending_limit_watchers(settings)
        resume_breakeven_guards(settings)
        resume_time_stops(settings)
        resume_tp_runners(settings)
    except Exception:
        logger.exception("恢复 Binance 测试网挂单 watcher 失败")
    # user-data websocket: 订单/账户事件推送替代常驻轮询. REST 快照轮询保留
    # 作为低频对账兜底, 且周期随 WS 健康状态自适应(在线 300s / 断开回落 60s).
    # 事件处理器跑在 WS 线程, 只做"节流后触发快照刷新 + 唤醒 fill watcher",
    # 不碰下单路径; 断开/重连通过 Telegram 通知(10 分钟冷却防刷屏).
    user_stream: Any | None = None
    mark_stream: Any | None = None
    try:
        if binance_cfg.enabled and binance_cfg.user_data_stream_enabled:
            from pa_agent.trading.binance_usdm_testnet import (
                BinanceUSDMTestnetClient,
                account_snapshot_poller,
                notify_user_order_update,
            )
            from pa_agent.trading.binance_user_data import (
                BinanceMarkPriceStream,
                BinanceUserDataStream,
                UserDataEventHandlers,
            )

            stream_client = BinanceUSDMTestnetClient(
                binance_cfg.api_key,
                binance_cfg.api_secret,
                base_url=exec_env.rest_base,
            )
            ws_url = (
                str(binance_cfg.user_data_stream_ws_url or "").strip() or exec_env.ws_base
            )
            event_refresh_gap = float(
                binance_cfg.user_data_event_refresh_gap_seconds
            )
            last_event_ts = [0.0]
            reconnect_count = [0]
            notice_cooldown_ts = [0.0]

            def _refresh_snapshot_after_event() -> None:
                now = time.time()
                if now - last_event_ts[0] < event_refresh_gap:
                    return
                last_event_ts[0] = now
                poller = account_snapshot_poller()
                if poller is not None:
                    try:
                        poller.refresh()
                    except Exception:
                        logger.exception("WS 事件触发的快照刷新失败")

            def _on_order_update(msg: dict) -> None:
                notify_user_order_update(msg)
                _refresh_snapshot_after_event()

            def _send_stream_notice(text: str) -> None:
                now = time.time()
                if now - notice_cooldown_ts[0] < 600.0:
                    return
                notice_cooldown_ts[0] = now
                try:
                    from pa_agent.notify.telegram_notifier import send_telegram_message

                    send_telegram_message(f"[监控] {text}", settings=settings)
                except Exception:
                    logger.exception("发送 user-data 流告警失败")

            def _on_connected() -> None:
                ws_stream_health["connected"] = True
                reconnect_count[0] += 1
                if reconnect_count[0] > 1:
                    _send_stream_notice(
                        f"user-data 流已重连(第 {reconnect_count[0]} 次)"
                    )
                _refresh_snapshot_after_event()

            def _on_disconnected() -> None:
                ws_stream_health["connected"] = False
                if not stop_event.is_set():  # 正常停止不告警
                    _send_stream_notice("user-data 流断开, 已切回 REST 轮询兜底")

            handlers = UserDataEventHandlers(
                on_order_update=_on_order_update,
                on_account_update=lambda _msg: _refresh_snapshot_after_event(),
                on_connected=_on_connected,
                on_disconnected=_on_disconnected,
            )
            user_stream = BinanceUserDataStream(
                create_listen_key=stream_client.create_listen_key,
                keepalive_listen_key=stream_client.keepalive_listen_key,
                close_listen_key=stream_client.close_listen_key,
                handlers=handlers,
                ws_base=ws_url or None,
            )
            user_stream.start()
            logger.info("User-data websocket stream started (event push)")

            # 公共 mark-price 流: 订阅监控品种, 推送到快照 poller 的 mark 槽,
            # 让守护线程的 mark 读取不再依赖 REST(断线时 poller REST 兜底).
            mark_symbols = [
                t.symbol for t in settings.monitoring.targets if t.enabled
            ] or [binance_cfg.symbol]
            if mark_symbols:
                try:
                    poller = account_snapshot_poller()
                    if poller is not None:
                        mark_stream = BinanceMarkPriceStream(
                            mark_symbols,
                            on_update=lambda sym, price: poller.update_mark_price(
                                sym, price
                            ),
                            ws_base=ws_url or None,
                        )
                        mark_stream.start()
                        logger.info(
                            "Mark-price stream started for %d symbols", len(mark_symbols)
                        )
                except Exception:
                    logger.exception("启动 mark-price 流失败(维持 REST mark 拉取)")
    except Exception:
        logger.exception("启动 user-data websocket 流失败(降级为纯 REST 轮询)")
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
        if user_stream is not None:
            try:
                user_stream.stop(timeout=3.0)
                logger.info("User-data websocket stream stopped")
            except Exception:
                logger.exception("停止 user-data websocket 流失败")
        if mark_stream is not None:
            try:
                mark_stream.stop(timeout=3.0)
                logger.info("Mark-price stream stopped")
            except Exception:
                logger.exception("停止 mark-price 流失败")
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
