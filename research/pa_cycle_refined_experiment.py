#!/usr/bin/env python3
"""Refined PA nesting / cycle experiments — fix under-detection of spike & micro-pushes."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pa_cycle_nesting_experiment import (
    DATA_DIR,
    DATASETS,
    MAX_BARS,
    atr,
    detect_ranges,
    ema,
    evaluate_tr_breakout_mm,
    find_swings,
    legs_from_swings,
    load_ohlc,
)

OUT_DIR = Path(__file__).resolve().parent / "out"


def local_extrema(h: np.ndarray, l: np.ndarray, left: int = 1, right: int = 1) -> list[tuple[int, str, float]]:
    n = len(h)
    out = []
    for i in range(left, n - right):
        if h[i] >= h[i - left : i + right + 1].max():
            out.append((i, "H", float(h[i])))
        if l[i] <= l[i - left : i + right + 1].min():
            out.append((i, "L", float(l[i])))
    out.sort(key=lambda x: x[0])
    # alternate
    filt = []
    for s in out:
        if not filt:
            filt.append(s)
            continue
        if s[1] == filt[-1][1]:
            if s[1] == "H" and s[2] >= filt[-1][2]:
                filt[-1] = s
            elif s[1] == "L" and s[2] <= filt[-1][2]:
                filt[-1] = s
        else:
            filt.append(s)
    return filt


def count_pushes_relaxed(df: pd.DataFrame, leg: dict[str, Any], min_frac: float = 0.12) -> int:
    """Count same-direction micro swings >= min_frac of leg range."""
    i0, i1 = leg["i0"], leg["i1"]
    if i1 - i0 < 5:
        return 1
    h = df["high"].to_numpy(float)[i0 : i1 + 1]
    l = df["low"].to_numpy(float)[i0 : i1 + 1]
    ex = local_extrema(h, l, 1, 1)
    if len(ex) < 2:
        return 1
    leg_range = max(leg["range"], 1e-9)
    pushes = 0
    for a, b in zip(ex, ex[1:]):
        d = "up" if b[2] > a[2] else "down"
        if d == leg["dir"] and abs(b[2] - a[2]) >= min_frac * leg_range:
            pushes += 1
    return max(1, pushes)


def classify_regime_v2(df: pd.DataFrame, atr_arr: np.ndarray, end: int, win: int = 40) -> str:
    start = max(0, end - win + 1)
    if end - start < 12:
        return "unknown"
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    atr_m = float(np.nanmean(atr_arr[start : end + 1]))
    if atr_m <= 0:
        return "unknown"

    body = np.abs(c[start : end + 1] - o[start : end + 1])
    rng = h[start : end + 1] - l[start : end + 1]
    overlaps = []
    for i in range(start + 1, end + 1):
        ov = max(0.0, min(h[i], h[i - 1]) - max(l[i], l[i - 1]))
        w = max(h[i], h[i - 1]) - min(l[i], l[i - 1])
        overlaps.append(ov / w if w > 0 else 1.0)
    ov_mean = float(np.mean(overlaps))
    net = abs(c[end] - c[start])
    path = float(np.sum(rng)) 
    efficiency = net / path if path > 0 else 0.0

    # trend-bar run
    dirs = np.sign(c[start : end + 1] - o[start : end + 1])
    max_run = 1
    run = 1
    for i in range(1, len(dirs)):
        strong = body[i] > 0.45 * atr_m
        if dirs[i] != 0 and dirs[i] == dirs[i - 1] and strong:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1

    # pullback via extrema
    ex = local_extrema(h[start : end + 1], l[start : end + 1], 1, 1)
    pullback = 0.0
    hh_hl = 0
    ll_lh = 0
    if len(ex) >= 4:
        # count HH+HL / LL+LH pairs on alternating swings
        for i in range(2, len(ex)):
            if ex[i][1] == "H" and ex[i - 2][1] == "H":
                if ex[i][2] > ex[i - 2][2]:
                    # look for HL between
                    if ex[i - 1][1] == "L" and i >= 3 and ex[i - 3][1] == "L" and ex[i - 1][2] > ex[i - 3][2]:
                        hh_hl += 1
            if ex[i][1] == "L" and ex[i - 2][1] == "L":
                if ex[i][2] < ex[i - 2][2]:
                    if ex[i - 1][1] == "H" and i >= 3 and ex[i - 3][1] == "H" and ex[i - 1][2] < ex[i - 3][2]:
                        ll_lh += 1
        for a, b, d in zip(ex[:-2], ex[1:-1], ex[2:]):
            impulse = abs(b[2] - a[2])
            pb = abs(d[2] - b[2])
            if impulse > 0:
                pullback = max(pullback, pb / impulse)

    # recent 8-bar urgency
    r8 = slice(max(start, end - 7), end + 1)
    urg_eff = abs(c[end] - c[max(start, end - 7)]) / max(float(np.sum(rng[r8])), 1e-9)

    if max_run >= 3 and (efficiency > 0.28 or urg_eff > 0.45) and ov_mean < 0.48:
        return "spike"
    if max_run >= 2 and urg_eff > 0.40 and ov_mean < 0.42:
        return "spike"
    if efficiency > 0.20 and ov_mean < 0.50 and pullback < 0.35 and (hh_hl >= 1 or ll_lh >= 1):
        return "tight_channel"
    if efficiency > 0.14 and pullback < 0.55 and (hh_hl >= 1 or ll_lh >= 1) and ov_mean < 0.58:
        return "normal_channel"
    if (hh_hl >= 2 or ll_lh >= 2) and pullback >= 0.45:
        return "broad_channel"
    if (hh_hl >= 1 or ll_lh >= 1) and pullback >= 0.35:
        return "broad_channel"
    width_atr = float(h[start : end + 1].max() - l[start : end + 1].min()) / atr_m
    if ov_mean > 0.62 and width_atr < 3.2:
        return "extreme_tr"
    return "trading_range"


def regime_series_v2(df, atr_arr, step=8, win=40):
    return [(i, classify_regime_v2(df, atr_arr, i, win)) for i in range(win, len(df), step)]


def markov(series):
    trans = defaultdict(Counter)
    for (_, a), (_, b) in zip(series, series[1:]):
        trans[a][b] += 1
    probs = {a: {b: c / max(1, sum(ctr.values())) for b, c in ctr.items()} for a, ctr in trans.items()}
    labels = [s for _, s in series]
    n = max(1, len(series) - 1)
    persist = sum(1 for (_, a), (_, b) in zip(series, series[1:]) if a == b) / n
    maj = Counter(labels).most_common(1)[0][0]
    maj_acc = sum(1 for s in labels[1:] if s == maj) / n
    correct = 0
    for (_, a), (_, b) in zip(series, series[1:]):
        pred = max(trans[a], key=trans[a].get) if trans[a] else a
        if pred == b:
            correct += 1
    return {
        "transition_probs": probs,
        "persistence_accuracy": persist,
        "majority_null_accuracy": maj_acc,
        "markov_accuracy": correct / n,
        "regime_counts": dict(Counter(labels)),
        "n_steps": n,
    }


def spike_aftermath_v2(df, atr_arr):
    n = len(df)
    c = df["close"].to_numpy(float)
    outcomes = Counter()
    samples = 0
    i = 40
    while i < n - 40:
        if classify_regime_v2(df, atr_arr, i, 40) != "spike":
            i += 6
            continue
        # ensure previous wasn't already spike (onset)
        if classify_regime_v2(df, atr_arr, i - 8, 40) == "spike":
            i += 6
            continue
        fut_i = min(n - 1, i + 32)
        fut = classify_regime_v2(df, atr_arr, fut_i, 32)
        if fut in ("tight_channel", "normal_channel", "broad_channel"):
            bucket = "channel"
        elif fut in ("trading_range", "extreme_tr"):
            bucket = "trading_range"
        elif fut == "spike":
            bucket = "channel"  # continued trend pressure ~ channel/spike family
        else:
            bucket = "other"
        pre = c[max(0, i - 12)]
        spike_move = c[i] - pre
        fut_move = c[fut_i] - c[i]
        if spike_move != 0 and fut_move * spike_move < 0 and abs(fut_move) > 0.55 * abs(spike_move):
            bucket = "reversal"
        outcomes[bucket] += 1
        samples += 1
        i += 28
    total = sum(outcomes.values()) or 1
    return {"n_spikes": samples, "distribution": {k: v / total for k, v in outcomes.items()}, "counts": dict(outcomes)}


def directional_edge_tests(df, atr_arr) -> dict[str, Any]:
    """Can structure features beat random for next-horizon direction?"""
    c = df["close"].to_numpy(float)
    e = ema(c, 20)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    results = {}
    for horizon in (5, 10, 20):
        # strategies
        scores = {
            "coin_flip_sim": [],  # placeholder filled later
            "ema_slope": [],
            "always_in_8": [],  # last 8 closes net
            "regime_channel_with_trend": [],
            "range_fade_mid": [],  # fade toward mid — should be ~noise or slight positive in TR
            "breakout_follow": [],
        }
        for i in range(60, len(df) - horizon, 5):
            fut = np.sign(c[i + horizon] - c[i])
            if fut == 0:
                continue
            # ema
            pred = np.sign(e[i] - e[i - 10])
            scores["ema_slope"].append(1 if pred == fut else 0)
            # always in
            pred = np.sign(c[i] - c[i - 8])
            scores["always_in_8"].append(1 if pred != 0 and pred == fut else 0)
            # channel with trend
            reg = classify_regime_v2(df, atr_arr, i, 40)
            if reg in ("spike", "tight_channel", "normal_channel", "broad_channel"):
                pred = np.sign(e[i] - e[i - 15])
                scores["regime_channel_with_trend"].append(1 if pred == fut else 0)
            # range fade: if TR and near extreme, fade
            if reg in ("trading_range", "extreme_tr"):
                rh, rl = h[i - 39 : i + 1].max(), l[i - 39 : i + 1].min()
                pos = (c[i] - rl) / max(rh - rl, 1e-9)
                if pos > 0.75:
                    pred = -1
                    scores["range_fade_mid"].append(1 if pred == fut else 0)
                elif pos < 0.25:
                    pred = 1
                    scores["range_fade_mid"].append(1 if pred == fut else 0)
            # breakout follow: close beyond 20-bar high/low
            if c[i] > h[i - 20 : i].max():
                scores["breakout_follow"].append(1 if fut == 1 else 0)
            elif c[i] < l[i - 20 : i].min():
                scores["breakout_follow"].append(1 if fut == -1 else 0)

        out = {}
        for k, vals in scores.items():
            if k == "coin_flip_sim":
                continue
            if not vals:
                out[k] = None
            else:
                out[k] = {"n": len(vals), "hit_rate": float(np.mean(vals)), "edge_vs_50": float(np.mean(vals) - 0.5)}
        results[f"h{horizon}"] = out
    return results


def nested_structure_scan(df, atr_arr) -> dict[str, Any]:
    swings = find_swings(df, atr_arr, atr_mult=1.0)
    legs = legs_from_swings(swings)
    push_hist = Counter()
    for leg in legs:
        push_hist[count_pushes_relaxed(df, leg, 0.12)] += 1
    ranges = detect_ranges(legs, atr_arr, min_legs=2)
    # User narrative pattern: up-leg(3+ push) + down-leg(3+ push) forming range, then breakout to MM
    pattern_hits = 0
    pattern_mm = 0
    pattern_fail = 0
    examples = []
    for r in ranges:
        sub = legs[r["leg_i0"] : r["leg_i1"] + 1]
        ups = [lg for lg in sub if lg["dir"] == "up"]
        dns = [lg for lg in sub if lg["dir"] == "down"]
        if not ups or not dns:
            continue
        up_p = max(count_pushes_relaxed(df, lg) for lg in ups)
        dn_p = max(count_pushes_relaxed(df, lg) for lg in dns)
        if up_p >= 3 and dn_p >= 3 and r["n_legs"] >= 2:
            pattern_hits += 1
            # evaluate breakout MM using single-range call
            mm = evaluate_tr_breakout_mm(df, [r], atr_arr)
            counts = mm["counts"]
            if counts.get("mm_hit", 0):
                pattern_mm += 1
                tag = "mm_hit"
            elif counts.get("failed_breakout", 0):
                pattern_fail += 1
                tag = "failed_breakout"
            else:
                tag = list(counts.keys())[0] if counts else "other"
            if len(examples) < 8:
                examples.append(
                    {
                        "t0": str(df["dt"].iloc[r["bar0"]]),
                        "t1": str(df["dt"].iloc[r["bar1"]]),
                        "up_pushes": up_p,
                        "down_pushes": dn_p,
                        "n_legs": r["n_legs"],
                        "high": r["high"],
                        "low": r["low"],
                        "height": r["height"],
                        "outcome": tag,
                    }
                )
    total_legs = sum(push_hist.values()) or 1
    return {
        "push_hist": {str(k): v for k, v in sorted(push_hist.items())},
        "pct_legs_3plus": sum(v for k, v in push_hist.items() if k >= 3) / total_legs,
        "pct_legs_3or4": sum(v for k, v in push_hist.items() if k in (3, 4)) / total_legs,
        "n_ranges": len(ranges),
        "user_pattern_count": pattern_hits,
        "user_pattern_mm_hit": pattern_mm,
        "user_pattern_fail": pattern_fail,
        "user_pattern_mm_rate": pattern_mm / pattern_hits if pattern_hits else None,
        "user_pattern_fail_rate": pattern_fail / pattern_hits if pattern_hits else None,
        "examples": examples,
    }


def chaos_vs_structure_tests(df, atr_arr) -> dict[str, Any]:
    """Autocorrelation / Hurst-ish / variance ratio as chaos proxies."""
    c = df["close"].to_numpy(float)
    rets = np.diff(np.log(np.maximum(c, 1e-9)))
    # lag-1 autocorr
    if len(rets) < 100:
        return {}
    r0 = rets[1:]
    r1 = rets[:-1]
    ac1 = float(np.corrcoef(r0, r1)[0, 1])
    # variance ratio for q=10
    q = 10
    var1 = np.var(rets)
    rq = np.array([np.sum(rets[i : i + q]) for i in range(0, len(rets) - q, q)])
    varq = np.var(rq) / q
    vr = float(varq / var1) if var1 > 0 else None
    # regime persistence already elsewhere
    # signed return after structure confirmation vs random timestamps
    rng = np.random.default_rng(42)
    struct_hits = []
    for i in range(80, len(df) - 20, 15):
        reg = classify_regime_v2(df, atr_arr, i, 40)
        if reg in ("spike", "tight_channel", "normal_channel"):
            direction = np.sign(c[i] - c[i - 10])
            fut = np.sign(c[i + 15] - c[i])
            if direction != 0 and fut != 0:
                struct_hits.append(1 if direction == fut else 0)
    rand_hits = []
    idxs = rng.integers(80, len(df) - 20, size=min(2000, max(100, len(struct_hits) * 3)))
    for i in idxs:
        direction = np.sign(c[i] - c[i - 10])
        fut = np.sign(c[i + 15] - c[i])
        if direction != 0 and fut != 0:
            rand_hits.append(1 if direction == fut else 0)
    return {
        "return_autocorr_lag1": ac1,
        "variance_ratio_q10": vr,
        "structure_followthrough_hit": float(np.mean(struct_hits)) if struct_hits else None,
        "n_structure": len(struct_hits),
        "random_momentum_hit": float(np.mean(rand_hits)) if rand_hits else None,
        "n_random": len(rand_hits),
        "structure_edge_vs_random": (
            float(np.mean(struct_hits) - np.mean(rand_hits)) if struct_hits and rand_hits else None
        ),
    }


def recent_chart_read(df, atr_arr, n_bars: int = 120) -> dict[str, Any]:
    """Algorithmic 'chart reading' of the latest window — narrative for report."""
    end = len(df) - 1
    start = max(0, end - n_bars + 1)
    window = df.iloc[start : end + 1].reset_index(drop=True)
    w_atr = atr_arr[start : end + 1]
    swings = find_swings(window, w_atr, atr_mult=0.9)
    legs = legs_from_swings(swings)
    for lg in legs:
        lg["pushes"] = count_pushes_relaxed(window, lg, 0.12)
    ranges = detect_ranges(legs, w_atr, min_legs=2)
    # multi-scale regimes
    scales = {}
    for win in (20, 40, 80, 120):
        if end >= win:
            scales[f"W{win}"] = classify_regime_v2(df, atr_arr, end, win)
    # last 5 bar summary
    c = window["close"].to_numpy(float)
    o = window["open"].to_numpy(float)
    h = window["high"].to_numpy(float)
    l = window["low"].to_numpy(float)
    last5 = []
    for i in range(max(0, len(window) - 5), len(window)):
        body = c[i] - o[i]
        last5.append(
            {
                "t": str(window["dt"].iloc[i]),
                "close": float(c[i]),
                "body": float(body),
                "range": float(h[i] - l[i]),
                "bull": bool(body > 0),
            }
        )
    return {
        "window_start": str(window["dt"].iloc[0]),
        "window_end": str(window["dt"].iloc[-1]),
        "multi_scale_regime": scales,
        "n_legs": len(legs),
        "legs": [
            {
                "dir": lg["dir"],
                "bars": lg["bars"],
                "pushes": lg["pushes"],
                "range": lg["range"],
                "t0": str(window["dt"].iloc[lg["i0"]]),
                "t1": str(window["dt"].iloc[lg["i1"]]),
            }
            for lg in legs[-8:]
        ],
        "ranges": [
            {
                "n_legs": r["n_legs"],
                "high": r["high"],
                "low": r["low"],
                "t0": str(window["dt"].iloc[r["bar0"]]),
                "t1": str(window["dt"].iloc[r["bar1"]]),
            }
            for r in ranges[-3:]
        ],
        "last5": last5,
        "interpretation": _interpret(scales, legs, ranges, last5),
    }


def _interpret(scales, legs, ranges, last5):
    notes = []
    notes.append(f"多尺度状态: " + ", ".join(f"{k}={v}" for k, v in scales.items()))
    if len(set(scales.values())) == 1:
        notes.append("各窗口同态 → 周期相对稳定，非瞬时翻转。")
    else:
        notes.append("各窗口异态 → 正在嵌套/转换；以短窗定执行方向、长窗定背景风险。")
    if legs:
        recent = legs[-3:]
        notes.append(
            "最近波段: "
            + "; ".join(f"{lg['dir']} {lg['pushes']}推/{lg['bars']}棒" for lg in recent)
        )
    if ranges:
        r = ranges[-1]
        notes.append(f"最近震荡盒: {r['n_legs']}腿, 高={r['high']:.2f}, 低={r['low']:.2f}")
    bull = sum(1 for b in last5 if b["bull"])
    notes.append(f"最近5棒多头棒数={bull}/5 — 仅反映即时惯性，不足以单独宣告周期切换。")
    return notes


def analyze(symbol, tf, path):
    df = load_ohlc(path, MAX_BARS.get(tf, 40000))
    atr_arr = atr(df, 14)
    series = regime_series_v2(df, atr_arr)
    mk = markov(series)
    spike = spike_aftermath_v2(df, atr_arr)
    nest = nested_structure_scan(df, atr_arr)
    edge = directional_edge_tests(df, atr_arr)
    chaos = chaos_vs_structure_tests(df, atr_arr)
    chart = recent_chart_read(df, atr_arr, 120)
    return {
        "symbol": symbol,
        "timeframe": tf,
        "bars": len(df),
        "start": str(df["dt"].iloc[0]),
        "end": str(df["dt"].iloc[-1]),
        "markov_v2": mk,
        "spike_aftermath_v2": spike,
        "nesting_v2": nest,
        "directional_edge": edge,
        "chaos_tests": chaos,
        "recent_chart_read": chart,
    }


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        x = float(obj)
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    return obj


def main():
    # Focus on H1 + D1 for speed/clarity; still include M15 for XAU and US100
    focus = [
        ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
        ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
        ("XAUUSD", "D1", "XAUUSD_D1.parquet"),
        ("US100", "H1", "US100.cash_H1.parquet"),
        ("US100", "D1", "US100.cash_D1.parquet"),
        ("US500", "H1", "US500.cash_H1.parquet"),
        ("US500", "D1", "US500.cash_D1.parquet"),
    ]
    results = []
    for symbol, tf, fname in focus:
        path = DATA_DIR / fname
        print("Refined", symbol, tf)
        r = analyze(symbol, tf, path)
        r = _jsonable(r)
        results.append(r)
        with open(OUT_DIR / f"{symbol}_{tf}_refined.json", "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(
            "  regimes", r["markov_v2"]["regime_counts"],
            "spike_n", r["spike_aftermath_v2"]["n_spikes"],
            "dist", r["spike_aftermath_v2"]["distribution"],
            "legs3+", round(r["nesting_v2"]["pct_legs_3plus"], 3),
            "pattern", r["nesting_v2"]["user_pattern_count"],
            "mm_rate", r["nesting_v2"]["user_pattern_mm_rate"],
        )

    # pool
    spike_c = Counter()
    for r in results:
        spike_c.update(r["spike_aftermath_v2"]["counts"])
    st = sum(spike_c.values()) or 1
    pattern_n = sum(r["nesting_v2"]["user_pattern_count"] for r in results)
    pattern_mm = sum(r["nesting_v2"]["user_pattern_mm_hit"] for r in results)
    pattern_fail = sum(r["nesting_v2"]["user_pattern_fail"] for r in results)
    edges = defaultdict(list)
    for r in results:
        e = r["directional_edge"].get("h10") or {}
        for k, v in e.items():
            if v:
                edges[k].append(v["hit_rate"])
    summary = {
        "pooled_spike_aftermath": {k: v / st for k, v in spike_c.items()},
        "pooled_spike_counts": dict(spike_c),
        "user_pattern_total": pattern_n,
        "user_pattern_mm_rate": pattern_mm / pattern_n if pattern_n else None,
        "user_pattern_fail_rate": pattern_fail / pattern_n if pattern_n else None,
        "avg_edge_h10": {k: float(np.mean(vs)) for k, vs in edges.items()},
        "avg_persist": float(np.mean([r["markov_v2"]["persistence_accuracy"] for r in results])),
        "avg_structure_edge": float(
            np.mean([r["chaos_tests"]["structure_edge_vs_random"] for r in results if r["chaos_tests"].get("structure_edge_vs_random") is not None])
        ),
        "per_dataset": results,
    }
    with open(OUT_DIR / "SUMMARY_REFINED.json", "w", encoding="utf-8") as f:
        json.dump(_jsonable(summary), f, ensure_ascii=False, indent=2)
    print("Wrote SUMMARY_REFINED.json")
    print("pooled spike", summary["pooled_spike_aftermath"])
    print("pattern mm", summary["user_pattern_mm_rate"], "fail", summary["user_pattern_fail_rate"])
    print("edges", summary["avg_edge_h10"])
    print("struct edge", summary["avg_structure_edge"])


if __name__ == "__main__":
    main()
