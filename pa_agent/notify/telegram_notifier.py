"""Telegram Bot 消息通知（配置在 settings.json 的 telegram 段，无 GUI）。

通过 Bot API 的 sendMessage 发送下单信号文本。
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT_S = 15


def _telegram_config_dict(settings: Settings | None = None) -> dict[str, Any]:
    if settings is not None:
        return settings.telegram.model_dump()
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import load_settings

    return load_settings(SETTINGS_JSON_PATH).telegram.model_dump()


def resolve_telegram_credentials(settings: Settings | None = None) -> tuple[str, str]:
    """(bot_token, chat_id) from settings.telegram, else env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."""
    cfg = _telegram_config_dict(settings)
    token = (cfg.get("bot_token") or "").strip()
    if not token:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(cfg.get("chat_id") or "").strip()
    if not chat_id:
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def telegram_is_active(settings: Settings | None = None) -> bool:
    """True only when Telegram is enabled and token + chat_id are configured."""
    cfg = _telegram_config_dict(settings)
    if not cfg.get("enabled", False):
        return False
    token, chat_id = resolve_telegram_credentials(settings)
    return bool(token and chat_id)


def send_telegram_message(
    text: str,
    *,
    settings: Settings | None = None,
    token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Send a plain-text message to the configured Telegram chat."""
    bot_token = token or resolve_telegram_credentials(settings)[0]
    chat = chat_id or resolve_telegram_credentials(settings)[1]
    if not bot_token or not chat:
        logger.debug("Telegram 未配置 bot_token/chat_id，跳过推送")
        return False

    try:
        import requests  # type: ignore[import]
    except ImportError:
        logger.warning("Telegram：requests 库未安装，请运行 pip install requests")
        return False

    try:
        resp = requests.post(
            _TELEGRAM_API.format(token=bot_token),
            json={"chat_id": chat, "text": text},
            timeout=_REQUEST_TIMEOUT_S,
        )
        if resp.status_code == 200:
            logger.info("Telegram 推送成功")
            return True
        logger.error("Telegram 返回异常: HTTP %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.error("发送 Telegram 推送出错: %s", exc)
    return False


def _fmt(value: Any, default: str = "—") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _truncate(text: str, max_len: int = 600) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _build_order_text(
    *,
    decision_inner: dict,
    stage2_full: dict,
    symbol: str,
    timeframe: str,
) -> str:
    dec = decision_inner or {}
    ncp: dict = stage2_full.get("next_cycle_prediction") or {}

    order_type = _fmt(dec.get("order_type"))
    order_dir = _fmt(dec.get("order_direction"))
    entry = _fmt(dec.get("entry_price"))
    stop = _fmt(dec.get("stop_loss_price"))
    tp = _fmt(dec.get("take_profit_price"))
    tp2 = _fmt(dec.get("take_profit_price_2"))
    reasoning = _truncate((dec.get("reasoning") or "").strip(), 600)
    trade_conf = _fmt(dec.get("trade_confidence"))
    win_rate = _fmt(dec.get("estimated_win_rate"))

    probs: dict = ncp.get("probabilities") or {}
    if probs:
        best_key = max(probs, key=lambda k: probs[k])
        next_cycle_str = f"{best_key}（概率 {probs[best_key]}）"
    elif ncp.get("cycle"):
        next_cycle_str = _fmt(ncp.get("cycle"))
    else:
        next_cycle_str = "—"

    lines = [
        "📊 PA Agent 下单信号",
        "",
        f"品种：{symbol}  周期：{timeframe}",
        f"下单类型：{order_type}  方向：{order_dir}",
        f"入场价：{entry}  止损：{stop}",
        f"TP1：{tp}  TP2：{tp2}",
        f"置信度：{trade_conf}  预估胜率：{win_rate}",
    ]
    if reasoning:
        lines.extend(["", f"决策理由：{reasoning}"])
    lines.extend(["", f"下一个市场周期预期：{next_cycle_str}"])
    return "\n".join(lines)


def send_order_signal(
    *,
    decision_inner: dict,
    stage2_full: dict,
    symbol: str,
    timeframe: str,
    settings: Settings | None = None,
) -> bool:
    """下单决策触发时向 Telegram 推送文本消息（与飞书/PushPlus 并行，互不依赖）。"""
    if not telegram_is_active(settings):
        return False
    text = _build_order_text(
        decision_inner=decision_inner,
        stage2_full=stage2_full,
        symbol=symbol,
        timeframe=timeframe,
    )
    return send_telegram_message(text, settings=settings)
