# ruff: noqa: RUF002 - Chinese product copy

"""尺度无关的结构 / 趋势特征, 供 tools/factor_audit.py 的 structural 空间审计.

第一硬约束是**尺度无关(dimensionless)**: 全部价格 ×k (k>0) 或 +c 后特征值必须不变.
绝对价位 / 成交额派生的量一律不进这里 —— 实测把原始收盘价当因子跑, 7 个 horizon
全部命中阈值, 而且这类伪信号在分档检验里还会显得格外"稳定"(价格水平本身日间自相关),
比真信号更像真信号. 本模块的 8 个特征全部按 ATR 或窗口长度归一.

口径约定(与审计工具一致): frame.bars 是 newest-first, frame.bars[0] 就是被评估的那根;
n = min(lookback, len(frame.bars)); w = frame.bars[:n]; atr = indicators.atr14[0].
n < 5 或 atr 非有限 / <= 0 时, 每个特征都返回 None.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from pa_agent.ai.market_features import _compute_hl_count, _find_swings_with_seq
from pa_agent.data.base import KlineBar, KlineFrame
from tools.factor_audit import Extractor

#: 默认回看窗口, 与 market 空间保持同一口径
DEFAULT_LOOKBACK = 40

#: 窗口短于此数时任何统计量都不可信, 全部特征返回 None
MIN_WINDOW_BARS = 5

#: 触及窗口上下沿的判定带宽(单位: ATR 的倍数)
_TOUCH_BAND_ATR = 0.1


def _atr(frame: KlineFrame) -> float | None:
    """最新 bar 的 ATR; 非有限或 <= 0 时返回 None."""
    series = frame.indicators.atr14
    if not series:
        return None
    value = float(series[0])
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _window(frame: KlineFrame, lookback: int) -> tuple[tuple[KlineBar, ...], float] | None:
    """返回 (最新 n 根 bars, 最新 ATR); n < MIN_WINDOW_BARS 或 ATR 不合法时返回 None."""
    n = min(lookback, len(frame.bars))
    if n < MIN_WINDOW_BARS:
        return None
    atr = _atr(frame)
    if atr is None:
        return None
    return frame.bars[:n], atr


def _ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """一维最小二乘拟合, 返回 (斜率, 截距). 自变量方差为 0 时斜率取 0.0."""
    n = len(xs)
    x_bar = math.fsum(xs) / n
    y_bar = math.fsum(ys) / n
    sxx = math.fsum((x - x_bar) ** 2 for x in xs)
    sxy = math.fsum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx if sxx > 0.0 else 0.0
    return slope, y_bar - slope * x_bar


def _close_fit(window: Sequence[KlineBar]) -> tuple[list[float], list[float]]:
    """把 newest-first 的窗口翻成 oldest-first 的 (x_j = j, y_j = close)."""
    bars = list(reversed(window))
    xs = [float(j) for j in range(len(bars))]
    ys = [float(bar.close) for bar in bars]
    return xs, ys


# ── trend.* ───────────────────────────────────────────────────────────────────


def _slope_atr(frame: KlineFrame, lookback: int) -> float | None:
    """收盘价对时间的 OLS 斜率 / ATR."""
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, atr = ctx
    xs, ys = _close_fit(window)
    slope, _intercept = _ols(xs, ys)
    return slope / atr


def _r2(frame: KlineFrame, lookback: int) -> float | None:
    """同一 OLS 拟合的 R²; SS_tot == 0(收盘价全等)时返回 None."""
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, _atr_value = ctx
    xs, ys = _close_fit(window)
    slope, intercept = _ols(xs, ys)
    y_bar = math.fsum(ys) / len(ys)
    ss_tot = math.fsum((y - y_bar) ** 2 for y in ys)
    if ss_tot == 0.0:
        return None
    ss_res = math.fsum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    return 1.0 - ss_res / ss_tot


def _bar_direction(bar: KlineBar) -> int:
    """close > open -> +1; close < open -> -1; 相等 -> 0."""
    close = float(bar.close)
    open_ = float(bar.open)
    if close > open_:
        return 1
    if close < open_:
        return -1
    return 0


def _run_length_norm(frame: KlineFrame, lookback: int) -> float | None:
    """从最新 bar 起连续同向 bar 数 / n; 最新 bar 是平盘时返回 None."""
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, _atr_value = ctx
    first = _bar_direction(window[0])
    if first == 0:
        return None
    run = 0
    for bar in window:
        if _bar_direction(bar) != first:
            break
        run += 1
    return run / len(window)


def _ema_gap_atr(frame: KlineFrame, lookback: int) -> float | None:
    """(最新收盘 - EMA20) / ATR; ema20 非有限时返回 None."""
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, atr = ctx
    series = frame.indicators.ema20
    if not series:
        return None
    ema = float(series[0])
    if not math.isfinite(ema):
        return None
    return (float(window[0].close) - ema) / atr


# ── channel.* ─────────────────────────────────────────────────────────────────


def _range_width(window: Sequence[KlineBar]) -> float:
    return max(float(bar.high) for bar in window) - min(float(bar.low) for bar in window)


def _width_slope_norm(frame: KlineFrame, lookback: int) -> float | None:
    """通道宽度的时间斜率(收敛为负 / 扩张为正), 按 ATR 归一.

    w 是 newest-first, 所以 w[:half] 是较新的一半, w[half:half*2] 是较旧的一半.
    """
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, atr = ctx
    half = len(window) // 2
    if half < 2:
        return None
    newer = window[:half]
    older = window[half : half * 2]
    return (_range_width(newer) - _range_width(older)) / atr


def _touch_count_norm(frame: KlineFrame, lookback: int) -> float | None:
    """触及窗口上沿或下沿(带宽 0.1 * ATR)的 bar 数 / n."""
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, atr = ctx
    hi = max(float(bar.high) for bar in window)
    lo = min(float(bar.low) for bar in window)
    band = _TOUCH_BAND_ATR * atr
    count = sum(1 for bar in window if float(bar.high) >= hi - band or float(bar.low) <= lo + band)
    return count / len(window)


# ── structure.* ───────────────────────────────────────────────────────────────


def _hl_balance(frame: KlineFrame, lookback: int) -> float | None:
    """H/L 计数平衡度 (bull_count - bear_count) / (bull_count + bear_count)."""
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, atr = ctx
    state = _compute_hl_count(tuple(window), atr)
    total = state.bull_count + state.bear_count
    if total == 0:
        return 0.0
    return (state.bull_count - state.bear_count) / total


def _swing_count_norm(frame: KlineFrame, lookback: int) -> float | None:
    """窗口内摆动点个数 / n.

    注意: 本仓库的 _find_swings_with_seq(bars) 只接受 bars 一个参数 —— 摆动点由 OHLC 的
    局部极值判定, 不需要 ATR(任务书里写的 _find_swings_with_seq(tuple(w), atr) 与该签名
    不符, 多传一个参数会 TypeError, 故这里按仓库实际签名调用).
    """
    ctx = _window(frame, lookback)
    if ctx is None:
        return None
    window, _atr_value = ctx
    return len(_find_swings_with_seq(tuple(window))) / len(window)


def structural_extractors(*, lookback: int = DEFAULT_LOOKBACK) -> dict[str, Extractor]:
    """8 个尺度无关的结构 / 趋势特征.

    每个特征都满足: 全部价格 ×k (k>0) 或 +c 后取值不变.
    """

    def bind(feature: Callable[[KlineFrame, int], float | None]) -> Extractor:
        def extractor(frame: KlineFrame) -> float | None:
            return feature(frame, lookback)

        return extractor

    return {
        "trend.slope_atr": bind(_slope_atr),
        "trend.r2": bind(_r2),
        "trend.run_length_norm": bind(_run_length_norm),
        "trend.ema_gap_atr": bind(_ema_gap_atr),
        "channel.width_slope_norm": bind(_width_slope_norm),
        "channel.touch_count_norm": bind(_touch_count_norm),
        "structure.hl_balance": bind(_hl_balance),
        "structure.swing_count_norm": bind(_swing_count_norm),
    }
