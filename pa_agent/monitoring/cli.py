"""Headless terminal entry point for settings-driven monitoring."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def format_decision_result(frame: Any, decision: dict | None) -> str:
    """Return one terminal-safe summary for a completed monitoring analysis."""
    prefix = f"[决策] {frame.symbol} {frame.timeframe}"
    if not isinstance(decision, dict):
        return f"{prefix} 分析未产生有效阶段二决策"
    inner = decision.get("decision") or {}
    order_type = inner.get("order_type", "—")
    direction = inner.get("order_direction", "—")
    confidence = inner.get("trade_confidence", "—")
    entry = inner.get("entry_price", "—")
    stop = inner.get("stop_loss_price", "—")
    take_profit = inner.get("take_profit_price", "—")
    summary = (
        f"{prefix} 类型={order_type} 方向={direction} 置信度={confidence} "
        f"入场={entry} 止损={stop} 止盈={take_profit}"
    )
    return f"{summary}\n[完整决策] {json.dumps(decision, ensure_ascii=False, sort_keys=True)}"


def run_monitor() -> int:
    """Run multi-symbol monitoring without importing or creating PyQt widgets."""
    from pa_agent.app_context import AppContext
    from pa_agent.config.paths import MONITORING_STATE_PATH
    from pa_agent.monitoring.service import MultiSymbolMonitor
    from pa_agent.util.crash_diagnostics import enable_crash_diagnostics, log_startup_diagnostics
    from pa_agent.util.logging import configure_logging

    enable_crash_diagnostics()
    configure_logging()
    log_startup_diagnostics()
    ctx = AppContext.bootstrap(connect_data_source=False, create_event_bus=False)
    settings = ctx.settings
    if settings is None or not settings.monitoring.enabled:
        print("[错误] monitoring.enabled=false。请在 config/settings.json 中开启后重试。", flush=True)
        return 2
    enabled_targets = [target for target in settings.monitoring.targets if target.enabled]
    if not enabled_targets:
        print("[错误] monitoring.targets 中没有启用的品种。", flush=True)
        return 2

    def on_result(frame: Any, decision: dict | None) -> None:
        message = format_decision_result(frame, decision)
        logger.info(message)
        print(message, flush=True)

    def on_status(message: str) -> None:
        print(f"[监控状态] {message}", flush=True)

    monitor = MultiSymbolMonitor(
        ctx=ctx,
        settings=settings,
        state_path=MONITORING_STATE_PATH,
        on_result=on_result,
        on_status=on_status,
    )
    targets = ", ".join(f"{target.symbol} {target.timeframe}" for target in enabled_targets)
    print(f"[监控已启动] 目标: {targets}, 分析并发: {settings.monitoring.max_concurrent_analyses}.", flush=True)
    monitor.start()
    try:
        while monitor.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[监控停止] 正在保存状态并关闭数据连接…", flush=True)
    finally:
        monitor.stop()
    return 0
