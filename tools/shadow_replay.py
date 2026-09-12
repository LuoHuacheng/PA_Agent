#!/usr/bin/env python3
"""Shadow replay: 用已落盘的决策记录 + 本地 K 线, 离线模拟挂单的成交与结局.

背景
----
真实自动成交约每天 1-5 笔(挂单回踩价常常到不了), 要攒够统计样本需要数周.
但 records/pending/*.json 里每条决策都存了决策时刻可见的 K 线(kline_data),
把这些 K 线按 symbol/timeframe 合并, 就能对每个计划单(限价单/突破单)离线重演:
价格是否触及入场价, 触及后先到止损还是止盈.

两个必须知道的约定
------------------
1. kline_data[].ts_open 是"北京时间当作 epoch"的伪时间戳, 真实 epoch 需要减去
   8 小时; meta.timestamp_local_ms 则是真实 epoch. 两者不可直接比较.
2. 模拟用统一的仓位口径(当前配置的 risk_per_trade_usdt)重算数量, 因此
   2026-09-08 之前的固定名义记录也能和之后的记录放进同一个样本池.

保守假设(会写在输出里, 便于复核)
----------------------------------
- 同一根 K 线内同时触及止损与止盈时, 一律按止损计.
- 出场判定从成交 K 线的下一根开始, 不利用成交当根的剩余振幅.
- 跳空穿越止损时按开盘价成交(更差的价格).
- 分批止盈按 tp_partial_close_pct 拆两段, 不做保本移损.

用法
----
    python tools/shadow_replay.py --replay --days 45
    python tools/shadow_replay.py --calibrate -v
    python tools/shadow_replay.py --replay --days 45 --csv logs/shadow_rows.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TZ8 = timezone(timedelta(hours=8))
PENDING_DIR = ROOT / "records" / "pending"
OUTCOMES_CSV = ROOT / "trade_records" / "outcomes.csv"

#: kline_data 的 ts_open 是本地墙上时间恰好当成 epoch 存下来的.
KLINE_EPOCH_OFFSET_MS = 8 * 3600 * 1000

BAR_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
          "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}

#: 与 config/settings.json 保持一致的模拟口径(改配置时同步这里).
DEFAULT_RISK_USDT = 4.0
DEFAULT_LEVERAGE = 20
DEFAULT_MARGIN_USDT = 250.0
DEFAULT_PARTIAL_PCT = 50.0
DEFAULT_TIMEOUT_MIN = 60.0
DEFAULT_TIME_STOP_MIN = 720.0
#: 往返手续费率(名义占比)的兜底值; --calibrate 会从真实成交反推.
FALLBACK_FEE_RATE = 0.0007


@dataclass
class Plan:
    """一条可模拟的计划单."""

    path: str
    symbol: str
    timeframe: str
    record_ms: int
    order_type: str
    direction: str
    entry: float
    stop: float
    target: float
    target2: float | None
    cycle_position: str
    diag_direction: str
    trade_conf: float | None
    diag_conf: float | None
    est_win_rate: float | None
    bars: list = field(default_factory=list)


def _num(value):
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _cutoff_from_days(days):
    if not days:
        return None
    return (datetime.now(TZ8) - timedelta(days=days)).strftime("%Y-%m-%d")


def load_plans(pending_dir, *, days=None, symbols=None):
    """解析 pending 记录, 产出可模拟的计划单(含决策时刻前后的 K 线)."""
    cutoff = _cutoff_from_days(days)
    plans = []
    for path in sorted(glob.glob(str(pending_dir / "*.json"))):
        name = os.path.basename(path)
        if cutoff and name[:10] < cutoff:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        meta = doc.get("meta") or {}
        symbol = str(meta.get("symbol") or "")
        if symbols and symbol not in symbols:
            continue
        record_ms = meta.get("timestamp_local_ms")
        if not record_ms:
            continue
        decision = ((doc.get("stage2_decision") or {}).get("decision")) or {}
        order_type = str(decision.get("order_type") or "")
        if order_type not in ("限价单", "突破单"):
            continue
        direction = str(decision.get("order_direction") or "")
        if direction not in ("做多", "做空"):
            continue
        entry = _num(decision.get("entry_price"))
        stop = _num(decision.get("stop_loss_price"))
        target = _num(decision.get("take_profit_price"))
        if entry is None or stop is None or target is None or entry <= 0:
            continue
        diag = doc.get("stage1_diagnosis") or {}
        bars = []
        for bar in doc.get("kline_data") or []:
            ts = bar.get("ts_open")
            o = _num(bar.get("open"))
            h = _num(bar.get("high"))
            low = _num(bar.get("low"))
            c = _num(bar.get("close"))
            if not ts or None in (o, h, low, c):
                continue
            bars.append((int(ts) - KLINE_EPOCH_OFFSET_MS, o, h, low, c))
        bars.sort()
        plans.append(Plan(
            path=path, symbol=symbol,
            timeframe=str(meta.get("timeframe") or ""),
            record_ms=int(record_ms), order_type=order_type, direction=direction,
            entry=entry, stop=stop, target=target,
            target2=_num(decision.get("take_profit_price_2")),
            cycle_position=str(diag.get("cycle_position") or "unknown"),
            diag_direction=str(diag.get("direction") or "unknown"),
            trade_conf=_num(decision.get("trade_confidence")),
            diag_conf=_num(decision.get("diagnosis_confidence")),
            est_win_rate=_num(decision.get("estimated_win_rate")),
            bars=bars,
        ))
    return plans


def merge_timelines(plans):
    """按 (symbol, timeframe) 合并所有记录里的 K 线, 去重后按时间升序."""
    merged = defaultdict(dict)
    for plan in plans:
        store = merged[(plan.symbol, plan.timeframe)]
        for bar in plan.bars:
            store[bar[0]] = bar
    return {key: sorted(store.values()) for key, store in merged.items()}


def atr_pct_from_bars(bars, period=14):
    """用决策时刻之前的 K 线算 ATR14 占收盘价的百分比."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        _t, _o, h, low, _c = bars[i]
        prev_close = bars[i - 1][4]
        trs.append(max(h - low, abs(h - prev_close), abs(low - prev_close)))
    atr = sum(trs[-period:]) / period
    close = bars[-1][4]
    return (atr / close * 100) if close else None


