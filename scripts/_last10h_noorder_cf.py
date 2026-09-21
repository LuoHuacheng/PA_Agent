#!/usr/bin/env python3
"""过去 N 小时「信号未下单」归因 + 反事实回放 (只读, 不改线上任何东西).

三类样本
  1) 计划单被闸门/超时拦下  -> 用真实 entry/stop/TP 回放 (口径A 挂单 / 口径B 市价)
  2) §10.3 交易者方程不通过  -> 从决策文本里抽取模型自己拟定的三价, 再回放
  3) §9.0P 无方案观望        -> 无三价可抽, 用统一配方做系统性反事实
                               (顺势/逆势 x 挂单/市价), MFE/MAE 一起给出

用法
    PYTHONPATH=. .venv/bin/python scripts/_last10h_noorder_cf.py --hours 10
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.shadow_replay import (  # noqa: E402
    KLINE_EPOCH_OFFSET_MS,
    Plan,
    bootstrap_mean_ci,
    simulate_plan,
)

TZ8 = timezone(timedelta(hours=8))
PENDING = ROOT / "records" / "pending"
FEE_RATE = 0.0006
COMMON = dict(risk_usdt=4.0, fee_rate=FEE_RATE, leverage=20, margin_usdt=250.0,
              partial_pct=50.0, timeout_min=120.0, time_stop_min=720.0,
              min_stop_pct=0.2, min_stop_atr=1.2)
BAR_MS = 1_800_000
NUM = r"(\d+(?:\.\d+)?)"


# ---------------------------------------------------------------- 数据装载
def load_window(hours):
    lo = time.time() * 1000 - hours * 3600 * 1000
    out = []
    for path in glob.glob(str(PENDING / "*.json")):
        try:
            mt = os.path.getmtime(path) * 1000
        except OSError:
            continue
        if mt < lo:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        meta = doc.get("meta") or {}
        ts = meta.get("timestamp_local_ms") or int(mt)
        if ts < lo:
            continue
        out.append((path, doc, meta, int(ts)))
    out.sort(key=lambda r: r[3])
    return out


def build_timelines(hours=96.0):
    lo = time.time() * 1000 - hours * 3600 * 1000
    store = collections.defaultdict(dict)
    for path in glob.glob(str(PENDING / "*.json")):
        try:
            if os.path.getmtime(path) * 1000 < lo:
                continue
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        meta = doc.get("meta") or {}
        sym, tf = meta.get("symbol"), meta.get("timeframe")
        if not sym or not tf:
            continue
        for b in doc.get("kline_data") or []:
            try:
                ts = int(b["ts_open"]) - KLINE_EPOCH_OFFSET_MS
                store[(sym, tf)][ts] = (ts, float(b["open"]), float(b["high"]),
                                        float(b["low"]), float(b["close"]))
            except (KeyError, TypeError, ValueError):
                continue
    return {k: sorted(v.values()) for k, v in store.items()}


def bars_upto(doc):
    out = []
    for b in doc.get("kline_data") or []:
        try:
            out.append((int(b["ts_open"]) - KLINE_EPOCH_OFFSET_MS, float(b["open"]),
                        float(b["high"]), float(b["low"]), float(b["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = [max(b[2] - b[3], abs(b[2] - bars[i - 1][4]), abs(b[3] - bars[i - 1][4]))
           for i, b in enumerate(bars) if i]
    return sum(trs[-period:]) / period


# ---------------------------------------------------------------- 三价抽取
def _hits(text, keys, before=False):
    """[(pos, value)] —— 关键词前/后最近的数字, 保留出现位置."""
    out = []
    for k in keys:
        pat = (NUM + r"[^\d]{0,14}?" + k) if before else (k + r"[^\d]{0,14}?" + NUM)
        for m in re.finditer(pat, text or ""):
            try:
                out.append((m.start(), float(m.group(1))))
            except ValueError:
                continue
    return sorted(out)


def _first_in(hits, lo, hi, taken=None):
    for pos, val in hits:
        if lo < val < hi and (taken is None or abs(val - taken[1]) > 1e-9):
            return pos, val
    return None


def extract_triple(doc, tp_pick="first"):
    """抽取模型自己拟定的主方案三价 (做多几何, 首选 §9.0P/§10.1 描述的那一版).

    tp_pick = "first" 取文本里第一个合格 TP1(通常是"最近结构目标");
    tp_pick = "best"  取区间内最远的 TP1(乐观口径: 放开后允许把目标推到程序下限之外).
    """
    s2 = doc.get("stage2_decision") or {}
    trace = {n.get("node_id"): (n.get("reason") or "") for n in (s2.get("decision_trace") or [])}
    dec = s2.get("decision") or {}
    text = "\n".join([trace.get("9.0P", ""), trace.get("10.1", ""),
                      trace.get("10.3", ""), dec.get("reasoning") or ""])
    if not text.strip():
        return None
    ent_hits = _hits(text, [r"entry", r"入场价", r"入场", r"回踩", r"计划在"])
    ent_hits = [(p, v) for p, v in ent_hits if v > 0]
    stops = _hits(text, [r"止损价", r"止损位", r"止损", r"stop"])
    tps1 = sorted(_hits(text, [r"TP1", r"止盈1", r"目标1", r"第一目标"])
                  + _hits(text, [r"TP1", r"止盈1", r"目标1", r"第一目标"], before=True))
    tps2 = sorted(_hits(text, [r"TP2", r"止盈2", r"目标2", r"第二目标"])
                  + _hits(text, [r"TP2", r"止盈2", r"目标2", r"第二目标"], before=True))
    for _p, entry in ent_hits:
        s = _first_in(stops, entry * 0.5, entry)
        if not s:
            continue
        valid1 = [(p, v) for p, v in tps1 if entry * 1.0005 < v < entry * 1.6]
        if not valid1:
            continue
        first = valid1[0]
        if tp_pick == "best":
            near = [(p, v) for p, v in valid1 if v <= entry * 1.15]
            t1 = max(near or [first], key=lambda x: x[1])
        else:
            t1 = first
        stop, tp1 = s[1], t1[1]
        if not (0.2 < (entry - stop) / entry * 100 < 5.0):
            continue
        tp2 = None
        t2 = _first_in(tps2, tp1 * 1.0005, entry * 1.8)
        if t2:
            tp2 = t2[1]
        return dict(entry=entry, stop=stop, tp1=tp1, tp2=tp2)
    return None


# ---------------------------------------------------------------- 回放
def make_plan(path, meta, ts, entry, stop, tp1, tp2, direction, order_type, bars, cycle, conf=None):
    return Plan(path=os.path.basename(path), symbol=meta.get("symbol") or "",
                timeframe=meta.get("timeframe") or "30m", record_ms=ts,
                order_type=order_type, direction=direction, entry=float(entry),
                stop=float(stop), target=float(tp1), target2=tp2,
                cycle_position=cycle or "unknown", diag_direction="unknown",
                trade_conf=conf, diag_conf=None, est_win_rate=None, bars=bars)


def run(plan, timeline, **kw):
    params = dict(COMMON)
    params.update(kw)
    return simulate_plan(plan, timeline, **params)


def stats(label, outs, n_all=None):
    nets = [o.net for o in outs]
    filled = [o for o in outs if o.filled]
    wins = sum(1 for o in filled if o.net > 0)
    lo, hi = bootstrap_mean_ci(nets) if nets else (0.0, 0.0)
    n = n_all if n_all is not None else len(outs)
    mean = (sum(nets) / n) if n else 0.0
    rate = f"{wins}/{len(filled)}" if filled else "-"
    print(f"    {label:<32} n={n:>3} 成交{len(filled):>3} 胜{rate:>7} "
          f"净{sum(nets):>+8.2f}U 均{mean:>+6.2f}U 90%CI[{lo:>+6.2f},{hi:>+6.2f}]")
    return dict(n=n, filled=len(filled), wins=wins, net=round(sum(nets), 2),
                mean=round(mean, 3), ci=[round(lo, 2), round(hi, 2)])


def mfe_mae(timeline, ts, direction, entry, n_bars=24):
    future = [b for b in timeline if b[0] > ts][:n_bars]
    if not future:
        return None
    side = 1 if direction == "做多" else -1
    best = max(b[2] for b in future) if side == 1 else min(b[3] for b in future)
    worst = min(b[3] for b in future) if side == 1 else max(b[2] for b in future)
    return (best - entry) / entry * 100 * side, (worst - entry) / entry * 100 * side


def fmt(o):
    return "无数据" if o is None else f"{o.reason:<16} {o.net:+7.2f}U"


def dedup(rows, gap_hours):
    """按品种贪心去重叠: 同一品种两笔决策至少间隔 gap_hours."""
    seen, keep = {}, []
    for item in rows:
        sym, ts = item[2].get("symbol"), item[3]
        if sym in seen and ts - seen[sym] < gap_hours * 3600_000:
            continue
        seen[sym] = ts
        keep.append(item)
    return keep


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=10.0)
    ap.add_argument("--json-out", default="logs/_last10h_noorder_cf.json")
    ap.add_argument("--until", default=None, help="窗口右端(本地时间, 如 2026-09-21T10:05); 缺省=最新一条决策")
    args = ap.parse_args()

    rows = load_window(args.hours + 2)
    if args.until:
        end = int(datetime.fromisoformat(args.until).replace(tzinfo=TZ8).timestamp() * 1000)
    elif rows:
        end = max(r[3] for r in rows)
    else:
        end = int(time.time() * 1000)
    rows = [r for r in rows if end - args.hours * 3600 * 1000 <= r[3] <= end]
    tl = {k: [b for b in v if b[0] <= end + BAR_MS / 2] for k, v in build_timelines(240.0).items()}
    if rows:
        a = datetime.fromtimestamp(rows[0][3] / 1000, TZ8).strftime("%m-%d %H:%M")
        b = datetime.fromtimestamp(rows[-1][3] / 1000, TZ8).strftime("%m-%d %H:%M")
    else:
        a = b = "-"
    print(f"窗口: {a} ~ {b} (锚定最新一条决策回溯 {args.hours:g} 小时) | 决策 {len(rows)} 条 | "
          f"K线覆盖 {', '.join(f'{k[0]} {len(v)}' for k, v in sorted(tl.items()) if k[0] in ('BTCUSDT','ETHUSDT','SOLUSDT','LINKUSDT','ZECUSDT'))}\n")

    planned, eq_reject, wait, broken = [], [], [], []
    for item in rows:
        doc = item[1]
        s2 = doc.get("stage2_decision") or {}
        dec = (s2 or {}).get("decision") or {}
        node = (s2.get("terminal") or {}).get("node_id")
        if not s2.get("decision"):
            broken.append(item)
        elif str(dec.get("order_type")) in ("限价单", "突破单"):
            planned.append(item)
        elif node == "10.3":
            eq_reject.append(item)
        else:
            wait.append(item)
    print(f"分类: 计划单(限价/突破)={len(planned)}  §10.3方程拒={len(eq_reject)}  "
          f"§9.0P无方案观望={len(wait)}  异常(网络)= {len(broken)}\n")
    report = dict(window_hours=args.hours, counts=dict(
        planned=len(planned), eq_reject=len(eq_reject), wait=len(wait), broken=len(broken)))

    # ---- 行情背景
    print("=" * 100)
    print("【0】窗口行情背景 (30m 收盘, 含决策点之后的走势)")
    print("=" * 100)
    bg = {}
    for (sym, tf), bars in sorted(tl.items()):
        if tf != "30m" or len(bars) < 3:
            continue
        win = [b for b in bars if b[0] >= rows[0][3] - 3600_000]
        if len(win) < 2:
            continue
        first, last = win[0][4], win[-1][4]
        hi = max(b[2] for b in win)
        low = min(b[3] for b in win)
        chg = (last - first) / first * 100
        if sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "ZECUSDT"):
            print(f"    {sym:<9} 首{first:<10.4f} 末{last:<10.4f} 区间涨跌{chg:>+6.2f}% "
                  f"高{hi:<10.4f} 低{low:<10.4f} 振幅{(hi - low) / low * 100:>5.2f}%")
            bg[sym] = dict(change_pct=round(chg, 2), high=hi, low=low)
    report["background"] = bg
    print()

    # ---- 1. 计划单
    print("=" * 100)
    print("【1】已产出计划单, 但被闸门拦下 / 挂单超时未成交 —— 真实三价回放")
    print("=" * 100)
    res_planned, det_planned = collections.defaultdict(list), []
    for path, doc, meta, ts in planned:
        s2 = doc["stage2_decision"]
        dec = s2["decision"]
        ex = s2.get("execution") or {}
        bars = bars_upto(doc)
        entry, stop = float(dec["entry_price"]), float(dec["stop_loss_price"])
        tp1 = float(dec["take_profit_price"])
        tp2 = float(dec["take_profit_price_2"]) if dec.get("take_profit_price_2") else None
        direction = str(dec.get("order_direction"))
        timeline = tl.get((meta["symbol"], meta.get("timeframe")), [])
        tag = str(ex.get("status") or "?")
        if tag == "pending":
            tag = "挂单未成交(120min超时撤单)"
        p = make_plan(path, meta, ts, entry, stop, tp1, tp2, direction,
                      str(dec.get("order_type")), bars,
                      (doc.get("stage1_diagnosis") or {}).get("cycle_position"),
                      dec.get("trade_confidence"))
        oA = run(p, timeline)
        seq = [b for b in timeline if b[0] <= ts]
        oB = run(dataclasses.replace(p, entry=seq[-1][4], order_type="突破单"), timeline) if seq else None
        mm = mfe_mae(timeline, ts, direction, seq[-1][4]) if seq else None
        res_planned[tag].append((oA, oB))
        gates = [str(g).split(":")[0] for g in (ex.get("gate_reasons") or [])]
        t = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%H:%M")
        print(f"  {t} {meta['symbol']:<9} {direction} entry={entry:<11} stop={stop:<11} tp1={tp1:<11} "
              f"| {tag} {'/'.join(gates)}")
        print(f"        口径A挂单 {fmt(oA)} | 口径B市价 {fmt(oB)} | "
              f"MFE {mm[0]:+.2f}% MAE {mm[1]:+.2f}%" if mm else "        (无后续K线)")
        det_planned.append(dict(time=t, sym=meta["symbol"], entry=entry, stop=stop, tp1=tp1,
                                tag=tag, gates=gates, netA=round(oA.net, 2),
                                reasonA=oA.reason, netB=round(oB.net, 2) if oB else None,
                                reasonB=oB.reason if oB else None))
    print()
    rep_planned = {}
    for tag, items in sorted(res_planned.items()):
        print(f"  分组 {tag}:")
        rep_planned[tag + "|口径A挂单"] = stats("口径A(按原计划挂单)", [a for a, _ in items])
        rep_planned[tag + "|口径B市价"] = stats("口径B(决策时市价)", [b for _, b in items if b])
    report["planned"] = dict(groups=rep_planned, detail=det_planned)

    # ---- 2. §10.3
    print()
    print("=" * 100)
    print("【2】§10.3 交易者方程不通过 —— 模型自己拟好的三价, 如果放行")
    print("=" * 100)
    outsA, outsB, outsBest, det_eq = [], [], [], []
    for path, doc, meta, ts in eq_reject:
        tri = extract_triple(doc)
        timeline = tl.get((meta["symbol"], meta.get("timeframe")), [])
        diag = doc.get("stage1_diagnosis") or {}
        dec = (doc["stage2_decision"] or {}).get("decision") or {}
        t = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%H:%M")
        if not tri or not timeline:
            print(f"  {t} {meta['symbol']:<9} 未能抽取三价或K线缺失 -> 跳过")
            det_eq.append(dict(time=t, sym=meta["symbol"], skip=True))
            continue
        best = extract_triple(doc, "best") or tri
        bars = bars_upto(doc)
        p = make_plan(path, meta, ts, tri["entry"], tri["stop"], tri["tp1"], tri["tp2"],
                      "做多", "限价单", bars, diag.get("cycle_position"),
                      dec.get("trade_confidence"))
        oA = run(p, timeline)
        seq = [b for b in timeline if b[0] <= ts]
        oB = run(dataclasses.replace(p, entry=seq[-1][4], order_type="突破单"), timeline) if seq else None
        oX = run(dataclasses.replace(p, target=best["tp1"], target2=best["tp2"]), timeline)
        mm = mfe_mae(timeline, ts, "做多", tri["entry"])
        outsA.append(oA)
        if oB:
            outsB.append(oB)
        outsBest.append(oX)
        extra = f" | MFE {mm[0]:+.2f}% MAE {mm[1]:+.2f}%" if mm else " | (无后续K线)"
        print(f"  {t} {meta['symbol']:<9} entry={tri['entry']:<11} stop={tri['stop']:<11} "
              f"tp1={tri['tp1']:<11} | 挂单 {fmt(oA)} | 市价 {fmt(oB)} | "
              f"乐观TP1({best['tp1']}) {fmt(oX)}{extra}")
        det_eq.append(dict(time=t, sym=meta["symbol"], **tri, netA=round(oA.net, 2),
                           reasonA=oA.reason, netB=round(oB.net, 2) if oB else None,
                           reasonB=oB.reason if oB else None, tp1_best=best["tp1"],
                           netX=round(oX.net, 2), mfe=round(mm[0], 2) if mm else None,
                           mae=round(mm[1], 2) if mm else None))
    print()
    report["eq_reject"] = dict(
        A=stats("口径A(按模型拟价挂单)", outsA),
        B=stats("口径B(决策时市价)", outsB),
        X=stats("口径A2(挂单+乐观TP1)", outsBest), detail=det_eq)

    # ---- 2b. 观望组拦因关键词归因
    print()
    print("=" * 100)
    print("【2b】观望组(§9.0P)真正卡在哪一步 —— 关键词归因 (一条可命中多类)")
    print("=" * 100)
    KEYWORDS = [
        ("无合格信号棒(§9.0=否)", [r"无合格.{0,6}信号棒", r"没有.{0,8}信号棒", r"信号棒.{0,6}无效"]),
        ("逆势禁令(禁止做空/逆势)", [r"逆势禁止", r"不得做空", r"禁止做空", r"逆势.{0,4}禁止", r"属逆势", r"逆势单"]),
        ("位置不佳(区间中部/上三分之一)", [r"区间中部", r"中部", r"上三分之一", r"分位0\.[5-9]", r"贴近上沿"]),
        ("止损下限/结构止损无锚点", [r"止损下限", r"最小止损", r"无.{0,6}止损.{0,4}锚点", r"止损.{0,6}无依据", r"止损过大"]),
        ("TP1下限/无合格目标位", [r"TP1下限", r"最小止盈", r"目标.{0,6}(不足|太近)", r"未达.{0,6}下限", r"找不到.{0,6}目标"]),
        ("追高/追第一根突破棒", [r"追第?一?根?突破棒", r"追高", r"spike", r"禁止追"]),
        ("铁丝网/无交易环境", [r"铁丝网", r"barbwire", r"无交易环境", r"不适合交易"]),
        ("方向中性/不明", [r"方向为中性", r"方向.{0,2}中性", r"中性.{0,4}只允许"]),
        ("RR/方程/几何不合规", [r"盈亏比", r"交易者方程", r"几何"]),
    ]
    tally = collections.Counter()
    per_sym = collections.Counter()
    for _, doc, meta, _ts in wait:
        s2 = doc["stage2_decision"]
        tr = {n.get("node_id"): (n.get("reason") or "") for n in (s2.get("decision_trace") or [])}
        text = (tr.get("9.0P", "") + "\n" + ((s2.get("decision") or {}).get("reasoning") or ""))
        per_sym[meta.get("symbol")] += 1
        for label, pats in KEYWORDS:
            if any(re.search(p, text) for p in pats):
                tally[label] += 1
    for label, cnt in tally.most_common():
        print(f"    {label:<34} {cnt:>3}/{len(wait)}")
    print("    品种分布: " + ", ".join(f"{k}={v}" for k, v in per_sym.most_common()))
    report["wait_taxonomy"] = dict(tally=dict(tally), per_symbol=dict(per_sym))

    # ---- 3. 观望: 系统性反事实
    print()
    print("=" * 100)
    print("【3】§9.0P 无方案观望 —— 无三价, 统一配方反事实")
    print("    配方: 结构止损=近10根极值∓0.2ATR (再套线上最小止损 1.2ATR / 0.2%)")
    print("          TP1=1R TP2=2R 半仓50% 超时120min 时间止损720min risk 4U 手续费6bp")
    print("=" * 100)
    variants = collections.OrderedDict((k, []) for k in
                                       ("顺势市价", "顺势挂单", "逆势市价", "逆势挂单"))
    detail_wait = []
    keep_wait = []
    for path, doc, meta, ts in wait:
        diag = doc.get("stage1_diagnosis") or {}
        dec = (doc["stage2_decision"] or {}).get("decision") or {}
        primary = str((diag.get("trend_context") or {}).get("primary_direction")
                      or diag.get("direction") or "")
        bars = bars_upto(doc)
        timeline = tl.get((meta["symbol"], meta.get("timeframe")), [])
        seq = [b for b in timeline if b[0] <= ts]
        future = [b for b in timeline if b[0] > ts]
        a = atr(bars)
        if not primary or not bars or not seq or len(future) < 2 or not a:
            continue
        keep_wait.append((path, doc, meta, ts))
        close = seq[-1][4]
        for trend, direction in (("顺势", "做多" if primary == "bullish" else "做空"),
                                 ("逆势", "做空" if primary == "bullish" else "做多")):
            side = 1 if direction == "做多" else -1
            swing = min(b[3] for b in bars[-10:]) if side == 1 else max(b[2] for b in bars[-10:])
            stop = swing - 0.2 * a if side == 1 else swing + 0.2 * a
            risk = abs(close - stop)
            if risk <= 0:
                continue
            cmn = dict(bars=bars, cycle=diag.get("cycle_position"), conf=dec.get("trade_confidence"))
            om = run(make_plan(
                path, meta, ts, close, stop, close + side * risk, close + side * 2 * risk,
                direction, "突破单", **cmn), timeline)
            lim = close - side * 0.5 * a
            ol = run(make_plan(
                path, meta, ts, lim, stop, lim + side * risk, lim + side * 2 * risk,
                direction, "限价单", **cmn), timeline)
            variants[trend + "市价"].append(om)
            variants[trend + "挂单"].append(ol)
            rec = datetime.fromtimestamp(ts / 1000, TZ8).strftime("%H:%M")
            for nm, o in ((trend + "市价", om), (trend + "挂单", ol)):
                detail_wait.append(dict(time=rec, sym=meta.get("symbol"), variant=nm,
                                        direction=direction, entry=round(close, 4),
                                        reason=o.reason, net=round(o.net, 2)))
    rep_wait = {name: stats(name, outs) for name, outs in variants.items()}

    # 去重叠
    print()
    print("  --- 去重叠口径 (同一品种两笔间隔 >= 6h, 抑制同一波行情被重复计数) ---")
    keep_dedup = dedup(keep_wait, 6.0)
    variants2 = collections.OrderedDict((k, []) for k in ("顺势市价", "顺势挂单", "逆势市价", "逆势挂单"))
    for path, doc, meta, ts in keep_dedup:
        diag = doc.get("stage1_diagnosis") or {}
        dec = (doc["stage2_decision"] or {}).get("decision") or {}
        primary = str((diag.get("trend_context") or {}).get("primary_direction")
                      or diag.get("direction") or "")
        bars = bars_upto(doc)
        timeline = tl.get((meta["symbol"], meta.get("timeframe")), [])
        seq = [b for b in timeline if b[0] <= ts]
        a = atr(bars)
        close = seq[-1][4]
        for trend, direction in (("顺势", "做多" if primary == "bullish" else "做空"),
                                 ("逆势", "做空" if primary == "bullish" else "做多")):
            side = 1 if direction == "做多" else -1
            swing = min(b[3] for b in bars[-10:]) if side == 1 else max(b[2] for b in bars[-10:])
            stop = swing - 0.2 * a if side == 1 else swing + 0.2 * a
            risk = abs(close - stop)
            if risk <= 0:
                continue
            cmn = dict(bars=bars, cycle=diag.get("cycle_position"), conf=dec.get("trade_confidence"))
            variants2[trend + "市价"].append(run(make_plan(
                path, meta, ts, close, stop, close + side * risk, close + side * 2 * risk,
                direction, "突破单", **cmn), timeline))
            lim = close - side * 0.5 * a
            variants2[trend + "挂单"].append(run(make_plan(
                path, meta, ts, lim, stop, lim + side * risk, lim + side * 2 * risk,
                direction, "限价单", **cmn), timeline))
    rep_wait_dedup = {name: stats(name + "(去重叠)", outs) for name, outs in variants2.items()}
    report["wait"] = dict(full=rep_wait, dedup=rep_wait_dedup, n_dedup=len(keep_dedup),
                          detail=detail_wait)
    print("\n  --- 单笔贡献 TOP (口径: 顺势市价 / 逆势市价) ---")
    for nm in ("顺势市价", "逆势市价", "顺势挂单"):
        rows_n = sorted([d for d in detail_wait if d["variant"] == nm and d["net"]],
                        key=lambda x: -abs(x["net"]))[:6]
        tot = sum(d["net"] for d in detail_wait if d["variant"] == nm)
        print(f"    {nm} 合计{tot:+.2f}U; 最大单笔: " +
              ", ".join(f'{d["time"]}{d["sym"][:3]} {d["net"]:+.2f}' for d in rows_n))
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写 {args.json_out}")


if __name__ == "__main__":
    main()
