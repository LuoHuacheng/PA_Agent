"""对照流实验室: 滚动样本外参数寻优 + 切片 + 模型标签过滤交叉实验。

用法:
    uv run python tools/audit/baseline_lab.py [--days 45] [--segments 5]

复用 tools/shadow_replay 的引擎与数据管线。回答三个问题:
  1. 机械对照流的参数在全样本上"最优"后, 滚动样本外是否仍接近/为正
     (线1a; 不稳定 = 样本不足, 继续影子养数据);
  2. 最优组合在品种/时段/周期切片上是否一致 (线1b);
  3. 模型的环境/方向标签能否改善对照流 (线2; 决定模型在链路中的去留)。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, ".")

from tools.shadow_replay import (  # noqa: E402
    OUTCOMES_CSV,
    TZ8,
    Plan,
    _cutoff_from_days,
    fee_rate_from_outcomes,
    generate_baseline_plans,
    load_plans,
    merge_timelines,
    simulate_plan,
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
        _ts, o, h, low, c = timeline[i]
        prev_c = timeline[i - 1][4]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    for i in range(donchian_n + 1, len(timeline) - 5):
        ts = timeline[i][0]
        if cutoff and datetime.fromtimestamp(ts / 1000, TZ8).strftime("%Y-%m-%d") < cutoff:
            continue
        window = timeline[i - donchian_n:i]
        hi = max(b[2] for b in window)
        lo = min(b[3] for b in window)
        _ts, o, h, low, c = timeline[i]
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


RULES = {"ema_pullback": generate_baseline_plans, "donchian_break": gen_donchian}
PARAM_GRID = [
    (ema, rr, atr)
    for ema in (20, 30, 50)
    for rr in (1.5, 2.0, 3.0)
    for atr in (1.0, 1.5)
]


def run_rule(rule_name, gen, combo, tl, *, args, maker_fee, taker_fee, be_tr):
    ema, rr, atr_mult = combo
    plans = gen(tl, "", "", days=args.days, ema_period=ema,
                target_rr=rr, stop_atr_mult=atr_mult)
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
    args = ap.parse_args()

    fee_rate = args.fee_rate if args.fee_rate is not None else fee_rate_from_outcomes(OUTCOMES_CSV)
    maker_fee, taker_fee = fee_rate * 0.4, fee_rate * 0.8

    from tools.shadow_replay import PENDING_DIR

    plans = load_plans(PENDING_DIR, days=args.days)
    pending_dir = PENDING_DIR
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
    for rule_name, gen in RULES.items():
        # 预生成每组合全部成交行(带段号)
        combo_rows: dict[tuple, list[dict]] = {}
        for combo in PARAM_GRID:
            rows = []
            for tl in timelines.values():
                rows += run_rule(rule_name, gen, combo, tl, args=args,
                                 maker_fee=maker_fee, taker_fee=taker_fee, be_tr=0.5)
            for r in rows:
                r["seg"] = next(
                    (i for i in range(args.segments)
                     if r["record_ms"] >= seg_bounds[i] and r["record_ms"] < seg_bounds[i + 1]),
                    args.segments - 1)
            combo_rows[combo] = rows
        if not any(combo_rows.values()):
            print(f"  {rule_name}: 无成交")
            continue
        chosen: tuple | None = None
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
                print(f"  {rule_name:14s} 段{seg}: 选参 EMA{best[0]}/RR{best[1]}/ATR{best[2]} "
                      f"→ OOS {len(oos):3d}笔 R合计{rr_sum:+7.2f} 胜率{wr*100:3.0f}%")
        # 全样本最优参考(非样本外, 仅透明度)
        full_best = max(combo_rows.items(), key=lambda kv: sum(
            r["net"] / r["risk"] for r in kv[1]))
        fb_rows = full_best[1]
        if fb_rows:
            fb_r = sum(r["net"] / r["risk"] for r in fb_rows)
            fb_wr = sum(1 for r in fb_rows if r["net"] > 0) / len(fb_rows)
            print(f"  {rule_name:14s} 全样本最优 EMA{full_best[0][0]}/RR{full_best[0][1]}/ATR{full_best[0][2]}: "
                  f"{len(fb_rows)}笔 R合计{fb_r:+.2f} 均R{fb_r/len(fb_rows):+.3f} 胜率{fb_wr*100:.0f}%")
    if oos_rows:
        tot_r = sum(r["r_sum"] for r in oos_rows)
        tot_n = sum(r["n"] for r in oos_rows)
        print(f"  OOS 合计: {tot_n} 笔, R合计 {tot_r:+.2f}, 均R {tot_r/max(tot_n,1):+.3f} "
              f"→ {'接近正/正' if tot_r > 0 else '仍为负或不足'}")

    print("\n=== 线1b: 切片 (全样本最优组合, EMA20/RR2/ATR1 固定参数) ===")
    rows0 = []
    for tl in timelines.values():
        rows0 += run_rule("ema_pullback", generate_baseline_plans, (20, 2.0, 1.0),
                          tl, args=args, maker_fee=maker_fee, taker_fee=taker_fee, be_tr=0.5)
    if not rows0:
        print("  无成交")
        return
    by_sym = defaultdict(lambda: [0, 0.0])
    by_tf = defaultdict(lambda: [0, 0.0])
    by_sess = defaultdict(lambda: [0, 0.0])
    for r in rows0:
        by_sym[r["symbol"]][0] += 1; by_sym[r["symbol"]][1] += r["net"] / r["risk"]
        by_tf[r["timeframe"]][0] += 1; by_tf[r["timeframe"]][1] += r["net"] / r["risk"]
        hour = datetime.fromtimestamp(r["record_ms"] / 1000, TZ8).hour
        sess = "04-12时" if 4 <= hour < 12 else "其余"
        by_sess[sess][0] += 1; by_sess[sess][1] += r["net"] / r["risk"]
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
            r.update(best[1]); tagged += 1
    print(f"  可标注 {tagged}/{len(rows0)} 笔 (最近1小时决策)")
    def _show(name, pred):
        sub = [r for r in rows0 if pred(r)]
        if not sub:
            print(f"    {name:34s} 0"); return
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


if __name__ == "__main__":
    main()
