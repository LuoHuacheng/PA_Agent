#!/usr/bin/env python3
"""Strict re-audit: spike aftermath — Brooks 60% channel / 30% TR / 10% reversal.

Prior refined study reported ~38% 'reversal' — likely inflated by:
- loose spike labels
- counting deep pullbacks as reversals
- mapping continued-spike into channel

This script reports multiple spike defs × multiple aftermath defs.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np

from pa_cycle_nesting_experiment import DATA_DIR, MAX_BARS, atr, ema, load_ohlc

OUT = Path(__file__).resolve().parent / "out"


def bar_features(df, atr_arr, i: int):
    o = float(df["open"].iloc[i])
    h = float(df["high"].iloc[i])
    l = float(df["low"].iloc[i])
    c = float(df["close"].iloc[i])
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    direction = 1 if c > o else (-1 if c < o else 0)
    atr_m = float(atr_arr[i]) if atr_arr[i] == atr_arr[i] else rng
    return {
        "o": o,
        "h": h,
        "l": l,
        "c": c,
        "body": body,
        "rng": rng,
        "dir": direction,
        "body_atr": body / max(atr_m, 1e-12),
        "close_pos": (c - l) / rng,  # 1=close on high
        "atr": atr_m,
    }


def overlap_ratio(a, b) -> float:
    ov = max(0.0, min(a["h"], b["h"]) - max(a["l"], b["l"]))
    w = max(a["h"], b["h"]) - min(a["l"], b["l"])
    return ov / w if w > 0 else 1.0


def find_spike_events(df, atr_arr, spec: dict) -> list[dict]:
    """Return spike events: {end, start, dir, move, n_bars, strength}."""
    n = len(df)
    events = []
    i = spec.get("min_start", 30)
    min_run = spec["min_run"]
    min_body_atr = spec["min_body_atr"]
    max_overlap = spec["max_avg_overlap"]
    require_ext = spec.get("require_new_ext", True)
    cooldown = spec.get("cooldown", 20)

    while i < n - 5:
        f0 = bar_features(df, atr_arr, i)
        if f0["dir"] == 0 or f0["body_atr"] < min_body_atr:
            i += 1
            continue
        direction = f0["dir"]
        run = [i]
        j = i + 1
        while j < n and len(run) < spec.get("max_run", 12):
            fj = bar_features(df, atr_arr, j)
            if fj["dir"] != direction or fj["body_atr"] < min_body_atr * 0.75:
                break
            # overlap with previous
            prev = bar_features(df, atr_arr, run[-1])
            if overlap_ratio(prev, fj) > max_overlap + 0.15:
                break
            if require_ext:
                if direction > 0 and fj["h"] < prev["h"] and fj["c"] < prev["c"]:
                    break
                if direction < 0 and fj["l"] > prev["l"] and fj["c"] > prev["c"]:
                    break
            run.append(j)
            j += 1

        if len(run) >= min_run:
            feats = [bar_features(df, atr_arr, k) for k in run]
            overlaps = [overlap_ratio(feats[a], feats[a + 1]) for a in range(len(feats) - 1)]
            avg_ov = float(np.mean(overlaps)) if overlaps else 1.0
            if avg_ov <= max_overlap:
                start, end = run[0], run[-1]
                # optional: prior bar not already same strong direction (onset)
                if start > 0:
                    prev = bar_features(df, atr_arr, start - 1)
                    if prev["dir"] == direction and prev["body_atr"] >= min_body_atr:
                        i = end + 1
                        continue
                move = feats[-1]["c"] - feats[0]["o"] if direction > 0 else feats[0]["o"] - feats[-1]["c"]
                # signed move in price
                signed = feats[-1]["c"] - float(df["close"].iloc[start - 1]) if start > 0 else feats[-1]["c"] - feats[0]["o"]
                if direction < 0:
                    # ensure signed negative for down spike
                    pass
                events.append(
                    {
                        "start": start,
                        "end": end,
                        "dir": direction,
                        "n_bars": len(run),
                        "avg_overlap": avg_ov,
                        "move": abs(float(df["close"].iloc[end]) - float(df["close"].iloc[start])),
                        "ext_high": max(f["h"] for f in feats),
                        "ext_low": min(f["l"] for f in feats),
                        "origin": float(df["low"].iloc[start]) if direction > 0 else float(df["high"].iloc[start]),
                        "end_close": float(df["close"].iloc[end]),
                    }
                )
                i = end + cooldown
                continue
        i += 1
    return events


def classify_aftermath(df, atr_arr, ev: dict, horizon: int, rev_spec: dict) -> str:
    """Return channel | trading_range | reversal | continued_spike."""
    end = ev["end"]
    n = len(df)
    fut_end = min(n - 1, end + horizon)
    if fut_end <= end + 3:
        return "other"

    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)

    direction = ev["dir"]
    spike_move = max(ev["move"], 1e-12)
    origin = ev["origin"]
    ext_h, ext_l = ev["ext_high"], ev["ext_low"]

    # future window stats
    fut_c = c[end + 1 : fut_end + 1]
    fut_h = h[end + 1 : fut_end + 1]
    fut_l = l[end + 1 : fut_end + 1]
    if len(fut_c) < 4:
        return "other"

    # max adverse excursion (MAE) vs spike direction
    if direction > 0:
        mae = max(0.0, float(c[end] - fut_l.min()))
        mfe = max(0.0, float(fut_h.max() - c[end]))
        broke_origin = float(fut_l.min()) < origin - rev_spec.get("origin_buffer_atr", 0.0) * float(atr_arr[end])
        opposite_ext = float(fut_l.min())
        # opposite spike: consecutive down bars
    else:
        mae = max(0.0, float(fut_h.max() - c[end]))
        mfe = max(0.0, float(c[end] - fut_l.min()))
        broke_origin = float(fut_h.max()) > origin + rev_spec.get("origin_buffer_atr", 0.0) * float(atr_arr[end])
        opposite_ext = float(fut_h.max())

    mae_ratio = mae / spike_move
    mfe_ratio = mfe / spike_move
    net = float(c[fut_end] - c[end])
    net_with = net * direction  # >0 continuation

    # continued spike: more strong same-dir bars early
    cont = 0
    for j in range(end + 1, min(n, end + 6)):
        f = bar_features(df, atr_arr, j)
        if f["dir"] == direction and f["body_atr"] >= 0.45:
            cont += 1
        else:
            break
    if cont >= 2 and mae_ratio < 0.35:
        return "continued_spike"

    # reversal ladder
    mode = rev_spec["mode"]
    is_rev = False
    if mode == "loose_55":
        # old study style
        is_rev = net_with < 0 and abs(net) > 0.55 * spike_move
    elif mode == "mae_70":
        is_rev = mae_ratio >= 0.70 and net_with < 0
    elif mode == "mae_100_or_origin":
        is_rev = (mae_ratio >= 1.0) or broke_origin
        # require close also against spike by end, or origin break confirmed by close
        if broke_origin:
            if direction > 0:
                is_rev = any(c[j] < origin for j in range(end + 1, fut_end + 1))
            else:
                is_rev = any(c[j] > origin for j in range(end + 1, fut_end + 1))
        else:
            is_rev = mae_ratio >= 1.0 and net_with < 0
    elif mode == "strict_brooks":
        # True reversal: give back entire spike (MAE>=100%) AND close beyond spike origin
        # OR opposite 2+ trend-bar spike against
        origin_break_close = False
        if direction > 0:
            origin_break_close = any(c[j] < origin for j in range(end + 1, fut_end + 1))
        else:
            origin_break_close = any(c[j] > origin for j in range(end + 1, fut_end + 1))
        full_retracement = mae_ratio >= 1.0
        # opposite mini-spike
        opp = 0
        for j in range(end + 1, fut_end + 1):
            f = bar_features(df, atr_arr, j)
            if f["dir"] == -direction and f["body_atr"] >= 0.5:
                opp += 1
                if opp >= 2 and origin_break_close:
                    break
            else:
                if opp < 2:
                    opp = 0
        opp_spike = opp >= 2 and origin_break_close
        is_rev = (full_retracement and origin_break_close) or opp_spike
    elif mode == "strict_plus_hold":
        # strict + still on wrong side at horizon end
        origin_break_close = (
            any(c[j] < origin for j in range(end + 1, fut_end + 1))
            if direction > 0
            else any(c[j] > origin for j in range(end + 1, fut_end + 1))
        )
        full_retracement = mae_ratio >= 1.0
        still_wrong = net_with < 0 and abs(net) > 0.25 * spike_move
        is_rev = full_retracement and origin_break_close and still_wrong
    else:
        raise ValueError(mode)

    if is_rev:
        return "reversal"

    # trading range: choppy, high overlap, low efficiency in future window
    overlaps = []
    for j in range(end + 1, fut_end + 1):
        a = {"h": h[j - 1], "l": l[j - 1]}
        b = {"h": h[j], "l": l[j]}
        overlaps.append(overlap_ratio(a, b))
    ov = float(np.mean(overlaps))
    path = float(np.sum(h[end + 1 : fut_end + 1] - l[end + 1 : fut_end + 1]))
    efficiency = abs(net) / path if path > 0 else 0.0
    width_atr = float(fut_h.max() - fut_l.min()) / max(float(atr_arr[end]), 1e-12)

    # pullback then resume = channel family
    # classic: MAE between 25-70% but net still with trend OR mfe extends
    if (0.15 <= mae_ratio <= 0.85 and (net_with > 0 or mfe_ratio >= 0.35)) and efficiency >= 0.12:
        return "channel"
    if ov > 0.55 or (efficiency < 0.12 and width_atr < 4.5):
        return "trading_range"
    if net_with > 0 or mfe_ratio >= 0.5:
        return "channel"
    # weak / ambiguous — if mild adverse and no extension, TR
    if mae_ratio >= 0.35 and mfe_ratio < 0.25:
        return "trading_range"
    return "channel"


def map_brooks_bucket(label: str) -> str:
    if label == "continued_spike":
        return "channel"  # Brooks often folds ongoing spike into trend/channel family
    if label in ("channel", "trading_range", "reversal"):
        return label
    return "other"


def run_combo(df, atr_arr, spike_spec, rev_mode, horizon) -> dict:
    events = find_spike_events(df, atr_arr, spike_spec)
    counts = Counter()
    raw = Counter()
    for ev in events:
        if ev["end"] + horizon >= len(df):
            continue
        lab = classify_aftermath(df, atr_arr, ev, horizon, {"mode": rev_mode})
        raw[lab] += 1
        counts[map_brooks_bucket(lab)] += 1
    total = sum(counts.values()) or 1
    return {
        "n": sum(counts.values()),
        "n_events_detected": len(events),
        "distribution": {k: counts[k] / total for k in ("channel", "trading_range", "reversal", "other")},
        "raw": dict(raw),
        "counts": dict(counts),
    }


SPIKE_SPECS = {
    "loose_oldproxy": {
        # approximates prior regime classifier sensitivity
        "min_run": 2,
        "min_body_atr": 0.35,
        "max_avg_overlap": 0.55,
        "require_new_ext": False,
        "cooldown": 15,
        "max_run": 10,
    },
    "standard_brooks": {
        "min_run": 3,
        "min_body_atr": 0.55,
        "max_avg_overlap": 0.35,
        "require_new_ext": True,
        "cooldown": 25,
        "max_run": 8,
    },
    "strict_spike": {
        "min_run": 3,
        "min_body_atr": 0.70,
        "max_avg_overlap": 0.25,
        "require_new_ext": True,
        "cooldown": 30,
        "max_run": 8,
    },
    "very_strict": {
        "min_run": 4,
        "min_body_atr": 0.80,
        "max_avg_overlap": 0.22,
        "require_new_ext": True,
        "cooldown": 35,
        "max_run": 10,
    },
}

REV_MODES = ["loose_55", "mae_70", "mae_100_or_origin", "strict_brooks", "strict_plus_hold"]
HORIZONS = [20, 30, 40]


def main():
    datasets = [
        ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
        ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
        ("XAUUSD", "D1", "XAUUSD_D1.parquet"),
        ("US100", "H1", "US100.cash_H1.parquet"),
        ("US100", "D1", "US100.cash_D1.parquet"),
        ("US500", "H1", "US500.cash_H1.parquet"),
        ("US500", "D1", "US500.cash_D1.parquet"),
    ]

    # Primary matrix: focus on key combos + full pooled for standard×strict_brooks
    matrix_pool = {}
    per_dataset = []

    for sym, tf, fn in datasets:
        path = DATA_DIR / fn
        df = load_ohlc(path, MAX_BARS.get(tf, 40000))
        atr_arr = atr(df)
        print(f"\n=== {sym} {tf} bars={len(df)} ===")
        ds = {"symbol": sym, "timeframe": tf, "bars": len(df), "combos": {}}

        for sname, sspec in SPIKE_SPECS.items():
            for rev in REV_MODES:
                for hz in HORIZONS:
                    key = f"{sname}|{rev}|h{hz}"
                    res = run_combo(df, atr_arr, sspec, rev, hz)
                    ds["combos"][key] = res
                    # pool
                    mp = matrix_pool.setdefault(key, Counter())
                    for k, v in res["counts"].items():
                        mp[k] += v
                    if sname == "standard_brooks" and rev == "strict_brooks" and hz == 30:
                        d = res["distribution"]
                        print(
                            f"  std/strict/h30 n={res['n']} ch={d['channel']:.1%} "
                            f"tr={d['trading_range']:.1%} rev={d['reversal']:.1%}"
                        )
        per_dataset.append(ds)

    pooled = {}
    for key, ctr in matrix_pool.items():
        total = sum(ctr.values()) or 1
        pooled[key] = {
            "n": total,
            "distribution": {k: ctr.get(k, 0) / total for k in ("channel", "trading_range", "reversal", "other")},
            "counts": dict(ctr),
        }

    # Print comparison table for h30
    print("\n======== POOLED h30 ========")
    print(f"{'spike_def':22} {'rev_mode':22} {'n':>6} {'chan':>8} {'TR':>8} {'REV':>8}")
    for sname in SPIKE_SPECS:
        for rev in REV_MODES:
            key = f"{sname}|{rev}|h30"
            p = pooled[key]
            d = p["distribution"]
            print(
                f"{sname:22} {rev:22} {p['n']:6d} {d['channel']:8.1%} {d['trading_range']:8.1%} {d['reversal']:8.1%}"
            )

    # Highlight recommended
    focus_keys = [
        "loose_oldproxy|loose_55|h30",
        "standard_brooks|loose_55|h30",
        "standard_brooks|mae_70|h30",
        "standard_brooks|strict_brooks|h30",
        "standard_brooks|strict_plus_hold|h30",
        "strict_spike|strict_brooks|h30",
        "very_strict|strict_brooks|h30",
        "strict_spike|strict_brooks|h20",
        "strict_spike|strict_brooks|h40",
    ]
    print("\n======== FOCUS ========")
    focus = {}
    for k in focus_keys:
        focus[k] = pooled[k]
        d = pooled[k]["distribution"]
        print(f"{k}: n={pooled[k]['n']} ch={d['channel']:.1%} tr={d['trading_range']:.1%} rev={d['reversal']:.1%}")

    summary = {
        "brooks_claim": {"channel": 0.60, "trading_range": 0.30, "reversal": 0.10},
        "prior_study_approx": {"note": "loose regime spike + loose_55 reversal ≈ 38% rev"},
        "focus_pooled": focus,
        "all_pooled_h30": {
            k: pooled[k] for k in pooled if k.endswith("|h30")
        },
        "definitions": {
            "spike": SPIKE_SPECS,
            "reversal_modes": {
                "loose_55": "net against spike by >55% of spike size (OLD — too loose)",
                "mae_70": "MAE>=70% of spike and net against",
                "mae_100_or_origin": "full retrace or close beyond spike origin",
                "strict_brooks": "MAE>=100% AND close beyond origin, OR opposite 2-bar spike through origin",
                "strict_plus_hold": "strict_brooks + still wrong-side at horizon",
            },
            "channel": "pullback then resume / extension with trend; continued_spike mapped into channel",
            "trading_range": "high overlap / low efficiency chop without origin break",
        },
        "verdict_guide": {
            "if_old_loose": "reproduces high reversal % — definition artifact",
            "if_strict_brooks": "compare to 10% claim under tradable reversal meaning",
        },
    }
    # don't dump full per_dataset (huge); keep only focus combos per ds
    slim = []
    for ds in per_dataset:
        slim.append(
            {
                "symbol": ds["symbol"],
                "timeframe": ds["timeframe"],
                "bars": ds["bars"],
                "focus": {k: ds["combos"][k] for k in focus_keys if k in ds["combos"]},
            }
        )
    summary["per_dataset_focus"] = slim

    OUT.mkdir(exist_ok=True)
    (OUT / "SPIKE_AFTERMATH_AUDIT.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\nwrote", OUT / "SPIKE_AFTERMATH_AUDIT.json")


if __name__ == "__main__":
    main()
