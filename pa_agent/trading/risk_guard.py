"""日度亏损熔断 (daily loss circuit breaker).

当日已实现净亏损达到上限后停止自动开新仓, 次日(本地日)自动解除。

口径与 repo 一致: net = REALIZED_PNL + COMMISSION, 资金费(FUNDING_FEE)不计。
账本来自 Binance 的 GET /fapi/v1/income (weight 30), 所以熔断状态写进运行时
state: 同一天内一旦触发, 后续下单请求不再重复拉账本。

本模块只提供纯函数, 状态读写留给调用方(binance_usdm_testnet 持有 _STATE_LOCK
与 _load_state/_save_state), 避免 trading 子模块之间互相导入。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

TZ8 = timezone(timedelta(hours=8))
#: 运行时 state 里记录熔断的键。
HALT_KEY = "risk_halt"
#: 计入净亏损的 income 类型(资金费不算)。
NET_INCOME_TYPES = ("REALIZED_PNL", "COMMISSION")


def _now_ms(now_ms: int | None) -> int:
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def day_key(now_ms: int | None = None) -> str:
    """本地日标识 yyyymmdd, 用于判断熔断是否还属于今天。"""
    return datetime.fromtimestamp(_now_ms(now_ms) / 1000, TZ8).strftime("%Y%m%d")


def day_start_ms(now_ms: int | None = None) -> int:
    """本地日零点对应的 epoch 毫秒。"""
    dt = datetime.fromtimestamp(_now_ms(now_ms) / 1000, TZ8)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def realized_net(rows: Any) -> float:
    """汇总账本里的已实现净盈亏(REALIZED_PNL + COMMISSION)。"""
    total = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("incomeType") or "") not in NET_INCOME_TYPES:
            continue
        try:
            total += float(row.get("income") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def halt_record(state: Any, now_ms: int | None = None) -> dict[str, Any] | None:
    """当日仍然生效的熔断记录; 缺失或隔日返回 None。"""
    record = (state or {}).get(HALT_KEY) if isinstance(state, dict) else None
    if not isinstance(record, dict):
        return None
    if str(record.get("day") or "") != day_key(now_ms):
        return None
    return record


def breach(rows: Any, limit_usdt: float) -> tuple[bool, float]:
    """(是否触发, 当日净盈亏)。limit <= 0 表示关闭。"""
    net = realized_net(rows)
    limit = float(limit_usdt or 0.0)
    return (limit > 0 and net <= -abs(limit)), net


def build_halt(net_usdt: float, limit_usdt: float, now_ms: int | None = None) -> dict[str, Any]:
    """构造写入 state 的熔断记录。"""
    ms = _now_ms(now_ms)
    return {
        "day": day_key(ms),
        "ts": ms / 1000.0,
        "net_usdt": round(float(net_usdt), 4),
        "limit_usdt": float(limit_usdt),
    }
