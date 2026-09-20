"""日线趋势口径: 窗口长度、未收盘 bar、端点法 vs 斜率法 (2026-09-20 修正).

修的是一个真实缺陷: 线上 daily_close_series(days) 曾返回 days+2 根含当日
未收盘 bar, 首尾法因此是 8 天跨度且末端价随日内波动; 回放则取最近 days 根。
两边不是同一个门, 所有"禁逆势"回放证据都对不上线上。
"""
from __future__ import annotations

import pytest

from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient
from tools.shadow_replay import _closed_window, _trend_pct_at, _trend_slope_pct_at

DAY = 86_400_000
END = 1_700_000_000_000


def _series(values):
    """(open_time, close) 由旧到新; 最后一根 open = END, 即"今日未收盘"."""
    n = len(values)
    return [(END - (n - 1 - i) * DAY, float(v)) for i, v in enumerate(values)]


def test_closed_window_drops_forming_bar():
    closes = _series([100, 101, 102, 103, 104, 105, 106, 107, 108, 999])
    win = _closed_window(closes, END, 7)
    assert win is not None
    assert len(win) == 8
    assert win[0] == (END - 8 * DAY, 101.0)
    assert win[-1] == (END - DAY, 108.0)


def test_closed_window_returns_none_when_short():
    assert _closed_window(_series([100, 101, 102]), END, 7) is None


def test_trend_pct_at_uses_closed_endpoints():
    closes = _series([100, 101, 102, 103, 104, 105, 106, 107, 108, 999])
    assert _trend_pct_at(closes, END, 7) == pytest.approx((108 - 101) / 101 * 100)


def test_trend_pct_at_returns_none_before_enough_history():
    closes = _series([100, 101, 102, 103, 104, 105, 106, 107, 108, 999])
    assert _trend_pct_at(closes, closes[0][0] + 2 * DAY, 7) is None


def test_slope_is_zero_on_flat_series():
    assert _trend_slope_pct_at(_series([100] * 10), END, 7) == pytest.approx(0.0)


def test_slope_equals_regression_on_straight_line():
    # 已收盘窗口 101..108, 回归斜率 b=1, mean=104.5 -> 1*7/104.5*100 = 6.70%
    closes = _series([100, 101, 102, 103, 104, 105, 106, 107, 108, 999])
    assert _trend_slope_pct_at(closes, END, 7) == pytest.approx(6.70, abs=0.05)


def test_slope_less_sensitive_to_a_final_spike_than_endpoints():
    flat_then_spike = _series([100, 100, 100, 100, 100, 100, 100, 100, 140, 999])
    # 端点法吃满末端那根: (140-100)/100
    assert _trend_pct_at(flat_then_spike, END, 7) == pytest.approx(40.0)
    # 斜率法把末端摊到整段: 实测 22.2%, 比 40% 温和 —— 但它是"摊平", 不是抗末端
    assert _trend_slope_pct_at(flat_then_spike, END, 7) == pytest.approx(22.2, abs=0.2)


def test_endpoints_blind_to_the_middle_while_slope_is_not():
    # 两个窗口端点相同(100 -> 200), 中间路径不同
    zigzag = _series([0, 100, 130, 120, 140, 110, 150, 160, 200, 999])
    flat = _series([0, 100, 100, 100, 100, 100, 100, 100, 200, 999])
    assert _trend_pct_at(zigzag, END, 7) == pytest.approx(_trend_pct_at(flat, END, 7))
    assert _trend_slope_pct_at(zigzag, END, 7) != pytest.approx(
        _trend_slope_pct_at(flat, END, 7), abs=0.5)


def test_daily_close_series_drops_forming_bar(monkeypatch):
    rows = [[END - i * DAY, "0", "0", "0", str(100 + (9 - i))] for i in range(9, -1, -1)]
    client = BinanceUSDMTestnetClient("k", "s", now_ms=lambda: END)
    monkeypatch.setattr(client, "_request", lambda *a, **k: rows)
    closes = client.daily_close_series("BTCUSDT", 7)
    assert len(closes) == 8
    assert closes[0] == 101.0
    assert closes[-1] == 108.0  # 今日那根(109)被丢弃
