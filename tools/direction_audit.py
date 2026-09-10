#!/usr/bin/env python3
"""决策级方向评估: 用每条诊断记录之后的真实走势, 检验周期位置的方向判断力.

影子回放(tools/shadow_replay.py)受限于"挂单是否成交", 样本被砍掉七成. 本工具
换一个角度: 不关心是否成交, 只看阶段一给的方向(bullish/bearish/neutral)在之后
N 根 K 线上有没有兑现. 样本量因此扩大约 40 倍, 代价是测不到执行层.

关键设计: 必须给出基线. 若样本期价格本来就在涨, bullish 命中率高不代表能力.
基准取"同一批样本里后续上涨的占比", 方向命中率与之相减, 得到超额.

用法规约:
    python tools/direction_audit.py --days 45
    python tools/direction_audit.py --days 45 --horizons 1,2,4,8,16 --min-conf 50
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TZ8 = timezone(timedelta(hours=8))
PENDING_DIR = ROOT / "records" / "pending"
#: kline_data 的 ts_open 是本地墙上时间当成 epoch 存下来的.
KLINE_EPOCH_OFFSET_MS = 8 * 3600 * 1000
BAR_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
          "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


#: 主网公共日线: 与 testnet 价格一致, 但不占用 testnet 共享 IP 的配额.
DAILY_URL = "https://fapi.binance.com/fapi/v1/klines"


def _cutoff(days):
    return (datetime.now(TZ8) - timedelta(days=days)).strftime("%Y-%m-%d") if days else None


def fetch_daily_closes(symbol, limit=90):
    """[(open_time_ms, close)] 真实 epoch, 主网公共接口."""
    import json as _json
    import urllib.request
    url = "%s?symbol=%s&interval=1d&limit=%d" % (DAILY_URL, symbol, limit)
    req = urllib.request.Request(url, headers={"User-Agent": "PA_Agent/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        rows = _json.loads(resp.read())
    return [(int(row[0]), float(row[4])) for row in rows]


def trend_pct_at(closes, ms, days):
    """决策时刻往回 days 根日线的涨跌幅(%); 数据不足返回 None."""
    past = [close for ts, close in closes if ts <= ms]
    if len(past) < 2:
        return None
    window = past[-days:] if len(past) >= days else past
    if len(window) < 2 or window[0] <= 0:
        return None
    return (window[-1] - window[0]) / window[0] * 100


def load_records(days=None):
    """返回 [(symbol, timeframe, record_ms, diagnosis, bars)]."""
    cutoff = _cutoff(days)
    out = []
    for path in sorted(glob.glob(str(PENDING_DIR / "*.json"))):
        if cutoff and os.path.basename(path)[:10] < cutoff:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        meta = doc.get("meta") or {}
        record_ms = meta.get("timestamp_local_ms")
        diag = doc.get("stage1_diagnosis")
        if not record_ms or not isinstance(diag, dict):
            continue
        bars = []
        for bar in doc.get("kline_data") or []:
            ts = bar.get("ts_open")
            if not ts:
                continue
            try:
                bars.append((int(ts) - KLINE_EPOCH_OFFSET_MS,
                             float(bar["open"]), float(bar["high"]),
                             float(bar["low"]), float(bar["close"])))
            except (KeyError, TypeError, ValueError):
                continue
        bars.sort()
        if bars:
            out.append((str(meta.get("symbol") or ""), str(meta.get("timeframe") or ""),
                        int(record_ms), diag, bars))
    return out


def merge_timelines(records):
    merged = collections.defaultdict(dict)
    for symbol, timeframe, _ms, _diag, bars in records:
        store = merged[(symbol, timeframe)]
        for bar in bars:
            store[bar[0]] = bar
    return {key: sorted(store.values()) for key, store in merged.items()}


def window_return(timeline, record_ms, horizon, bar_ms):
    """决策之后连续 horizon 根 K 线的收盘变化率; 数据不连续或不足时返回 None."""
    past = [b for b in timeline if b[0] <= record_ms]
    if not past:
        return None
    anchor = past[-1][4]
    if anchor <= 0:
        return None
    future = [b for b in timeline if b[0] > record_ms]
    if len(future) < horizon:
        return None
    for i in range(horizon - 1):
        if future[i + 1][0] - future[i][0] != bar_ms:
            return None
    return (future[horizon - 1][4] - anchor) / anchor


def audit(args):
    records = load_records(args.days)
    print("加载诊断记录:", len(records))
    if not records:
        return 0
    timelines = merge_timelines(records)
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    # rows: (cycle, direction, conf, {horizon: ret})
    rows = []
    for symbol, timeframe, ms, diag, _bars in records:
        conf = diag.get("diagnosis_confidence")
        if args.min_conf is not None:
            try:
                if conf is None or float(conf) < args.min_conf:
                    continue
            except (TypeError, ValueError):
                continue
        direction = str(diag.get("direction") or "").lower()
        if direction not in ("bullish", "bearish"):
            continue
        bar_ms = BAR_MS.get(timeframe)
        if not bar_ms:
            continue
        rets = {}
        for h in horizons:
            ret = window_return(timelines.get((symbol, timeframe), []), ms, h, bar_ms)
            if ret is not None:
                rets[h] = ret
        if rets:
            rows.append((str(diag.get("cycle_position") or "unknown"), direction, conf,
                         rets, symbol, ms))

    print("有方向且数据连续的记录:", len(rows))
    if not rows:
        return 0

    # ---- 30 天趋势对齐: 验证模型是否系统性逆势 ----
    if not args.no_trend:
        symbols = sorted({r[4] for r in rows})
        daily = {}
        for sym in symbols:
            try:
                daily[sym] = fetch_daily_closes(sym)
            except Exception as exc:
                print("  日线拉取失败 %s: %s" % (sym, exc))
        tagged = []
        for row in rows:
            closes = daily.get(row[4])
            if not closes:
                continue
            pct = trend_pct_at(closes, row[5], args.trend_days)
            if pct is None:
                continue
            trend = "bull" if pct > args.trend_neutral else ("bear" if pct < -args.trend_neutral else "neutral")
            if trend == "neutral":
                align = "neutral"
            else:
                align = "with" if (row[1] == "bullish") == (trend == "bull") else "against"
            tagged.append((row, pct, align))
        if tagged:
            h = horizons[-1]
            print()
            print("=== %d 天趋势对齐 (horizon=%d, 中性带 %.1f%%) ===" % (
                args.trend_days, h, args.trend_neutral))
            groups = collections.defaultdict(list)
            for row, _pct, align in tagged:
                if h in row[3]:
                    groups[align].append(row)
            print("%-10s %6s %8s %8s %10s %10s" % ("对齐", "n", "占比", "命中率", "基线", "超额"))
            total = sum(len(v) for v in groups.values())
            for name, label in (("with", "顺势"), ("against", "逆势"), ("neutral", "中性")):
                bucket = groups.get(name, [])
                if not bucket:
                    continue
                up = sum(1 for r in bucket if r[3][h] > 0) / len(bucket)
                hit = sum(1 for r in bucket if (r[3][h] > 0) == (r[1] == "bullish")) / len(bucket)
                print("%-10s %6d %7.0f%% %7.1f%% %7.1f%% %+9.1f" % (
                    label, len(bucket), len(bucket) / total * 100 if total else 0,
                    hit * 100, max(up, 1 - up) * 100,
                    (hit - max(up, 1 - up)) * 100))
            pcts = [p for _r, p, _a in tagged]
            pcts.sort()
            print("  决策时刻 %d 天趋势分布: p25 %+.1f%%  中位 %+.1f%%  p75 %+.1f%%" % (
                args.trend_days, pcts[len(pcts) // 4], pcts[len(pcts) // 2],
                pcts[len(pcts) * 3 // 4]))

    for h in horizons:
        usable = [r for r in rows if h in r[3]]
        if not usable:
            continue
        up_rate = sum(1 for r in usable if r[3][h] > 0) / len(usable)
        hits = sum(1 for r in usable if (r[3][h] > 0) == (r[1] == "bullish"))
        print()
        print("=== 未来 %d 根 K 线 ===" % h)
        print("样本 %d   方向命中 %.1f%%   基线(多数类) %.1f%%   超额 %+.1f 个百分点" % (
            len(usable), hits / len(usable) * 100, max(up_rate, 1 - up_rate) * 100,
            (hits / len(usable) - max(up_rate, 1 - up_rate)) * 100))
        bulls = [r for r in usable if r[1] == "bullish"]
        bears = [r for r in usable if r[1] == "bearish"]
        b_hit = sum(1 for r in bulls if r[3][h] > 0) / len(bulls) * 100 if bulls else 0
        s_hit = sum(1 for r in bears if r[3][h] < 0) / len(bears) * 100 if bears else 0
        print("  bull 预测 %4d 笔(%.0f%%)  其中真涨 %.1f%%   市场涨占比 %.1f%%   超额 %+.1f" % (
            len(bulls), len(bulls) / len(usable) * 100, b_hit, up_rate * 100, b_hit - up_rate * 100))
        print("  bear 预测 %4d 笔(%.0f%%)  其中真跌 %.1f%%   市场跌占比 %.1f%%   超额 %+.1f" % (
            len(bears), len(bears) / len(usable) * 100, s_hit, (1 - up_rate) * 100,
            s_hit - (1 - up_rate) * 100))
        groups = collections.defaultdict(list)
        for r in usable:
            groups[r[0]].append(r)
        print("%-16s %6s %8s %8s %10s" % ("cycle_position", "n", "命中率", "基线", "超额"))
        for name in sorted(groups, key=lambda k: -len(groups[k])):
            bucket = groups[name]
            if len(bucket) < args.min_samples:
                continue
            up = sum(1 for r in bucket if r[3][h] > 0) / len(bucket)
            hit = sum(1 for r in bucket if (r[3][h] > 0) == (r[1] == "bullish")) / len(bucket)
            print("%-16s %6d %7.1f%% %7.1f%% %+9.1f" % (
                name, len(bucket), hit * 100, up * 100, (hit - max(up, 1 - up)) * 100))

    print()
    print("=== 按诊断置信度分桶 (horizon=%d) ===" % horizons[-1])
    h = horizons[-1]
    usable = [r for r in rows if h in r[3]]
    buckets = collections.defaultdict(list)
    for r in usable:
        try:
            c = float(r[2])
        except (TypeError, ValueError):
            continue
        buckets[int(c // 5) * 5].append(r)
    print("%-10s %6s %8s %10s" % ("conf 桶", "n", "命中率", "平均涨跌"))
    for lo in sorted(buckets):
        bucket = buckets[lo]
        if len(bucket) < args.min_samples:
            continue
        hit = sum(1 for r in bucket if (r[3][h] > 0) == (r[1] == "bullish")) / len(bucket)
        mean = sum(r[3][h] for r in bucket) / len(bucket) * 100
        print("%-10s %6d %7.1f%% %+9.2f%%" % ("%d-%d" % (lo, lo + 4), len(bucket), hit * 100, mean))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--horizons", default="1,2,4,8")
    ap.add_argument("--min-conf", type=float, default=None)
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--no-trend", action="store_true", help="跳过 30 天趋势对齐分析")
    ap.add_argument("--trend-days", type=int, default=30)
    ap.add_argument("--trend-neutral", type=float, default=3.0,
                    help="|涨跌幅| 不超过该值视为无趋势(%%)")
    args = ap.parse_args()
    return audit(args)


if __name__ == "__main__":
    raise SystemExit(main())
