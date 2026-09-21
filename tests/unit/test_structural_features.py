# ruff: noqa: RUF002 - Chinese product copy

"""structural 空间: 尺度无关性与已知取值测试.

第一硬约束是尺度无关 —— 全部价格 ×k (k>0) 或 +c 后特征值必须不变, 容差不放宽.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import compute_indicators
from tools.structural_features import MIN_WINDOW_BARS, structural_extractors

KEYS = (
    "trend.slope_atr",
    "trend.r2",
    "trend.run_length_norm",
    "trend.ema_gap_atr",
    "channel.width_slope_norm",
    "channel.touch_count_norm",
    "structure.hl_balance",
    "structure.swing_count_norm",
)


def _ohlc_frame(
    spec: list[tuple[float, float, float, float]], *, symbol: str = "BTCUSDT"
) -> KlineFrame:
    """spec 按"旧到新"给出 (open, high, low, close); 返回 newest-first 的 KlineFrame."""
    built = [
        KlineBar(
            seq=len(spec) - i,
            ts_open=1_700_000_000.0 + i * 900.0,
            open=float(o),
            high=float(h),
            low=float(lo),
            close=float(c),
            volume=10.0,
        )
        for i, (o, h, lo, c) in enumerate(spec)
    ]
    built.reverse()
    bars = [replace(bar, seq=i + 1) for i, bar in enumerate(built)]
    return KlineFrame(
        symbol=symbol,
        timeframe="15m",
        bars=tuple(bars),
        indicators=compute_indicators(bars),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


def _frame(closes: list[float], *, symbol: str = "BTCUSDT", band: float = 1.0) -> KlineFrame:
    """closes 按"旧到新"给出; open 取前一根收盘(首根 -0.5), high/low = close ± band.

    band 恒定是很重要的: ATR 因此恒 >= 2 * band > 0, 而每根 bar 的 high-low 恒等于
    2 * band, 于是 _compute_hl_count 里的 reset 分支(newer_range >= 1.2 * atr)永远为
    假且留有 20% 余量 —— 不会出现"比较恰好落在阈值上"的尺度无关性假阴性.
    """
    spec = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i > 0 else c - 0.5
        spec.append((float(prev), float(c) + band, float(c) - band, float(c)))
    return _ohlc_frame(spec, symbol=symbol)


def _rescaled(frame: KlineFrame, *, k: float = 1.0, c: float = 0.0) -> KlineFrame:
    """所有价格 ×k 再 +c, 指标重算."""
    bars = [
        replace(
            bar,
            open=bar.open * k + c,
            high=bar.high * k + c,
            low=bar.low * k + c,
            close=bar.close * k + c,
        )
        for bar in frame.bars
    ]
    return KlineFrame(
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        bars=tuple(bars),
        indicators=compute_indicators(bars),
        snapshot_ts_local_ms=frame.snapshot_ts_local_ms,
    )


def _synthetic_closes() -> list[float]:
    """40 根: 旧半窄幅震荡, 新半宽幅上行(末 4 根连续阳线, 摆动点与通道都良定义)."""
    closes = [100.0 + 0.4 * ((i % 4) - 2) for i in range(20)]
    closes += [101.0 + 1.5 * (i - 20) + 2.0 * ((i % 4) - 2) for i in range(20, 40)]
    return closes


def test_structural_extractors_expose_exactly_eight_features():
    assert set(structural_extractors()) == set(KEYS)


@pytest.mark.parametrize("name", KEYS)
def test_feature_is_scale_invariant(name: str):
    """乘性(×3.7)与加性(+500)都必须保持特征值不变; 相对容差 1e-9, 绝对容差 0."""
    base = structural_extractors()[name](_frame(_synthetic_closes()))
    scaled = structural_extractors()[name](_rescaled(_frame(_synthetic_closes()), k=3.7))
    shifted = structural_extractors()[name](_rescaled(_frame(_synthetic_closes()), c=500.0))
    assert base is not None
    assert scaled == pytest.approx(base, rel=1e-9, abs=0.0), f"{name} 不是乘性尺度无关"
    assert shifted == pytest.approx(base, rel=1e-9, abs=0.0), f"{name} 不是加性尺度无关"


def test_slope_atr_equals_one_over_atr_on_a_linear_ramp():
    frame = _frame([100.0 + i for i in range(40)])
    atr = frame.indicators.atr14[0]
    assert math.isfinite(atr) and atr > 0
    value = structural_extractors()["trend.slope_atr"](frame)
    assert value == pytest.approx(1.0 / atr, rel=1e-12)


def test_r2_is_one_on_a_linear_ramp():
    frame = _frame([100.0 + i for i in range(40)])
    assert structural_extractors()["trend.r2"](frame) == pytest.approx(1.0, rel=1e-9)


def test_run_length_norm_counts_the_trailing_up_bars():
    closes = [100.0 - 0.5 * i for i in range(37)] + [82.5, 83.5, 84.5]
    frame = _frame(closes)
    value = structural_extractors()["trend.run_length_norm"](frame)
    assert value == pytest.approx(3 / 40)


def test_run_length_norm_is_none_when_the_newest_bar_is_flat():
    frame = _frame([100.0 + 0.1 * i for i in range(40)])
    bars = list(frame.bars)
    bars[0] = replace(bars[0], open=bars[0].close)  # bars[0] 就是最新那根
    flat = KlineFrame(
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        bars=tuple(bars),
        indicators=compute_indicators(bars),
        snapshot_ts_local_ms=frame.snapshot_ts_local_ms,
    )
    assert structural_extractors()["trend.run_length_norm"](flat) is None


@pytest.mark.parametrize("name", KEYS)
def test_feature_is_none_when_the_window_is_too_short(name: str):
    frame = _frame([100.0 + i for i in range(40)])
    assert frame.indicators.atr14[0] > 0  # ATR 本身合法, 只有窗口长度不合格
    extractor = structural_extractors(lookback=MIN_WINDOW_BARS - 1)[name]
    assert extractor(frame) is None


@pytest.mark.parametrize("name", KEYS)
def test_feature_is_none_when_atr_is_nan(name: str):
    frame = _frame([100.0 + i for i in range(10)])  # < 14 根: atr14[0] 是 nan
    assert math.isnan(frame.indicators.atr14[0])
    assert structural_extractors()[name](frame) is None


@pytest.mark.parametrize("name", KEYS)
def test_feature_is_none_when_atr_is_zero(name: str):
    frame = _frame([100.0] * 14, band=0.0)  # high == low == close => TR 恒为 0
    assert frame.indicators.atr14[0] == 0.0
    assert structural_extractors()[name](frame) is None


def test_width_slope_is_positive_when_the_newer_half_is_wider():
    closes = [100.0 + 0.2 * (i % 3) for i in range(20)]
    closes += [100.0 + 3.0 * (i - 20) for i in range(20, 40)]
    value = structural_extractors()["channel.width_slope_norm"](_frame(closes))
    assert value is not None and value > 0


def test_width_slope_is_negative_when_the_newer_half_is_narrower():
    closes = [100.0 + 3.0 * i for i in range(20)]
    closes += [200.0 + 0.2 * (i % 3) for i in range(20, 40)]
    value = structural_extractors()["channel.width_slope_norm"](_frame(closes))
    assert value is not None and value < 0


def test_hl_balance_is_positive_on_a_monotone_uptrend():
    frame = _frame([100.0 + 2.0 * i for i in range(40)])
    value = structural_extractors()["structure.hl_balance"](frame)
    assert value is not None and value > 0
