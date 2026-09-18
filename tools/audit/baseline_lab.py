"""对照流实验室: 滚动样本外参数寻优 + 切片 + 模型标签过滤交叉实验。

用法:
    uv run python tools/audit/baseline_lab.py [--days 45] [--segments 5]
        [--source records|binance] [--symbols BTCUSDT,...] [--kline-cache DIR]
        [--interval 15m] [--json-out FILE]

复用 tools/shadow_replay 的引擎与数据管线。回答三个问题:
  1. 机械对照流的参数在全样本上"最优"后, 滚动样本外是否仍接近/为正
     (线1a; 不稳定 = 样本不足, 继续影子养数据);
  2. 最优组合在品种/时段/周期切片上是否一致 (线1b);
  3. 模型的环境/方向标签能否改善对照流 (线2; 决定模型在链路中的去留)。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from tools.shadow_replay import (
    OUTCOMES_CSV,
    TZ8,
    Plan,
    _cutoff_from_days,
    fee_rate_from_outcomes,
    generate_baseline_plans,
    load_plans,
    maker_fee_rate,
    merge_timelines,
    simulate_plan,
    taker_fee_rate,
)
from tools.vergex_gens import (
    gen_dual_supertrend,
    gen_momo_breakout,
    gen_trend_pullback,
)


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def gen_donchian(timeline, symbol, timeframe, *, days, ema_period=20,
                 target_rr=2.0, stop_atr_mult=1.0, per_day=1, donchian_n=20):
    """第二规则: Donchian 突破(收在 N 根高点上方→市价突破单, ATR 止损, RR 目标)."""
    plans: list[Plan] = []
    if len(timeline) < donchian_n + 60:
        return plans
    cutoff = _cutoff_from_days(days) if days else None
    day_key = None
    day_count = 0
    cooldown_until = -1
    trs: list[float] = []
    for i in range(1, len(timeline)):
        _ts, _o, h, low, c = timeline[i]
        prev_c = timeline[i - 1][4]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    for i in range(donchian_n + 1, len(timeline) - 5):
        ts = timeline[i][0]
        if cutoff and datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d") < cutoff:
            continue
        window = timeline[i - donchian_n:i]
        hi = max(b[2] for b in window)
        lo = min(b[3] for b in window)
        _ts, _o, h, low, c = timeline[i]
        atr = sum(trs[max(0, i - 14):i]) / 14 if i >= 14 else 0.0
        if atr <= 0:
            continue
        side = 0
        if c > hi:
            side = 1
        elif c < lo:
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
            path="baseline-donchian", symbol=symbol, timeframe=timeframe,
            record_ms=ts, order_type="突破单",
            direction="做多" if side == 1 else "做空",
            entry=entry, stop=stop, target=target, target2=None,
            cycle_position="baseline", diag_direction="baseline",
            trade_conf=None, diag_conf=None, est_win_rate=None,
        ))
        day_count += 1
        cooldown_until = i + 8
    return plans


def build_timeline_from_klines(cache_dir, symbol, interval="15m") -> list:
    """从 klines.json 缓存读单品种 K 线, 转成 (ts,o,h,l,c) 5 元组时间线, ts 升序."""
    data = json.loads((Path(cache_dir) / "klines.json").read_text())
    tl = [[int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])]
          for row in data[symbol]]
    tl.sort(key=lambda row: row[0])
    return tl


RULES = {
    "ema_pullback": (generate_baseline_plans, [{"ema_period": e, "target_rr": rr,
                     "stop_atr_mult": a} for e in (20, 30, 50)
                     for rr in (1.5, 2.0, 3.0) for a in (1.0, 1.5)]),
    "donchian_break": (gen_donchian, [{"donchian_n": n, "target_rr": rr,
                       "stop_atr_mult": a} for n in (10, 20, 40)
                       for rr in (1.5, 2.0) for a in (1.0, 1.5)]),
    "momo_break": (gen_momo_breakout, [{"momo_n": n, "momo_atr": m, "target_rr": rr,
                   "stop_atr_mult": a} for n in (10, 14, 20) for m in (1.0, 1.5, 2.0)
                   for rr in (1.5, 2.0) for a in (1.0, 1.5)]),
    "dual_supertrend": (gen_dual_supertrend, [{"st_period": p, "st_mult": m,
                        "target_rr": rr} for p in (10, 20) for m in (2.0, 3.0, 4.0)
                        for rr in (1.5, 2.0, 3.0)]),
    "trend_pullback": (gen_trend_pullback, [{"ema_fast": f, "ema_slow": s,
                       "target_rr": rr} for f in (10, 20) for s in (50, 100, 200)
                       for rr in (1.5, 2.0, 3.0)]),
}


def run_rule(rule_name, gen, combo, tl, *, args, maker_fee, taker_fee, be_tr):
    plans = gen(tl, "", "", days=args.days, **combo)
    rows = []
    for p in plans:
        p.symbol, p.timeframe = tl_symbol_tf(tl)
        out = simulate_plan(
            p, tl, risk_usdt=args.risk_usdt, fee_rate=0.0,
            leverage=args.leverage, margin_usdt=args.margin_usdt,
            partial_pct=args.partial_pct, timeout_min=args.timeout_min,
            time_stop_min=args.time_stop_min,
            min_stop_pct=args.min_stop_pct, min_stop_atr=args.min_stop_atr,
            be_trigger=be_tr, entry_gtx=True,
            maker_fee=maker_fee, taker_fee=taker_fee,
        )
        if out.filled:
            rows.append({"plan": p, "net": out.net, "risk": args.risk_usdt,
                         "record_ms": p.record_ms, "symbol": p.symbol,
                         "timeframe": p.timeframe})
    return rows


_TF_CACHE: dict[int, tuple[str, str]] = {}


def tl_symbol_tf(tl) -> tuple[str, str]:
    return _TF_CACHE[id(tl)]


def _write_json_out(args, oos_rows, full_best_all) -> None:
    if not args.json_out:
        return
    payload = {
        "generated_at": datetime.now(TZ8).isoformat(),
        "source": args.source,
        "days": args.days,
        "segments": args.segments,
        "oos_rows": [
            {**r, "combo": dict(r["combo"])} if isinstance(r.get("combo"), tuple) else r
            for r in oos_rows
        ],
        "full_best": full_best_all,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"\nJSON 产物已写入: {args.json_out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--segments", type=int, default=5)
    ap.add_argument("--risk-usdt", type=float, default=4.0)
    ap.add_argument("--leverage", type=int, default=20)
    ap.add_argument("--margin-usdt", type=float, default=250.0)
    ap.add_argument("--partial-pct", type=float, default=50.0)
    ap.add_argument("--timeout-min", type=float, default=60.0)
    ap.add_argument("--time-stop-min", type=float, default=720.0)
    ap.add_argument("--min-stop-pct", type=float, default=0.2)
    ap.add_argument("--min-stop-atr", type=float, default=0.0)
    ap.add_argument("--fee-rate", type=float, default=None)
    ap.add_argument("--source", choices=("records", "binance"), default="records",
                    help="时间线来源: records=决策记录合并, binance=Binance K 线缓存")
    ap.add_argument("--symbols", type=str, default="",
                    help="binance 源品种, 逗号分隔, 如 BTCUSDT,ETHUSDT,ZECUSDT")
    ap.add_argument("--kline-cache", type=Path, default=Path("research/vergex/data/_klines"))
    ap.add_argument("--interval", type=str, default="15m")
    ap.add_argument("--json-out", type=Path, default=None,
                    help="运行结束写入 JSON 产物路径")
    args = ap.parse_args()

    fee_rate = args.fee_rate if args.fee_rate is not None else fee_rate_from_outcomes(OUTCOMES_CSV)
    maker_fee, taker_fee = maker_fee_rate(fee_rate), taker_fee_rate(fee_rate)

    from tools.shadow_replay import PENDING_DIR

    pending_dir = PENDING_DIR
    if args.source == "binance":
        if args.interval not in {"5m", "15m", "30m", "1h"}:
            print(f"[错误] --interval 仅支持 5m/15m/30m/1h, 收到: {args.interval}")
            sys.exit(1)
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if not symbols:
            print("[错误] --source binance 需通过 --symbols 指定品种, "
                  "如 BTCUSDT,ETHUSDT,ZECUSDT")
            sys.exit(1)
        if not (args.kline_cache / "klines.json").exists():
            # 缓存缺失才惰性拉取; 已有缓存直接复用, 勿覆盖重拉
            from tools._pa_sim_common import window_bounds
            from tools.sim_trailing_stop import fetch_klines

            args.kline_cache.mkdir(parents=True, exist_ok=True)
            start_ms, end_ms = window_bounds(args.days)
            print(f"klines.json 缺失, 拉取 {symbols} {args.interval} "
                  f"({datetime.fromtimestamp(start_ms / 1000):%m-%d} ~ "
                  f"{datetime.fromtimestamp(end_ms / 1000):%m-%d}) ...")
            fetch_klines(symbols, start_ms, end_ms, args.interval, args.kline_cache)
        timelines = {}
        for sym in symbols:
            try:
                tl = build_timeline_from_klines(args.kline_cache, sym, args.interval)
            except KeyError:
                print(f"[错误] klines.json 缓存中无品种 {sym} 数据, "
                      f"缓存目录: {args.kline_cache}")
                sys.exit(1)
            if not tl:
                print(f"[错误] 品种 {sym} 的时间线为空, 无法运行 OOS; "
                      f"请检查 klines.json 覆盖区间或重新提供缓存")
                sys.exit(1)
            timelines[sym] = tl
            _TF_CACHE[id(tl)] = (sym, args.interval)
    else:
        plans = load_plans(PENDING_DIR, days=args.days)
        timelines = merge_timelines(plans)
        # 给每个时间线挂 symbol/tf
        for key, tl in timelines.items():
            _TF_CACHE[id(tl)] = key
    print(f"时间线: {len(timelines)} 组, fee maker={maker_fee:.4f} taker={taker_fee:.4f}")

    seg_bounds = None
    all_ts = [bar[0] for tl in timelines.values() for bar in tl]
    if all_ts:
        t0, t1 = min(all_ts), max(all_ts)
        step = (t1 - t0) / args.segments
        seg_bounds = [t0 + step * i for i in range(args.segments + 1)]
        print(f"窗口: {datetime.fromtimestamp(t0/1000):%m-%d} ~ "
              f"{datetime.fromtimestamp(t1/1000):%m-%d}, {args.segments} 段, "
              f"每段 {step/86400000:.1f} 天")

    print("\n=== 线1a: 滚动样本外 (前段选参 → 下段验证) ===")
    oos_rows: list[dict] = []
    full_best_all: dict[str, dict] = {}
    for rule_name, (gen, grid) in RULES.items():
        # 预生成每组合全部成交行(带段号); combo 为 dict, 键转可哈希元组
        combo_rows: dict[tuple, list[dict]] = {}
        for combo in grid:
            rows = []
            for tl in timelines.values():
                rows += run_rule(rule_name, gen, combo, tl, args=args,
                                 maker_fee=maker_fee, taker_fee=taker_fee, be_tr=0.5)
            for r in rows:
                r["seg"] = next(
                    (i for i in range(args.segments)
                     if r["record_ms"] >= seg_bounds[i] and r["record_ms"] < seg_bounds[i + 1]),
                    args.segments - 1)
            combo_rows[tuple(sorted(combo.items()))] = rows
        if not any(combo_rows.values()):
            print(f"  {rule_name}: 无成交")
            continue
        for seg in range(1, args.segments):
            # 前段(0..seg-1)选参
            best, best_v = None, -1e18
            for combo, rows in combo_rows.items():
                v = sum(r["net"] / r["risk"] for r in rows if r["seg"] < seg)
                if best is None or v > best_v:
                    best, best_v = combo, v
            # 本段验证
            oos = [r for r in combo_rows[best] if r["seg"] == seg]
            if oos:
                rr_sum = sum(r["net"] / r["risk"] for r in oos)
                wr = sum(1 for r in oos if r["net"] > 0) / len(oos)
                oos_rows.append({"rule": rule_name, "seg": seg, "combo": best,
                                 "n": len(oos), "r_sum": rr_sum, "wr": wr})
                print(f"  {rule_name:14s} 段{seg}: 选参 "
                      f"{json.dumps(dict(best), sort_keys=True, ensure_ascii=False)} "
                      f"→ OOS {len(oos):3d}笔 R合计{rr_sum:+7.2f} 胜率{wr*100:3.0f}%")
        # 全样本最优参考(非样本外, 仅透明度)
        full_best = max(combo_rows.items(), key=lambda kv: sum(
            r["net"] / r["risk"] for r in kv[1]))
        fb_rows = full_best[1]
        if fb_rows:
            fb_r = sum(r["net"] / r["risk"] for r in fb_rows)
            fb_wr = sum(1 for r in fb_rows if r["net"] > 0) / len(fb_rows)
            full_best_all[rule_name] = {"combo": dict(full_best[0]), "n": len(fb_rows),
                                        "wr": fb_wr, "r_sum": fb_r,
                                        "avg_r": fb_r / len(fb_rows)}
            print(f"  {rule_name:14s} 全样本最优 "
                  f"{json.dumps(dict(full_best[0]), sort_keys=True, ensure_ascii=False)}: "
                  f"{len(fb_rows)}笔 R合计{fb_r:+.2f} 均R{fb_r/len(fb_rows):+.3f} 胜率{fb_wr*100:.0f}%")
    if oos_rows:
        tot_r = sum(r["r_sum"] for r in oos_rows)
        tot_n = sum(r["n"] for r in oos_rows)
        print(f"  OOS 合计: {tot_n} 笔, R合计 {tot_r:+.2f}, 均R {tot_r/max(tot_n,1):+.3f} "
              f"→ {'接近正/正' if tot_r > 0 else '仍为负或不足'}")

    print("\n=== 线1b: 切片 (全样本最优组合, EMA20/RR2/ATR1 固定参数) ===")
    rows0 = []
    for tl in timelines.values():
        rows0 += run_rule("ema_pullback", generate_baseline_plans,
                          {"ema_period": 20, "target_rr": 2.0, "stop_atr_mult": 1.0},
                          tl, args=args, maker_fee=maker_fee, taker_fee=taker_fee, be_tr=0.5)
    if not rows0:
        print("  无成交")
        _write_json_out(args, oos_rows, full_best_all)
        return
    by_sym = defaultdict(lambda: [0, 0.0])
    by_tf = defaultdict(lambda: [0, 0.0])
    by_sess = defaultdict(lambda: [0, 0.0])
    for r in rows0:
        by_sym[r["symbol"]][0] += 1
        by_sym[r["symbol"]][1] += r["net"] / r["risk"]
        by_tf[r["timeframe"]][0] += 1
        by_tf[r["timeframe"]][1] += r["net"] / r["risk"]
        hour = datetime.fromtimestamp(r["record_ms"] / 1000, TZ8).hour
        sess = "04-12时" if 4 <= hour < 12 else "其余"
        by_sess[sess][0] += 1
        by_sess[sess][1] += r["net"] / r["risk"]
    for title, by in (("品种", by_sym), ("周期", by_tf), ("时段", by_sess)):
        print(f"  -- {title}")
        for k, (n, rs) in sorted(by.items(), key=lambda kv: -kv[1][0]):
            print(f"    {k:10s} {n:3d}笔 均R{rs/n:+.3f} R合计{rs:+.2f}")

    print("\n=== 线2: 模型标签能否过滤对照流 (固定参数 EMA20/RR2/ATR1) ===")
    # 模型标签: 最近 1 小时内的决策记录
    qidx: dict[tuple, list] = defaultdict(list)
    for fn in sorted(pending_dir.glob("*.json")):
        try:
            import json as _json
            doc = _json.loads(fn.read_text())
        except Exception:
            continue
        meta = doc.get("meta") or {}
        sym = str(meta.get("symbol") or "")
        tf = str(meta.get("timeframe") or "")
        ts = meta.get("timestamp_local_ms")
        if not sym or not tf or not ts:
            continue
        diag = doc.get("stage1_diagnosis") or {}
        s2 = doc.get("stage2_decision") or {}
        qidx[(sym, tf)].append((ts, {
            "diag": str(diag.get("direction") or ""),
            "cycle": str(diag.get("cycle_position") or ""),
            "always_in": str((s2.get("bar_analysis") or {}).get("always_in") or ""),
        }))
    for v in qidx.values():
        v.sort()
    tagged = 0
    for r in rows0:
        cands = qidx.get((r["symbol"], r["timeframe"]), [])
        best = min(cands, key=lambda x: abs(x[0] - r["record_ms"])) if cands else None
        if best and abs(best[0] - r["record_ms"]) < 3600_000:
            r.update(best[1])
            tagged += 1
    print(f"  可标注 {tagged}/{len(rows0)} 笔 (最近1小时决策)")
    def _show(name, pred):
        sub = [r for r in rows0 if pred(r)]
        if not sub:
            print(f"    {name:34s} 0")
            return
        rs = sum(r["net"] / r["risk"] for r in sub)
        wr = sum(1 for r in sub if r["net"] > 0) / len(sub)
        print(f"    {name:34s} {len(sub):3d}笔 均R{rs/len(sub):+.3f} 胜率{wr*100:3.0f}%")
    _show("不过滤(基线)", lambda r: True)
    _show("模型 diag=bullish 才留", lambda r: r.get("diag") == "bullish")
    _show("模型 diag∈{bullish}且cycle≠trending_tr",
          lambda r: r.get("diag") == "bullish" and r.get("cycle") != "trending_tr")
    _show("模型 always_in=long 才留", lambda r: r.get("always_in") == "long")
    _show("模型 cycle∈{normal,broad,range}", lambda r: r.get("cycle") in ("normal_channel", "broad_channel", "trading_range"))
    print("  判读: 过滤后均R显著高于基线 → 模型作过滤器有正贡献; 反之模型退出链路")
    _write_json_out(args, oos_rows, full_best_all)


if __name__ == "__main__":
    main()
