"""看护循环共享的容错预算。

四条持仓看护循环(breakeven guard / TP runner / time-stop / stop watchdog)
历史上各自手写 ``consecutive_errors`` 计数与 "连续 5 次即弃守" 判定
(13 处魔法数字)。ErrorBudget 把阈值与计数收进一个 module; 失败描述
(轮询失败 / 挂损失败 / TP2 挂单失败...) 语义各异, 仍由各循环自记。
"""
from __future__ import annotations

#: 连续 API 错误达到该次数即弃守本循环 (record() 返回 True)。
GUARD_MAX_ERRORS = 5


class ErrorBudget:
    """连续错误计数器; record() 返回 True 表示预算耗尽, 调用方应退出循环。

    成功一轮后调用 reset() 归零。限流等待不计入(限流是共享 IP 的环境
    惩罚, 不是本循环的逻辑故障), 由调用方在进入本计数器之前分流。
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def record(self) -> bool:
        """Count one error; True when the give-up budget is exhausted."""
        self.count += 1
        return self.count >= GUARD_MAX_ERRORS

    def reset(self) -> None:
        self.count = 0
