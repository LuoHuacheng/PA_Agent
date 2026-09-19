# ruff: noqa: RUF002, RUF003 - Chinese product copy
"""限价挂单成交率遥测: 回答"回踩挂单到底成交了多少".

为什么需要它
------------
2026-09-19 复盘发现: 单边行情里被拦/被拒的多单几乎全是 `no_fill` —— 挂单价在
现价下方, 价格直接涨走。这类"策略天然空仓"在现有口径里完全不可见:

- `monitoring_state.json` 只记门控命中次数 (`direction_gate_stats`), 不记成交与否;
- `records/orders/*.jsonl` 是订单事件账本, 没有"一条计划最终有没有成交"的结论;
- `records/cancels/*.jsonl` 有 `limit_entry_timeout` 撤回, 但缺分母(挂了几条)。

本模块把三件事合成一条比率, 只做**计数**, 不参与任何下单判定:

    fill_rate = fills / (fills + timeouts)

口径约定 (必须知道, 否则比率会被误读)
--------------------------------------
1. `placed` 只在执行层真的挂出 resting 限价时 +1, 即 `ExecutionResult.status == "pending"`。
   被门控/Gate 拦下、被拒、dry-run 的轮次都不计入分母 —— 它们根本没上交易所。
2. `fills` 只在限价单**确认成交**时 +1:
   - 下单前发现上一单已成交并补挂保护 (`_replace_pending_limit`, status="skipped"
     且 reason 含 "Previously filled");
   - watcher 在超时窗口内轮询到 FILLED。
3. `timeouts` 只在 `limit_entry_timeout` 撤回时 +1。
   `plan_replaced` / `stale_entry_removed` 是**换单/清理**, 不是"没成交"的结论,
   故意不计入 —— 计入会把主动换计划污名成挂单失效。
4. 计数器落盘在 `monitoring_state.json` 的 `fill_stats` 键, 与 `direction_gate_stats`
   平级; 未知键在读取时忽略, 所以旧进程写下的文件可以直接被新代码读。

    fill_rate 偏低 (< ~30%) 且持续 = 市场在单边, 限价回踩策略天然空仓的报警信号。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

#: 计数可能在成交 watcher 线程与监控主线程并发发生, 用一个粗锁保护累加。
_LOCK = threading.Lock()

#: 成交 watcher（执行层线程）报告"限价超时未成交"的回调。
#: 执行层不依赖监控层：未注册时通知静默丢弃。
_timeout_sink: Callable[[str], None] | None = None


def set_timeout_sink(sink: Callable[[str], None] | None) -> None:
    """注册/清除超时未成交回调（监控进程启动时注册）。"""
    global _timeout_sink
    _timeout_sink = sink


def notify_timeout(symbol: str) -> None:
    """执行层调用：一条 resting 限价等待超时被撤回（未成交）。"""
    sink = _timeout_sink
    if sink is None:
        return
    try:
        sink(str(symbol or "").strip().upper())
    except Exception:  # 遥测绝不打断撤单主流程
        return

#: 状态文件里的顶层键名。
STATE_KEY = "fill_stats"

#: 每个 symbol 下的计数字段。
FIELDS = ("placed", "fills", "timeouts", "breakout_shadow")

#: 低于此成交率视为"策略天然空仓"预警阈值 (只用于展示, 不做任何拦截)。
LOW_FILL_RATE_ALERT = 0.30


def empty_stats() -> dict[str, dict[str, int]]:
    """返回空的 per-symbol 计数器。"""
    return {}


def _coerce_count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def bump(
    stats: dict[str, dict[str, int]] | None,
    symbol: str,
    field: str,
    *,
    by: int = 1,
) -> dict[str, dict[str, int]]:
    """在 *stats* 上原地累加一个计数并返回它; 非法输入静默忽略。

    遥测绝不影响下单主流程: 未知 field 或空 symbol 直接返回原字典。
    """
    if not isinstance(stats, dict):
        return stats or {}
    name = str(symbol or "").strip().upper()
    if not name or field not in FIELDS:
        return stats
    with _LOCK:
        bucket = stats.get(name)
        if not isinstance(bucket, dict):
            bucket = {}
            stats[name] = bucket
        bucket[field] = _coerce_count(bucket.get(field)) + int(by)
    return stats


def normalize(raw: Any) -> dict[str, dict[str, int]]:
    """把落盘的任意形状归一成 `{SYMBOL: {placed, fills, timeouts}}`。

    未知字段丢弃, 负数和字符串数字归一成非负整数 —— 手改过的 state 文件
    不该让监控进程起不来。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for symbol, bucket in raw.items():
        name = str(symbol or "").strip().upper()
        if not name or not isinstance(bucket, dict):
            continue
        cleaned = {field: _coerce_count(bucket.get(field)) for field in FIELDS}
        if any(cleaned.values()):
            out[name] = cleaned
    return out


def fill_rate(bucket: dict[str, int] | None) -> float | None:
    """成交率 = fills / (fills + timeouts); 无样本时返回 None。"""
    if not isinstance(bucket, dict):
        return None
    fills = _coerce_count(bucket.get("fills"))
    timeouts = _coerce_count(bucket.get("timeouts"))
    decided = fills + timeouts
    if decided <= 0:
        return None
    return fills / decided


def summarize(stats: dict[str, dict[str, int]] | None, *, top: int = 5) -> str:
    """一行文本摘要, 供日志/日检复用; 无样本时说明"尚无样本"。"""
    data = normalize(stats)
    if not data:
        return "fill_stats: 尚无样本"
    placed = sum(_coerce_count(b.get("placed")) for b in data.values())
    fills = sum(_coerce_count(b.get("fills")) for b in data.values())
    timeouts = sum(_coerce_count(b.get("timeouts")) for b in data.values())
    overall = fill_rate({"fills": fills, "timeouts": timeouts})
    shown = "n/a" if overall is None else f"{overall:.0%}"
    parts = [
        f"{name}:{rate:.0%}({b.get('fills', 0)}/{_coerce_count(b.get('fills')) + _coerce_count(b.get('timeouts'))})"
        for name, b in sorted(
            data.items(),
            key=lambda kv: (_coerce_count(kv[1].get("fills")) + _coerce_count(kv[1].get("timeouts"))),
            reverse=True,
        )[:top]
        if (rate := fill_rate(b)) is not None
    ]
    tail = f" | {' '.join(parts)}" if parts else ""
    return f"fill_stats: 挂牌{placed} 成交{fills} 超时{timeouts} 成交率{shown}{tail}"
