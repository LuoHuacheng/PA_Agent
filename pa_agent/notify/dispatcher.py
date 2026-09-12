"""通知通道 dispatch: 下单信号扇出的唯一入口。

各通道(feishu/pushplus/telegram)保留自己的传输与文案实现 — 本 module 拥有
"逐通道容错扇出"策略: 单通道失败只记 warning, 不打断主流程, 结果按通道回传。
执行失败告警(send_execution_failure)目前仍为 telegram 独有, 属于产品行为
而非 seam 缺口。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def send_order_signal_all(
    *,
    decision_inner: dict[str, Any],
    stage2_full: dict[str, Any],
    symbol: str,
    timeframe: str,
    settings: Any = None,
    chart_image_path: str | Path | None = None,
) -> dict[str, bool]:
    """Fan one order signal out to every enabled channel; never raises."""
    outcomes: dict[str, bool] = {}

    try:
        from pa_agent.notify.feishu_notifier import send_order_signal as send_feishu

        outcomes["feishu"] = send_feishu(
            decision_inner=decision_inner,
            stage2_full=stage2_full,
            symbol=symbol,
            timeframe=timeframe,
            chart_image_path=chart_image_path,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - 单通道失败不影响主流程
        logger.warning("feishu 下单信号通知失败（不影响主流程）: %s", exc)

    try:
        from pa_agent.notify.pushplus_notifier import send_order_signal as send_pushplus

        outcomes["pushplus"] = send_pushplus(
            decision_inner=decision_inner,
            stage2_full=stage2_full,
            symbol=symbol,
            timeframe=timeframe,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pushplus 下单信号通知失败（不影响主流程）: %s", exc)

    try:
        from pa_agent.notify.telegram_notifier import send_order_signal as send_telegram

        outcomes["telegram"] = send_telegram(
            decision_inner=decision_inner,
            stage2_full=stage2_full,
            symbol=symbol,
            timeframe=timeframe,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram 下单信号通知失败（不影响主流程）: %s", exc)

    return outcomes
