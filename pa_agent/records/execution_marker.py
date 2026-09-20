# ruff: noqa: RUF002, RUF003 - Chinese product copy
"""执行标记：判断一条 stage-2 方案"到底有没有真的下单"的唯一事实来源。

Why: 方案连续性守卫（``ai.decision_continuity`` / ``monitoring.light_gate``）见到上一轮
``order_type=限价单`` 就默认"交易所上有挂单在场"，于是让模型等待触价、禁止反手。但执行层
可能在此之前就把方案拦下（入口门控 / 执行拒绝 / dry-run / 禁用），交易所上其实什么都没有：

    2026-09-13 现场 —— ``block_short_entry`` 连拦 6 条做空限价单，CSV 无行、无推送，
    之后每轮模型都在"等待上一轮 77100 做空限价触价"，新机会被连续性规则一路压掉。

标记写在 ``stage2_decision`` 内部，随记录一起落盘：``"execution": {"status": ...,
"gate_reasons": [...]}``。只有 ``ACTIVE_STATUSES`` 里的状态代表"交易所上真的有单在工作"；
未标记的记录按旧行为视为已提交（历史记录与尚未接入标记的路径保持兼容）。
"""
from __future__ import annotations

from typing import Any

#: stage2_decision 内的 execution 键名。
EXECUTION_KEY = "execution"

#: 只有在场工作的状态才算"有效方案"。其余取值（gate_blocked / rejected /
#: skipped / dry_run / failed / unknown）都不构成在场挂单。
ACTIVE_STATUSES = frozenset({"submitted", "pending"})


def mark_execution(
    decision: Any,
    *,
    status: str,
    gate_reasons: tuple[str, ...] | list[str] = (),
    reason: str = "",
) -> dict[str, Any]:
    """把执行结果原地写进 *decision*（即记录的 stage2_decision）并返回 marker。

    reason 是执行层自己的文案（ExecutionResult.reason），与入口门控的
    gate_reasons 互补：门控拦的单原因在 gate_reasons，而执行层拒绝的单
    （notional 超限 / 重复持仓 / 逆势 / 止损过近…）此前只落 status、不落原因，
    事后无法归因。2026-09-20 补：docs/被拦订单反事实分析.md 第 6 节发现 89 条
    rejected 记录无原因，而它们恰是净额最大的那批。
    """
    marker: dict[str, Any] = {"status": str(status or "unknown")}
    reasons = [str(r) for r in gate_reasons if str(r)]
    if reasons:
        marker["gate_reasons"] = reasons
    text = str(reason or "").strip()
    if text:
        marker["reason"] = text
    if isinstance(decision, dict):
        decision[EXECUTION_KEY] = marker
    return marker


def decision_of(record: Any) -> dict[str, Any] | None:
    """取出记录（AnalysisRecord 对象或 dict）里的 stage2_decision。"""
    s2 = getattr(record, "stage2_decision", None)
    if not isinstance(s2, dict) and isinstance(record, dict):
        s2 = record.get("stage2_decision")
    return s2 if isinstance(s2, dict) else None


def execution_status(record: Any) -> str:
    """记录的执行状态；未标记（或不适用）返回空串。"""
    s2 = decision_of(record)
    if s2 is None:
        return ""
    marker = s2.get(EXECUTION_KEY)
    if not isinstance(marker, dict):
        return ""
    return str(marker.get("status") or "")


def gate_reasons(record: Any) -> list[str]:
    """门控拦截原因列表（非门控拦截时为空）。"""
    s2 = decision_of(record)
    if s2 is None:
        return []
    marker = s2.get(EXECUTION_KEY)
    if not isinstance(marker, dict):
        return []
    reasons = marker.get("gate_reasons")
    if not isinstance(reasons, list):
        return []
    return [str(r) for r in reasons]


def plan_is_working(record: Any) -> bool:
    """记录里的方案是否真有单在交易所工作。

    未标记 == 旧行为（视为已提交），只有明确的非在场状态才把它判为"没下过单"。
    """
    status = execution_status(record)
    if not status:
        return True
    return status in ACTIVE_STATUSES