@dataclass
class Outcome:
    filled: bool
    reason: str
    entry: float
    exit_avg: float | None
    qty: float
    notional: float
    gross: float
    fees: float
    net: float
    win_r: float
    bars_held: int


def simulate_plan(plan, timeline, *, risk_usdt, fee_rate, leverage, margin_usdt,
                  partial_pct, timeout_min, time_stop_min,
                  min_stop_pct=0.0, min_stop_atr=0.0,
                  be_trigger=None, entry_gtx=False,
                  maker_fee=None, taker_fee=None):
    """在决策时刻之后的 K 线上重演一次计划单."""
    side = 1 if plan.direction == "做多" else -1
    entry, stop, target = plan.entry, plan.stop, plan.target
    zero = Outcome(False, "rejected", entry, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    if side == 1 and not (stop < entry < target):
        return zero
    if side == -1 and not (target < entry < stop):
        return zero

    bar_ms = BAR_MS.get(plan.timeframe, 900_000)
    timeout_bars = max(1, round(timeout_min * 60_000 / bar_ms))
    max_hold_bars = max(1, round(time_stop_min * 60_000 / bar_ms))

    # 执行器会把过窄的止损抬到下限(计划侧 lift_stop_to_min_distance_floor),
    # 模拟沿用同一规则, 否则窄止损单会算出天文数字的名义仓位.
    floor_pct = float(min_stop_pct or 0.0)
    atr_pct = atr_pct_from_bars(plan.bars)
    if min_stop_atr and atr_pct is not None:
        floor_pct = max(floor_pct, float(min_stop_atr) * atr_pct)
    if floor_pct > 0:
        gap_pct = abs(entry - stop) / entry * 100
        if gap_pct < floor_pct:
            stop = entry * (1 - floor_pct / 100) if side == 1 else entry * (1 + floor_pct / 100)
    if side == 1 and not (stop < entry < target):
        return zero
    if side == -1 and not (target < entry < stop):
        return zero

    risk = abs(entry - stop)
    qty = risk_usdt / risk
    notional0 = qty * entry
    if notional0 > margin_usdt * leverage * 1.0001:
        return Outcome(False, "rejected", entry, None, qty, notional0, 0, 0, 0, 0, 0)

    # 只取从决策时刻起连续的一段 K 线: pending 记录是多轮分析的并集, 停机期
    # 会留下空洞, 跨空洞判定会把几小时后的价格当成"下一根"从而误判止损.
    future = []
    for bar in timeline:
        if bar[0] <= plan.record_ms:
            continue
        if future and bar[0] - future[-1][0] != bar_ms:
            break
        future.append(bar)
        if len(future) >= timeout_bars + max_hold_bars + 1:
            break
    if len(future) < 2 or future[0][0] - plan.record_ms > bar_ms * 1.5:
        return Outcome(False, "no_data", entry, None, qty, notional0, 0, 0, 0, 0, 0)

    # ---- 1. 等成交 ----
    fill_i = None
    fill_px = entry
    entry_taker = False  # GTC 下第一根开盘已越过限价 = 挂单即吃单
    crossed0 = False
    if future:
        crossed0 = (future[0][1] < entry) if side == 1 else (future[0][1] > entry)
    for i, (_ts, o, h, low, _c) in enumerate(future[:timeout_bars]):
        if plan.order_type == "限价单":
            if entry_gtx and crossed0:
                # GTX(post-only): 越价挂单会被拒, 不立即成交; 等价格回触 entry
                # 才以 maker 成交(挂单在簿, 触及即成交, 不吃开盘跳价).
                if i == 0:
                    continue
                if (low <= entry <= h) if side == 1 else (low <= entry <= h):
                    fill_px = entry
                    fill_i = i
                    break
                continue
            if (low <= entry) if side == 1 else (h >= entry):
                fill_px = min(entry, o) if side == 1 else max(entry, o)
                if entry_gtx:
                    fill_px = entry
                entry_taker = crossed0 and i == 0 and not entry_gtx
                fill_i = i
                break
        else:
            if (h >= entry) if side == 1 else (low <= entry):
                fill_px = max(entry, o) if side == 1 else min(entry, o)
                fill_i = i
                entry_taker = True  # 市价单恒为 taker
                break
    if fill_i is None:
        return Outcome(False, "unfilled", entry, None, qty, qty * entry, 0, 0, 0, 0, 0)

    # 与执行器一致: 数量由计划锚点(entry)与止损的距离决定, 不因成交价漂移而
    # 重算, 否则跳空成交会把仓位放大到名义上限之外.
    notional = qty * fill_px

    # ---- 2. 等出场(从成交 K 线的下一根开始, 保守) ----
    tp1_qty = qty
    if partial_pct and partial_pct < 100 and plan.target2:
        tp1_qty = qty * (partial_pct / 100.0)
    remaining = qty
    gross = 0.0
    exits = []
    reason = "time_stop"
    bars_held = 0
    stage = 1
    be_stop = None  # 保本移损生效价; 触发后下一根 K 起才检查(保守, 移单有延迟)
    be_armed_pending = False
    last_i = min(len(future) - 1, fill_i + max_hold_bars)
    for j in range(fill_i + 1, min(len(future), fill_i + 1 + max_hold_bars)):
        _ts, o, h, low, _c = future[j]
        bars_held = j - fill_i
        if be_armed_pending:
            be_stop = fill_px
            be_armed_pending = False
        cur_stop = be_stop if be_stop is not None else stop
        if (low <= cur_stop) if side == 1 else (h >= cur_stop):
            px = cur_stop
            if side == 1 and o < cur_stop:
                px = o
            if side == -1 and o > cur_stop:
                px = o
            gross += (px - fill_px) * side * remaining
            exits.append((px, remaining))
            remaining = 0.0
            reason = "be_stop" if be_stop is not None else "stop"
            break
        if be_stop is None and be_trigger:
            be_gain = be_trigger * risk
            if ((h >= fill_px + be_gain) if side == 1 else (low <= fill_px - be_gain)):
                be_armed_pending = True  # 下一根 K 起按保本价检查; 本根 TP 照常评估
        if stage == 1 and ((h >= target) if side == 1 else (low <= target)):
            gross += (target - fill_px) * side * tp1_qty
            exits.append((target, tp1_qty))
            remaining -= tp1_qty
            stage = 2
            if remaining <= qty * 1e-9:
                reason = "tp"
                break
            continue
        if stage == 2 and plan.target2 and ((h >= plan.target2) if side == 1 else (low <= plan.target2)):
            gross += (plan.target2 - fill_px) * side * remaining
            exits.append((plan.target2, remaining))
            remaining = 0.0
            reason = "tp2"
            break
    if remaining > 0:
        last_px = future[last_i][4]
        gross += (last_px - fill_px) * side * remaining
        exits.append((last_px, remaining))
        reason = "time_stop"

    if maker_fee is not None and taker_fee is not None:
        entry_fee_rate = taker_fee if entry_taker else maker_fee
        fees = entry_fee_rate * notional + taker_fee * sum(px * q for px, q in exits)
    else:
        fees = fee_rate * (notional + sum(px * q for px, q in exits))
    exit_avg = (sum(px * q for px, q in exits) / qty) if qty else None
    net = gross - fees
    return Outcome(True, reason, fill_px, exit_avg, qty, notional, gross, fees, net,
                   net / risk_usdt, bars_held)


DAILY_URL = "https://fapi.binance.com/fapi/v1/klines"


def _fetch_daily_closes(symbol, limit=60):
    """主网公共日线收盘 [(open_time_ms, close)]; 不占用 testnet 共享 IP 配额."""
    import json as _json
    import urllib.request
    url = "%s?symbol=%s&interval=1d&limit=%d" % (DAILY_URL, symbol, limit)
    req = urllib.request.Request(url, headers={"User-Agent": "PA_Agent/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        rows = _json.loads(resp.read())
    return [(int(row[0]), float(row[4])) for row in rows]


def _trend_pct_at(closes, ms, days):
    """决策时刻往回 days 根日线的涨跌幅(%); 数据不足返回 None."""
    past = [close for ts, close in closes if ts <= ms]
    if len(past) < 2:
        return None
    window = past[-days:] if len(past) >= days else past
    if len(window) < 2 or window[0] <= 0:
        return None
    return (window[-1] - window[0]) / window[0] * 100


def attach_trends(plans, days):
    """给每条计划单贴上决策时刻的日线趋势(%), 键为 pending 文件名."""
    by_symbol = {}
    for plan in plans:
        if plan.symbol in by_symbol:
            continue
        try:
            by_symbol[plan.symbol] = _fetch_daily_closes(plan.symbol)
        except Exception as exc:
            print("  日线拉取失败 %s: %s" % (plan.symbol, exc))
            by_symbol[plan.symbol] = []
    return {
        os.path.basename(plan.path): _trend_pct_at(
            by_symbol.get(plan.symbol) or [], plan.record_ms, days)
        for plan in plans
    }


def _float_list(raw):
    """'3' 或 '1,2,3' -> [3.0] / [1.0,2.0,3.0]."""
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part:
            try:
                out.append(float(part))
            except ValueError:
                pass
    return out or [3.0]


def _is_counter_trend(row, neutral):
    """该笔是否逆决策时刻的日线趋势."""
    pct = row.get("trend_pct")
    if pct is None or abs(pct) <= neutral:
        return False
    return (row["direction"] == "做多") != (pct > 0)


def _plan_hour_utc8(record_ms: int) -> int:
    return datetime.fromtimestamp(record_ms / 1000, TZ8).hour


def _bad_session_ms(record_ms: int) -> bool:
    """04:00-11:59 UTC+8 = 美盘尾段+亚盘早段低流动性窗口 (2026-09-13 分桶: -290U/-500U)."""
    return 4 <= _plan_hour_utc8(record_ms) < 12


def report_variants(rows, neutral_list=()):
    """同一批模拟成交上比较各个过滤条件, 看边际收益."""
    filters = [
        ("基线 全部", lambda r: True),
        ("禁做空", lambda r: r["direction"] == "做多"),
        ("禁做多(对照)", lambda r: r["direction"] == "做空"),
        ("RR>=1.0", lambda r: (r["rr"] or 0) >= 1.0),
        ("RR>=1.5", lambda r: (r["rr"] or 0) >= 1.5),
        ("RR>=2.0", lambda r: (r["rr"] or 0) >= 2.0),
        ("禁 trending_tr", lambda r: r["cycle_position"] != "trending_tr"),
        ("禁neutral诊断", lambda r: (r.get("diag_direction") or "") != "neutral"),
        ("禁空+禁trend_tr+禁neutral",
         lambda r: r["direction"] == "做多" and r["cycle_position"] != "trending_tr"
         and (r.get("diag_direction") or "") != "neutral"),
        ("禁空+禁trending_tr", lambda r: r["direction"] == "做多" and r["cycle_position"] != "trending_tr"),
        ("normal_channel且做多", lambda r: r["cycle_position"] == "normal_channel" and r["direction"] == "做多"),
        ("trade_conf>=55", lambda r: (r["trade_conf"] or 0) >= 55),
        ("做多+RR>=1.5", lambda r: r["direction"] == "做多" and (r["rr"] or 0) >= 1.5),
        ("做多+RR>=1.5+禁trend_tr", lambda r: r["direction"] == "做多" and (r["rr"] or 0) >= 1.5
         and r["cycle_position"] != "trending_tr"),
        ("做多+RR>=1.5+仅channel", lambda r: r["direction"] == "做多" and (r["rr"] or 0) >= 1.5
         and r["cycle_position"] in ("normal_channel", "broad_channel")),
    ]
    for neutral in neutral_list:
        filters.append((
            "禁逆势 %.1f%%" % neutral,
            lambda r, n=neutral: not _is_counter_trend(r, n),
        ))
    base = [r for r in rows if r["filled"]]
    base_net = sum(r["net"] for r in base)
    print()
    print("=== 过滤变体 (基线 %d 笔, 净 %+.2fU) ===" % (len(base), base_net))
    print("%-26s %5s %6s %10s %8s %7s %9s %10s" %
          ("变体", "笔数", "保留", "净U", "均U", "胜率", "净/风险", "相比基线"))
    for name, pred in filters:
        sub = [r for r in base if pred(r)]
        if not sub:
            print("%-26s %5d" % (name, 0))
            continue
        net = sum(r["net"] for r in sub)
        risk = sum(r["risk_usdt"] for r in sub)
        print("%-26s %5d %5.0f%% %10.2f %8.2f %6.0f%% %9.3f %+10.2f" % (
            name, len(sub), len(sub) / len(base) * 100, net, net / len(sub),
            sum(1 for r in sub if r["net"] > 0) / len(sub) * 100,
            net / risk if risk else 0, net - base_net))



BE_MODES = (("off", None), ("0.5r", 0.5), ("1r", 1.0))


def report_matrix(plans_with_tl, *, args, fee_rate, maker_fee, taker_fee):
    """保本移损 × 入场方式 × 环境门控 的组合矩阵(D3/D4 寻优用).

    每个组合独立重演一遍成交与出场; gate 过滤在结果行上按变体筛选,
    与 report_variants 同口径 (filled 行).
    """
    gate_filters = [
        ("全部", lambda r: True),
        ("禁空", lambda r: r["direction"] == "做多"),
        ("禁trending_tr", lambda r: r["cycle_position"] != "trending_tr"),
        ("禁04-12时", lambda r: not r.get("_bad_session", False)),
        ("禁空+禁trend_tr",
         lambda r: r["direction"] == "做多" and r["cycle_position"] != "trending_tr"),
        ("禁空+禁trend_tr+禁04-12",
         lambda r: r["direction"] == "做多" and r["cycle_position"] != "trending_tr"
         and not r.get("_bad_session", False)),
        ("禁空+禁trend_tr+禁neutral",
         lambda r: r["direction"] == "做多" and r["cycle_position"] != "trending_tr"
         and (r.get("diag_direction") or "") != "neutral"),
    ]
    print()
    print("=== 保本移损 × 入场方式 组合矩阵 (maker=%.4f taker=%.4f) ==="
          % (maker_fee, taker_fee))
    print("%-6s %-4s | %-22s %5s %9s %7s %9s" %
          ("保本", "入场", "环境门控", "笔数", "净U", "胜率", "净/风险"))
    base_rows: list[dict] = []
    for be_name, be_tr in BE_MODES:
        for gtx in (False, True):
            rows = []
            for plan, tl in plans_with_tl:
                out = simulate_plan(
                    plan, tl,
                    risk_usdt=args.risk_usdt, fee_rate=fee_rate,
                    leverage=args.leverage, margin_usdt=args.margin_usdt,
                    partial_pct=args.partial_pct, timeout_min=args.timeout_min,
                    time_stop_min=args.time_stop_min,
                    min_stop_pct=args.min_stop_pct, min_stop_atr=args.min_stop_atr,
                    be_trigger=be_tr, entry_gtx=gtx,
                    maker_fee=maker_fee, taker_fee=taker_fee,
                )
                if not out.filled:
                    continue
                rows.append({
                    "direction": plan.direction,
                    "cycle_position": plan.cycle_position,
                    "diag_direction": plan.diag_direction,
                    "_bad_session": _bad_session_ms(plan.record_ms),
                    "net": out.net, "risk_usdt": args.risk_usdt,
                })
            if be_name == "off" and not gtx:
                base_rows = rows
            for gname, gpred in gate_filters:
                sub = [r for r in rows if gpred(r)]
                if not sub:
                    continue
                net = sum(r["net"] for r in sub)
                risk = sum(r["risk_usdt"] for r in sub)
                wr = sum(1 for r in sub if r["net"] > 0) / len(sub) * 100
                print("%-6s %-4s | %-22s %5d %+9.2f %6.0f%% %+9.2f" %
                      (be_name, "gtx" if gtx else "gtc", gname,
                       len(sub), net, wr, net / risk if risk else 0.0))
    # 时段过滤与 trending_tr 的独立性: 若高度重叠, 两闸门叠加无增量。
    if base_rows:
        tt = sum(1 for r in base_rows if r["cycle_position"] == "trending_tr")
        bs = sum(1 for r in base_rows if r["_bad_session"])
        both = sum(1 for r in base_rows
                   if r["cycle_position"] == "trending_tr" and r["_bad_session"])
        print("时段×trending_tr 重叠度(成交): trending_tr %d, 04-12时 %d, 两者皆 %d "
              "(交集占比 %.0f%%)" % (tt, bs, both, (both / min(tt, bs) * 100) if min(tt, bs) else 0))
    print()


def fee_rate_from_outcomes(path):
    """从真实成交反推往返手续费率(手续费 / 名义)的中位数."""
    if not path.exists():
        return FALLBACK_FEE_RATE
    rates = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                notional = float(row["entry_avg"]) * float(row["qty"])
                fees = abs(float(row["fees_usdt"]))
            except (KeyError, TypeError, ValueError):
                continue
            if notional > 0 and fees > 0:
                rates.append(fees / notional)
    if not rates:
        return FALLBACK_FEE_RATE
    rates.sort()
    return rates[len(rates) // 2]


def summarize(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    print("%-16s %5s %5s %6s %10s %8s %7s %9s" %
          (key, "计划", "成交", "成交率", "净U", "均U", "胜率", "净/风险"))
    for name in sorted(groups, key=lambda k: sum(r["net"] for r in groups[k])):
        bucket = groups[name]
        filled = [r for r in bucket if r["filled"]]
        net = sum(r["net"] for r in filled)
        risk = sum(r["risk_usdt"] for r in filled)
        wins = sum(1 for r in filled if r["net"] > 0)
        print("%-16s %5d %5d %5.0f%% %10.2f %8.2f %6.0f%% %9.3f" % (
            name, len(bucket), len(filled),
            (len(filled) / len(bucket) * 100) if bucket else 0,
            net, (net / len(filled)) if filled else 0,
            (wins / len(filled) * 100) if filled else 0,
            (net / risk) if risk else 0))
    total = [r for r in rows if r["filled"]]
    net = sum(r["net"] for r in total)
    risk = sum(r["risk_usdt"] for r in total)
    print("%-16s %5d %5d %5.0f%% %10.2f %8.2f %6.0f%% %9.3f" % (
        "合计", len(rows), len(total),
        (len(total) / len(rows) * 100) if rows else 0,
        net, (net / len(total)) if total else 0,
        (sum(1 for r in total if r["net"] > 0) / len(total) * 100) if total else 0,
        (net / risk) if risk else 0))


def generate_baseline_plans(timeline, symbol, timeframe, *,
                            days, ema_period=20, target_rr=2.0,
                            stop_atr_mult=1.0, per_day=1):
    """无模型对照流: 机械趋势回踩信号, 与模型计划共用同一模拟引擎.

    规则(全程序化, 零模型输入):
      - EMA%d 趋势过滤: 收盘 > EMA 且 EMA 较上一根上升 → 只做多; 反之只做空;
      - 回踩触发: 当根最低价触及 EMA(多) / 最高价触及 EMA(空) → 计划挂单;
      - entry = EMA 值, stop = entry ∓ stop_atr_mult×ATR14, target = entry ± target_rr×risk;
      - 同一 symbol/timeframe 同时只持一仓, 每日至多 per_day 笔.
    目的不是做策略, 而是给"模型计划"一个同市况、同成本的哑巴对照组:
    若哑巴组也大亏 → 亏损主要来自市况/成本; 若哑巴组明显好于模型 → 模型是负边际本体.
    """
    plans: list[Plan] = []
    if len(timeline) < ema_period + 60:
        return plans
    closes = [bar[4] for bar in timeline]
    # EMA 序列
    k = 2.0 / (ema_period + 1)
    ema = [closes[0]]
    for c in closes[1:]:
        ema.append(ema[-1] + k * (c - ema[-1]))
    # ATR14 序列(简化: 真实波幅均值)
    atrs: list[float | None] = [None] * len(timeline)
    trs: list[float] = []
    for i in range(1, len(timeline)):
        _ts, o, h, low, c = timeline[i]
        prev_c = timeline[i - 1][4]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
        window = trs[-14:]
        atrs[i] = sum(window) / len(window)
    cutoff = _cutoff_from_days(days) if days else None
    cooldown_until_idx = -1
    day_key = None
    day_count = 0
    for i in range(ema_period + 1, len(timeline) - 5):
        ts = timeline[i][0]
        if cutoff and datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d") < cutoff:
            continue
        e_now, e_prev = ema[i], ema[i - 1]
        c_now = closes[i]
        up = c_now > e_now > e_prev
        down = c_now < e_now < e_prev
        if not up and not down:
            continue
        dk = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d")
        if dk != day_key:
            day_key, day_count = dk, 0
        if day_count >= per_day or i <= cooldown_until_idx:
            continue
        side = 1 if up else -1
        entry = ema[i]
        atr = atrs[i] or 0.0
        if atr <= 0:
            continue
        stop = entry - side * stop_atr_mult * atr
        target = entry + side * target_rr * (stop_atr_mult * atr)
        plans.append(Plan(
            path="baseline", symbol=symbol, timeframe=timeframe,
            record_ms=ts, order_type="限价单",
            direction="做多" if side == 1 else "做空",
            entry=entry, stop=stop, target=target, target2=None,
            cycle_position="baseline", diag_direction="baseline",
            trade_conf=None, diag_conf=None, est_win_rate=None,
        ))
        day_count += 1
        # 持仓冷却: 粗略按 8 根 K 计, 与模拟互斥近似
        cooldown_until_idx = i + 8
    return plans


def run_baseline(args):
    """哑巴对照组: 同引擎同成本, 机械规则 vs 模型计划. --baseline 触发."""
    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    plans = load_plans(PENDING_DIR, days=args.days, symbols=symbols)
    timelines = merge_timelines(plans)
    fee_rate = args.fee_rate if args.fee_rate is not None else fee_rate_from_outcomes(OUTCOMES_CSV)
    maker_fee = args.maker_fee if args.maker_fee is not None else fee_rate * 0.4
    taker_fee = args.taker_fee if args.taker_fee is not None else fee_rate * 0.8
    be_tr = None
    if getattr(args, "baseline_be", "0.5r") == "0.5r":
        be_tr = 0.5
    elif getattr(args, "baseline_be", "") == "1r":
        be_tr = 1.0

    def _simulate(plan, tl):
        return simulate_plan(
            plan, tl, risk_usdt=args.risk_usdt, fee_rate=fee_rate,
            leverage=args.leverage, margin_usdt=args.margin_usdt,
            partial_pct=args.partial_pct, timeout_min=args.timeout_min,
            time_stop_min=args.time_stop_min,
            min_stop_pct=args.min_stop_pct, min_stop_atr=args.min_stop_atr,
            be_trigger=be_tr, entry_gtx=True,
            maker_fee=maker_fee, taker_fee=taker_fee,
        )

    print("=== 无模型对照流 (EMA%d 回踩, RR=%.1f, be=%s, gtx) ==="
          % (20, args.baseline_rr, getattr(args, "baseline_be", "0.5r")))
    b_rows = []
    for (sym, tf), tl in sorted(timelines.items()):
        b_plans = generate_baseline_plans(
            tl, sym, tf, days=args.days, target_rr=args.baseline_rr)
        for bp in b_plans:
            out = _simulate(bp, tl)
            if out.filled:
                b_rows.append((sym, out.net))
    if not b_rows:
        print("  基线流无成交")
        return 0
    net = sum(n for _s, n in b_rows)
    wr = sum(1 for _s, n in b_rows if n > 0) / len(b_rows) * 100
    print("  成交 %d 笔 | 净 %+.2fU | 均 %+.2fU | 胜率 %.0f%% | 净/风险 %+.2f"
          % (len(b_rows), net, net / len(b_rows), wr,
             net / (len(b_rows) * args.risk_usdt)))
    print("  对照: 同窗口模型计划(见 --replay 输出) — 两者差值 = 模型贡献的方向性估计")
    return 0


def run_replay(args):
    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    plans = load_plans(PENDING_DIR, days=args.days, symbols=symbols)
    print("载入计划单:", len(plans))
    if not plans:
        return 0
    timelines = merge_timelines(plans)
    print("K 线时间线:", {k[0] + "/" + k[1]: len(v) for k, v in sorted(timelines.items())})
    fee_rate = args.fee_rate if args.fee_rate is not None else fee_rate_from_outcomes(OUTCOMES_CSV)
    print("手续费率(往返, 名义占比): %.5f" % fee_rate)
    trends = attach_trends(plans, args.trend_days) if args.trend_days > 0 else {}
    if trends:
        tagged = sum(1 for v in trends.values() if v is not None)
        print("日线趋势标签: %d 天, 覆盖 %d/%d 条计划单" % (
            args.trend_days, tagged, len(plans)))

    if getattr(args, "matrix", False):
        maker_fee = args.maker_fee if args.maker_fee is not None else fee_rate * 0.4
        taker_fee = args.taker_fee if args.taker_fee is not None else fee_rate * 0.8
        report_matrix(
            [(p, timelines.get((p.symbol, p.timeframe), [])) for p in plans],
            args=args, fee_rate=fee_rate, maker_fee=maker_fee, taker_fee=taker_fee,
        )

    rows = []
    for plan in plans:
        out = simulate_plan(
            plan, timelines.get((plan.symbol, plan.timeframe), []),
            risk_usdt=args.risk_usdt, fee_rate=fee_rate, leverage=args.leverage,
            margin_usdt=args.margin_usdt, partial_pct=args.partial_pct,
            timeout_min=args.timeout_min, time_stop_min=args.time_stop_min,
            min_stop_pct=args.min_stop_pct, min_stop_atr=args.min_stop_atr)
        rows.append({
            "record_time": datetime.fromtimestamp(plan.record_ms / 1000, TZ8).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": plan.symbol, "timeframe": plan.timeframe,
            "cycle_position": plan.cycle_position, "diag_direction": plan.diag_direction,
            "direction": plan.direction, "order_type": plan.order_type,
            "trade_conf": plan.trade_conf, "diag_conf": plan.diag_conf,
            "est_win_rate": plan.est_win_rate,
            "filled": out.filled, "reason": out.reason,
            "entry": round(out.entry, 8),
            "exit_avg": None if out.exit_avg is None else round(out.exit_avg, 8),
            "stop": plan.stop, "target": plan.target,
            "rr": round(abs(plan.target - plan.entry) / abs(plan.entry - plan.stop), 4)
            if abs(plan.entry - plan.stop) > 0 else None,
            "notional": round(out.notional, 2),
            "gross": round(out.gross, 4), "fees": round(out.fees, 4),
            "net": round(out.net, 4), "win_r": round(out.win_r, 4),
            "risk_usdt": args.risk_usdt, "bars_held": out.bars_held,
            "trend_pct": trends.get(os.path.basename(plan.path)),
            "record_file": os.path.basename(plan.path),
        })

    print()
    print("=== 结局分布 ===", dict(Counter(r["reason"] for r in rows)))
    filled = [r for r in rows if r["filled"]]
    print("成交 %d 笔 / 计划 %d 笔 = %.1f%%" %
          (len(filled), len(rows), (len(filled) / len(rows) * 100) if rows else 0))
    if filled:
        net_total = sum(r["net"] for r in filled)
        fee_total = sum(r["fees"] for r in filled)
        notionals = sorted(r["notional"] for r in filled)
        print("净 %.2fU  均 %.2fU  胜率 %.1f%%  手续费 %.2fU" % (
            net_total, net_total / len(filled),
            sum(1 for r in filled if r["net"] > 0) / len(filled) * 100, fee_total))
        print("名义: 中位 %.0fU  均值 %.0fU  最大 %.0fU   手续费/毛亏 %.0f%%" % (
            notionals[len(notionals) // 2], sum(notionals) / len(notionals), notionals[-1],
            (fee_total / abs(net_total) * 100) if abs(net_total) > 1e-9 else 0))
    print()
    print("=== 按周期位置 ===")
    summarize(rows, "cycle_position")
    print()
    print("=== 按方向 ===")
    summarize(rows, "direction")
    if args.variants:
        report_variants(rows, _float_list(args.trend_neutral))

    out_csv = Path(args.csv) if args.csv else (ROOT / "logs" / "shadow_rows.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print()
    print("明细:", out_csv)
    return 0


def run_calibrate(args):
    """在真实成交上跑同一套模拟, 量化模拟与真实的偏差."""
    if not OUTCOMES_CSV.exists():
        print("缺少", OUTCOMES_CSV)
        return 0
    plans = load_plans(PENDING_DIR, days=args.days)
    by_file = {os.path.basename(p.path): p for p in plans}
    timelines = merge_timelines(plans)
    fee_rate = fee_rate_from_outcomes(OUTCOMES_CSV)
    print("真实成交口径手续费率: %.5f" % fee_rate)

    diffs, fills, reasons = [], 0, Counter()
    if args.verbose:
        print("%-10s %-22s %10s %10s %10s" % ("symbol", "真实/模拟结局", "真实净U", "模拟净U", "差"))
    with open(OUTCOMES_CSV, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = os.path.basename(row.get("record_file") or "")
            plan = by_file.get(name)
            if plan is None:
                continue
            out = simulate_plan(
                plan, timelines.get((plan.symbol, plan.timeframe), []),
                risk_usdt=args.risk_usdt, fee_rate=fee_rate, leverage=args.leverage,
                margin_usdt=args.margin_usdt, partial_pct=args.partial_pct,
                timeout_min=args.timeout_min, time_stop_min=args.time_stop_min,
                min_stop_pct=args.min_stop_pct, min_stop_atr=args.min_stop_atr)
            try:
                real_net = float(row["net_usdt"])
            except (KeyError, TypeError, ValueError):
                continue
            fills += 1 if out.filled else 0
            reasons[str(row.get("close_reason")) + "/" + out.reason] += 1
            diffs.append(out.net - real_net)
            if args.verbose:
                print("%-10s %-22s %10.2f %10.2f %+10.2f" % (
                    row.get("symbol"), str(row.get("close_reason")) + "/" + out.reason,
                    real_net, out.net, out.net - real_net))
    if not diffs:
        print("没有可配对的真实成交(record_file 缺失或不在 pending 中)")
        return 0
    diffs.sort()
    n = len(diffs)
    worst = diffs[-1] if abs(diffs[-1]) > abs(diffs[0]) else diffs[0]
    print()
    print("配对 %d 笔, 模拟判定成交 %d 笔" % (n, fills))
    print("模拟净 - 真实净:  中位 %+.3fU   均值 %+.3fU   最大偏差 %+.3fU" % (
        diffs[n // 2], sum(diffs) / n, worst))
    print("结局对照 (真实/模拟):", dict(reasons.most_common(8)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="跑影子回放")
    ap.add_argument("--calibrate", action="store_true", help="用真实成交校准模拟器")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--csv", default="")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--variants", action="store_true", help="输出过滤变体对比表")
    ap.add_argument("--trend-days", type=int, default=0,
                    help="变体表贴日线趋势标签的回看天数(0=不拉日线)")
    ap.add_argument("--trend-neutral", default="3.0",
                    help="逆势判定中性带(%%)列表, 如 1,2,3")
    ap.add_argument("--risk-usdt", type=float, default=DEFAULT_RISK_USDT)
    ap.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    ap.add_argument("--margin-usdt", type=float, default=DEFAULT_MARGIN_USDT)
    ap.add_argument("--partial-pct", type=float, default=DEFAULT_PARTIAL_PCT)
    ap.add_argument("--timeout-min", type=float, default=DEFAULT_TIMEOUT_MIN)
    ap.add_argument("--time-stop-min", type=float, default=DEFAULT_TIME_STOP_MIN)
    ap.add_argument("--fee-rate", type=float, default=None)
    ap.add_argument("--baseline", action="store_true",
                    help="无模型对照流: 机械规则信号走同一引擎, 量化模型贡献")
    ap.add_argument("--baseline-rr", type=float, default=2.0)
    ap.add_argument("--baseline-be", default="0.5r", choices=["off", "0.5r", "1r"])
    ap.add_argument("--matrix", action="store_true",
                    help="输出 保本移损×入场方式×环境门控 组合矩阵")
    ap.add_argument("--maker-fee", type=float, default=None,
                    help="maker 费率(单边); 默认 fee_rate*0.4")
    ap.add_argument("--taker-fee", type=float, default=None,
                    help="taker 费率(单边); 默认 fee_rate*0.8")
    ap.add_argument("--min-stop-pct", type=float, default=0.2,
                    help="止损距离下限(%%), 与 min_stop_distance_pct 同步")
    ap.add_argument("--min-stop-atr", type=float, default=0.7,
                    help="止损距离的 ATR 倍数下限, 与 min_stop_atr_multiple 同步")
    args = ap.parse_args()
    if args.calibrate:
        return run_calibrate(args)
    if args.baseline:
        return run_baseline(args)
    return run_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
