"""Vergex 反推生成器: 从对齐报告的成交特征反推三条机械规则, 产出对照计划单.

三个生成器与 tools/audit/baseline_lab.gen_donchian 同签名, 产出可直接喂给
tools/shadow_replay.simulate_plan 的 Plan(突破单口径), 用于在相同时间线上
重演 vergex 平台各策略族的交易特征:
  - gen_momo_breakout: 动量突破族 (近似 ZEC_MOM1/动量族)
  - gen_dual_supertrend: 双 SuperTrend 翻线族
  - gen_trend_pullback: 趋势回踩族 (近似 Livermore/退婚重生/trend 的 K 线子集)

timeline 为 5 元组 (ts_ms, open, high, low, close) 旧→新。
"""
from __future__ import annotations

import math
from datetime import datetime

from tools.shadow_replay import TZ8, Plan, _cutoff_from_days


def _supertrend(klines, period=10, mult=3.0):
    """返回与 klines 等长的 (trend, st_line) 列表; 暖机段为 (0, nan).

    trend ∈ {1, -1}, 暖机段(前 period+2 根, 与主循环起点 range(period+2, ...)
    对齐)内不建立趋势。暖机后收盘首次突破/跌破棘轮带即确立方向, 之后按标准
    SuperTrend 翻线。

    注: 棘轮条件采用标准口径(收盘未破带则带只能向价格收紧: 上带 min、下带
    max)。若按"收盘破带才收紧"的反向写法, 基本带恒距中价 mult*ATR, 单根波动
    不及 3*ATR 时翻线条件永不触发, 任何序列都产不出信号。
    """
    trs: list[float] = []
    out: list[tuple[int, float]] = []
    prev_close = None
    upper = lower = None
    trend = 0
    for i, (_ts, _o, h, low, c) in enumerate(klines):
        tr = h - low if prev_close is None else max(
            h - low, abs(h - prev_close), abs(low - prev_close))
        trs.append(tr)
        atr = sum(trs[-period:]) / min(period, len(trs))
        mid = (h + low) / 2
        up, lo = mid + mult * atr, mid - mult * atr
        if upper is not None:
            up = min(up, upper) if c <= upper else up
            lo = max(lo, lower) if c >= lower else lo
        if i >= period + 2:
            if trend <= 0 and c > upper:
                trend = 1
            elif trend >= 0 and c < lower:
                trend = -1
        line = lo if trend == 1 else (up if trend == -1 else math.nan)
        out.append((trend, line))
        upper, lower, prev_close = up, lo, c
    return out


def gen_momo_breakout(timeline, symbol, timeframe, *, days=None, target_rr=2.0,
                      stop_atr_mult=1.0, per_day=2, momo_n=14, momo_atr=1.5) -> list[Plan]:
    """动量突破 (近似 ZEC_MOM1/动量族).

    反推依据: 对齐报告显示该族止损≈1.58ATR、持仓 1.75h、入场多在盘中 MACD
    触发, 故以"收盘创 momo_n 根新高/新低 + 相对 momo_n 根前收盘的动量超过
    momo_atr*ATR14"近似 MACD 发散确认的盘中突破入场。

    多头: close > 前 momo_n 根最高价 且 close - close[i-momo_n] > momo_atr*ATR14;
    空头镜像。止损 = entry - side*stop_atr_mult*ATR14,
    目标 = entry + side*target_rr*stop_atr_mult*ATR14。
    """
    plans: list[Plan] = []
    if len(timeline) < momo_n + 60:
        return plans
    cutoff = _cutoff_from_days(days) if days else None
    day_key = None
    day_count = 0
    cooldown_until = -1
    trs: list[float] = []
    for i in range(1, len(timeline)):
        _ts, _o, h, low, _c = timeline[i]
        prev_c = timeline[i - 1][4]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    for i in range(momo_n + 1, len(timeline) - 5):
        ts = timeline[i][0]
        if cutoff and datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d") < cutoff:
            continue
        window = timeline[i - momo_n:i]
        hi = max(b[2] for b in window)
        lo = min(b[3] for b in window)
        _ts, _o, _h, _low, c = timeline[i]
        ref_c = timeline[i - momo_n][4]
        atr = sum(trs[max(0, i - 14):i]) / 14 if i >= 14 else 0.0
        if atr <= 0:
            continue
        side = 0
        if c > hi and c - ref_c > momo_atr * atr:
            side = 1
        elif c < lo and ref_c - c > momo_atr * atr:
            side = -1
        if side == 0:
            continue
        dk = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d")
        if dk != day_key:
            day_key, day_count = dk, 0
        if day_count >= per_day or i <= cooldown_until:
            continue
        entry = c
        stop = entry - side * stop_atr_mult * atr
        target = entry + side * target_rr * stop_atr_mult * atr
        plans.append(Plan(
            path="vergex-momo-breakout", symbol=symbol, timeframe=timeframe,
            record_ms=ts, order_type="突破单",
            direction="做多" if side == 1 else "做空",
            entry=entry, stop=stop, target=target, target2=None,
            cycle_position="baseline", diag_direction="baseline",
            trade_conf=None, diag_conf=None, est_win_rate=None,
        ))
        day_count += 1
        cooldown_until = i + 8
    return plans


