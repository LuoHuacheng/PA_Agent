"""看护循环共享的容错预算与瞬时错误判定。

四条持仓看护循环(breakeven guard / TP runner / time-stop / stop watchdog)
历史上各自手写 ``consecutive_errors`` 计数与 "连续 5 次即弃守" 判定
(13 处魔法数字)。ErrorBudget 把阈值与计数收进一个 module; 失败描述
(轮询失败 / 挂损失败 / TP2 挂单失败...) 语义各异, 仍由各循环自记。

2026-09-17 ZEC 事故补充: 04:56-05:01 的 Binance SSL UNEXPECTED_EOF 属环境
抖动, 却按逻辑故障烧掉弃守预算, TP runner 与 stop watchdog 双双退出; 11:25
TP1 半平成交时已无人看护, 剩余仓位再没挂上 TP2。瞬时错误因此单独计数:
只做指数退避重试, 不消耗弃守预算, 也不再打死线程。
"""
from __future__ import annotations

#: 连续 API 错误达到该次数即弃守本循环 (record() 返回 True)。
GUARD_MAX_ERRORS = 5

#: 瞬时错误退避上限(秒): Binance Testnet 抖动可能持续数分钟, 退避到上限后
#: 固定间隔重试即可, 再放大只会让恢复更慢。
MAX_BACKOFF_SECONDS = 300.0

#: 瞬时/环境类错误标记(小写匹配)。命中即视为"可以重试", 不消耗弃守预算。
#: 刻意不含 -1111/-2011/-4130 等业务拒绝: 那些是逻辑故障, 该走原弃守路径。
#: 也刻意不含 "network error" 这个外壳前缀: 客户端把所有 URLError 都包成
#: "Binance network error: <reason>", 判据必须是里面的真实原因(SSL EOF /
#: timeout / connection reset 都能命中); 只按前缀判等于让一切 URLError 都无限
#: 重试, 逻辑故障也会被当成网络抖动。
_TRANSIENT_MARKERS = (
    "ssl",
    "unexpected_eof",
    "eof occurred",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "bad gateway",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)


def is_transient_error(exc: object) -> bool:
    """True when *exc* looks like a transient transport/environment failure.

    SSL EOF / 连接中断 / 超时 / 5xx 这类错误下一秒可能就好了, 调用方应当退避
    重试而不是把看护线程停掉。限流(429/-1003)由调用方在进入本判定前分流。
    """
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class ErrorBudget:
    """连续错误计数器; record() 返回 True 表示预算耗尽, 调用方应退出循环。

    成功一轮后调用 reset() 归零。限流等待不计入(限流是共享 IP 的环境
    惩罚, 不是本循环的逻辑故障), 由调用方在进入本计数器之前分流; 瞬时网络
    错误同理, 走 record_transient() 只退避不弃守。
    """

    __slots__ = ("count", "transient")

    def __init__(self) -> None:
        self.count = 0
        self.transient = 0

    def record(self) -> bool:
        """Count one error; True when the give-up budget is exhausted."""
        self.count += 1
        return self.count >= GUARD_MAX_ERRORS

    def record_transient(self, base_seconds: float) -> float:
        """Count one transient error; return the backoff delay in seconds.

        指数退避 base * 2^(n-1), 上限 MAX_BACKOFF_SECONDS; 不参与弃守判定。
        """
        self.transient += 1
        exponent = min(self.transient - 1, 16)
        delay = max(float(base_seconds), 0.0) * (2**exponent)
        return min(delay, MAX_BACKOFF_SECONDS)

    def reset(self) -> None:
        self.count = 0
        self.transient = 0
