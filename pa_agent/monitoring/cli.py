"""Headless terminal entry point for settings-driven monitoring."""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def format_decision_result(frame: Any, decision: dict | None) -> str:
    """Return the order-relevant portion of a completed monitoring decision."""
    prefix = f"[决策] {frame.symbol} {frame.timeframe}"
    if not isinstance(decision, dict):
        return f"{prefix} 分析未产生有效阶段二决策"
    inner = decision.get("decision") or {}
    order_type = inner.get("order_type", "—")
    direction = inner.get("order_direction", "—")
    confidence = inner.get("trade_confidence", "—")
    entry = inner.get("entry_price", "—")
    stop = inner.get("stop_loss_price", "—")
    take_profit_1 = inner.get("take_profit_price", "—")
    take_profit_2 = inner.get("take_profit_price_2", "—")
    win_rate = inner.get("estimated_win_rate", "—")
    reasoning = str(inner.get("reasoning") or "").strip()
    if len(reasoning) > 180:
        reasoning = f"{reasoning[:180]}…"
    summary = [
        f"{prefix} 类型={order_type} 方向={direction} 置信度={confidence} "
        f"入场={entry} 止损={stop} TP1={take_profit_1} TP2={take_profit_2} 胜率={win_rate}"
    ]
    if reasoning:
        summary.append(f"理由={reasoning}")
    return "\n".join(summary)


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
        logger.error("monitoring.enabled=false。请在 config/settings.json 中开启后重试。")
        return 2
    enabled_targets = [target for target in settings.monitoring.targets if target.enabled]
    if not enabled_targets:
        logger.error("monitoring.targets 中没有启用的品种。")
        return 2

    def on_result(frame: Any, decision: dict | None) -> None:
        logger.info(format_decision_result(frame, decision))

    def on_status(_message: str) -> None:
        # MultiSymbolMonitor has already logged this status. Avoid duplicate lines.
        return

    monitor = MultiSymbolMonitor(
        ctx=ctx,
        settings=settings,
        state_path=MONITORING_STATE_PATH,
        on_result=on_result,
        on_status=on_status,
    )
    targets = ", ".join(f"{target.symbol} {target.timeframe}" for target in enabled_targets)
    logger.info(
        "[监控已启动] 目标: %s, 分析并发: %d.",
        targets,
        settings.monitoring.max_concurrent_analyses,
    )
    monitor.start()
    try:
        while monitor.is_running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("[监控停止] 正在保存状态并关闭数据连接…")
    finally:
        monitor.stop()
    return 0