def gen_dual_supertrend(timeline, symbol, timeframe, *, days=None, target_rr=2.0,
                        stop_atr_mult=1.0, per_day=2, st_period=10, st_mult=3.0) -> list[Plan]:
    """双 SuperTrend 翻线 (近似 Dual-SuperTrend 族).

    反推依据: 对齐报告显示该族止损≈2.8ATR、持仓 31h, 与 SuperTrend(st_period/
    st_mult) 翻线时止损挂在趋势线上的口径一致。

    标准 SuperTrend 翻线入场: trend 由 -1→1(含暖机后首次确立)做多、
    1→-1 做空; 止损 = 翻线时的 SuperTrend 线, 目标 = entry +
    target_rr*|entry - stop|。stop_atr_mult 仅为与族内其他生成器签名对齐而
    保留, 该族止损由趋势线决定。
    """
    plans: list[Plan] = []
    if len(timeline) < st_period + 60:
        return plans
    cutoff = _cutoff_from_days(days) if days else None
    st = _supertrend(timeline, st_period, st_mult)
    day_key = None
    day_count = 0
    cooldown_until = -1
    for i in range(st_period + 2, len(timeline) - 5):
        ts = timeline[i][0]
        if cutoff and datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d") < cutoff:
            continue
        trend, line = st[i]
        if trend == 0 or trend == st[i - 1][0]:
            continue
        side = 1 if trend == 1 else -1
        dk = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d")
        if dk != day_key:
            day_key, day_count = dk, 0
        if day_count >= per_day or i <= cooldown_until:
            continue
        entry = timeline[i][4]
        stop = line
        if side == 1 and not stop < entry:
            continue
        if side == -1 and not stop > entry:
            continue
        target = entry + side * target_rr * abs(entry - stop)
        plans.append(Plan(
            path="vergex-dual-supertrend", symbol=symbol, timeframe=timeframe,
            record_ms=ts, order_type="突破单",
            direction="做多" if side == 1 else "做空",
            entry=entry, stop=stop, target=target, target2=None,
            cycle_position="baseline", diag_direction="baseline",
            trade_conf=None, diag_conf=None, est_win_rate=None,
        ))
        day_count += 1
        cooldown_until = i + 8
    return plans


def gen_trend_pullback(timeline, symbol, timeframe, *, days=None, target_rr=2.0,
                       stop_atr_mult=1.0, per_day=1, ema_fast=20, ema_slow=50) -> list[Plan]:
    """趋势回踩收复 (近似 Livermore/退婚重生/trend strategy 的 K 线子集).

    反推依据: 对齐报告显示 eth_mid 止损≈1.47ATR、持仓 5h、全部为上根收盘
    决策, 故方向与入场只用已收盘 K 线判定。

    趋势方向 = EMA_fast > EMA_slow 只做多、EMA_fast < EMA_slow 只做空
    (泛化平台 "EMA50>EMA200" 口径); 入场 = 回踩收复: 多头要求前一根
    close <= EMA_fast 且当前 close > EMA_fast(空头镜像); 止损 = entry -
    side*stop_atr_mult*ATR14, 目标 = entry + side*target_rr*stop_atr_mult*ATR14。
    EMA 用标准递推(alpha=2/(n+1), 种子=首根 close)。

    注: 暖机期视为"未收复"基线, 暖机后首根满足趋势方向的 K 线视作首次收复
    入场——否则单调强趋势(回踩不到 EMA_fast)的序列将没有任何信号。
    """
    plans: list[Plan] = []
    if len(timeline) < ema_slow + 60:
        return plans
    cutoff = _cutoff_from_days(days) if days else None
    alpha_f = 2.0 / (ema_fast + 1)
    alpha_s = 2.0 / (ema_slow + 1)
    emas_f: list[float] = []
    emas_s: list[float] = []
    f = s = None
    for _ts, _o, _h, _low, c in timeline:
        f = c if f is None else f + alpha_f * (c - f)
        s = c if s is None else s + alpha_s * (c - s)
        emas_f.append(f)
        emas_s.append(s)
    day_key = None
    day_count = 0
    cooldown_until = -1
    below = above = True  # 暖机基线: 视为未收复状态
    trs: list[float] = []
    for i in range(1, len(timeline)):
        _ts, _o, h, low, _c = timeline[i]
        prev_c = timeline[i - 1][4]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    for i in range(ema_slow + 1, len(timeline) - 5):
        ts = timeline[i][0]
        if cutoff and datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d") < cutoff:
            continue
        _ts, _o, _h, _low, c = timeline[i]
        ef, es = emas_f[i], emas_s[i]
        atr = sum(trs[max(0, i - 14):i]) / 14 if i >= 14 else 0.0
        if atr <= 0:
            continue
        side = 0
        if ef > es and (below or timeline[i - 1][4] <= emas_f[i - 1]) and c > ef:
            side = 1
        elif ef < es and (above or timeline[i - 1][4] >= emas_f[i - 1]) and c < ef:
            side = -1
        below = c <= ef
        above = c >= ef
        if side == 0:
            continue
        dk = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d")
        if dk != day_key:
            day_key, day_count = dk, 0
        if day_count >= per_day or i <= cooldown_until:
            continue
        entry = c
        stop = entry - side * stop_atr_mult * atr
        target = entry + side * target_rr * stop_atr_mult * atr
        plans.append(Plan(
            path="vergex-trend-pullback", symbol=symbol, timeframe=timeframe,
            record_ms=ts, order_type="突破单",
            direction="做多" if side == 1 else "做空",
            entry=entry, stop=stop, target=target, target2=None,
            cycle_position="baseline", diag_direction="baseline",
            trade_conf=None, diag_conf=None, est_win_rate=None,
        ))
        day_count += 1
        cooldown_until = i + 8
    return plans
