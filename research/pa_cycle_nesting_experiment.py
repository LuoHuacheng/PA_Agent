#!/usr/bin/env python3
"""PA nesting / cycle-evolution empirical study on MT5 parquet data.

Tests (objectively, no LLM):
1) Nested structure existence: micro legs compose macro ranges
2) Brooks probability anchors (TR breakout->MM, spike aftermath)
3) Regime Markov transitions + predictability vs null
4) Early detection lag: when can cycle change be recognized?
5) Forward return by regime / transition type

Outputs JSON summary under research/out/
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DATA_DIR = Path(r"D:\MT5_K线数据")
OUT_DIR = Path(__file__).resolve().parent / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DATASETS = [
    ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
    ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
    ("XAUUSD", "D1", "XAUUSD_D1.parquet"),
    ("US100", "H1", "US100.cash_H1.parquet"),
    ("US100", "M15", "US100.cash_M15.parquet"),
    ("US100", "D1", "US100.cash_D1.parquet"),
    ("US500", "H1", "US500.cash_H1.parquet"),
    ("US500", "M15", "US500.cash_M15.parquet"),
    ("US500", "D1", "US500.cash_D1.parquet"),
]

# Use recent contiguous history for speed / relevance
MAX_BARS = {
    "M15": 80000,
    "H1": 50000,
    "D1": 20000,
}


@dataclass
class Swing:
    i: int
    kind: str  # H or L
    price: float


def load_ohlc(path: Path, max_bars: int) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df.rename(columns={c: c.lower() for c in df.columns})
    need = ["time", "open", "high", "low", "close"]
    for c in need:
        if c not in df.columns:
            raise ValueError(f"missing {c} in {path}")
    df = df[need].dropna().reset_index(drop=True)
    if len(df) > max_bars:
        df = df.iloc[-max_bars:].reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    prev = np.roll(c, 1)
    prev[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    out = np.empty_like(tr)
    out[:n] = np.nan
    if len(tr) < n:
        return out
    out[n - 1] = tr[:n].mean()
    alpha = 1.0 / n
    for i in range(n, len(tr)):
        out[i] = out[i - 1] * (1 - alpha) + tr[i] * alpha
    # backfill early
    first = out[n - 1]
    out[: n - 1] = first
    return out


def find_swings(df: pd.DataFrame, atr_arr: np.ndarray, atr_mult: float = 1.2, left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal pivots filtered by min ATR distance (zigzag-like)."""
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    n = len(df)
    raw: list[Swing] = []
    for i in range(left, n - right):
        if h[i] >= h[i - left : i + right + 1].max() and h[i] > h[i - 1] and h[i] >= h[i + 1]:
            raw.append(Swing(i, "H", float(h[i])))
        if l[i] <= l[i - left : i + right + 1].min() and l[i] < l[i - 1] and l[i] <= l[i + 1]:
            raw.append(Swing(i, "L", float(l[i])))
    if not raw:
        return []
    raw.sort(key=lambda s: s.i)

    # Alternate + min move
    filtered: list[Swing] = [raw[0]]
    for s in raw[1:]:
        last = filtered[-1]
        thr = float(atr_arr[s.i]) * atr_mult
        if thr <= 0 or math.isnan(thr):
            thr = abs(last.price) * 0.001
        if s.kind == last.kind:
            # keep more extreme
            if last.kind == "H" and s.price >= last.price:
                filtered[-1] = s
            elif last.kind == "L" and s.price <= last.price:
                filtered[-1] = s
            continue
        if abs(s.price - last.price) < thr:
            # too small — absorb into last extremity if same direction extreme
            continue
        filtered.append(s)
    return filtered


def legs_from_swings(swings: list[Swing]) -> list[dict[str, Any]]:
    legs = []
    for a, b in zip(swings, swings[1:]):
        direction = "up" if b.price > a.price else "down"
        legs.append(
            {
                "i0": a.i,
                "i1": b.i,
                "p0": a.price,
                "p1": b.price,
                "dir": direction,
                "bars": b.i - a.i,
                "range": abs(b.price - a.price),
            }
        )
    return legs


def count_pushes_in_leg(df: pd.DataFrame, leg: dict[str, Any], atr_arr: np.ndarray) -> int:
    """Count micro swings inside a macro leg (3-push / 4-push heuristic)."""
    i0, i1 = leg["i0"], leg["i1"]
    if i1 - i0 < 6:
        return 1
    sub = df.iloc[i0 : i1 + 1].reset_index(drop=True)
    sub_atr = atr_arr[i0 : i1 + 1]
    # finer swings
    swings = find_swings(sub, sub_atr, atr_mult=0.55, left=1, right=1)
    if len(swings) < 2:
        return 1
    # count same-direction micro legs
    pushes = 0
    for a, b in zip(swings, swings[1:]):
        d = "up" if b.price > a.price else "down"
        if d == leg["dir"]:
            pushes += 1
    return max(1, pushes)


def detect_ranges(legs: list[dict[str, Any]], atr_arr: np.ndarray, min_legs: int = 2) -> list[dict[str, Any]]:
    """A range = alternating up/down legs with overlapping price envelopes."""
    ranges: list[dict[str, Any]] = []
    i = 0
    while i < len(legs) - 1:
        # start candidate with two opposite legs
        if legs[i]["dir"] == legs[i + 1]["dir"]:
            i += 1
            continue
        j = i + 1
        highs = [max(legs[i]["p0"], legs[i]["p1"]), max(legs[j]["p0"], legs[j]["p1"])]
        lows = [min(legs[i]["p0"], legs[i]["p1"]), min(legs[j]["p0"], legs[j]["p1"])]
        while j + 1 < len(legs):
            nxt = legs[j + 1]
            if nxt["dir"] == legs[j]["dir"]:
                break
            nh, nl = max(nxt["p0"], nxt["p1"]), min(nxt["p0"], nxt["p1"])
            rh, rl = max(highs), min(lows)
            # must substantially overlap current envelope
            overlap = min(rh, nh) - max(rl, nl)
            width = max(rh, nh) - min(rl, nl)
            if width <= 0 or overlap / width < 0.35:
                break
            # don't allow runaway expansion (> 2.5x prior width)
            prior_w = rh - rl
            if prior_w > 0 and (max(rh, nh) - min(rl, nl)) / prior_w > 2.5:
                break
            j += 1
            highs.append(nh)
            lows.append(nl)
        nlegs = j - i + 1
        if nlegs >= min_legs:
            rh, rl = max(highs), min(lows)
            mid_i = (legs[i]["i0"] + legs[j]["i1"]) // 2
            atr_m = float(atr_arr[min(mid_i, len(atr_arr) - 1)])
            if atr_m > 0 and (rh - rl) >= 1.5 * atr_m:
                ranges.append(
                    {
                        "leg_i0": i,
                        "leg_i1": j,
                        "bar0": legs[i]["i0"],
                        "bar1": legs[j]["i1"],
                        "high": rh,
                        "low": rl,
                        "height": rh - rl,
                        "n_legs": nlegs,
                        "up_legs": sum(1 for k in range(i, j + 1) if legs[k]["dir"] == "up"),
                        "down_legs": sum(1 for k in range(i, j + 1) if legs[k]["dir"] == "down"),
                    }
                )
                i = j + 1
                continue
        i += 1
    return ranges


def classify_regime_window(
    df: pd.DataFrame,
    atr_arr: np.ndarray,
    end: int,
    win: int = 40,
) -> str:
    """Objective 5-state regime on [end-win+1, end]."""
    start = max(0, end - win + 1)
    if end - start < 10:
        return "unknown"
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    body = np.abs(c[start : end + 1] - o[start : end + 1])
    rng = h[start : end + 1] - l[start : end + 1]
    atr_m = float(np.nanmean(atr_arr[start : end + 1]))
    if atr_m <= 0:
        return "unknown"

    # overlap ratio of consecutive bars
    overlaps = []
    for i in range(start + 1, end + 1):
        ov = max(0.0, min(h[i], h[i - 1]) - max(l[i], l[i - 1]))
        w = max(h[i], h[i - 1]) - min(l[i], l[i - 1])
        overlaps.append(ov / w if w > 0 else 1.0)
    ov_mean = float(np.mean(overlaps)) if overlaps else 1.0

    # directional efficiency
    net = abs(c[end] - c[start])
    path = float(np.sum(rng[1:])) if len(rng) > 1 else net
    efficiency = net / path if path > 0 else 0.0

    # consecutive trend bars (same color, body > 0.6 ATR)
    bodies = body
    dirs = np.sign(c[start : end + 1] - o[start : end + 1])
    max_run = 1
    run = 1
    for i in range(1, len(dirs)):
        if dirs[i] == dirs[i - 1] and dirs[i] != 0 and bodies[i] > 0.55 * atr_m:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1

    # pullback depth via local swings in window
    sub = df.iloc[start : end + 1].reset_index(drop=True)
    sub_atr = atr_arr[start : end + 1]
    swings = find_swings(sub, sub_atr, atr_mult=0.8, left=1, right=1)
    pullback = 0.0
    if len(swings) >= 3:
        # last completed counter-move vs prior impulse
        for a, b, d in zip(swings[:-2], swings[1:-1], swings[2:]):
            impulse = abs(b.price - a.price)
            pb = abs(d.price - b.price)
            if impulse > 0:
                pullback = max(pullback, pb / impulse)

    # classification spectrum (Brooks-inspired, simplified)
    if max_run >= 3 and efficiency > 0.55 and ov_mean < 0.35:
        return "spike"
    if efficiency > 0.35 and ov_mean < 0.45 and pullback < 0.30:
        return "tight_channel"
    if efficiency > 0.22 and pullback < 0.55 and ov_mean < 0.55:
        return "normal_channel"
    if ov_mean > 0.55 or efficiency < 0.12:
        # extreme vs normal TR by width in ATR
        width_atr = (float(h[start : end + 1].max() - l[start : end + 1].min())) / atr_m
        if width_atr < 3.0 and ov_mean > 0.65:
            return "extreme_tr"
        return "trading_range"
    if pullback >= 0.50:
        return "broad_channel"
    return "trading_range"


def ema(series: np.ndarray, n: int = 20) -> np.ndarray:
    out = np.empty_like(series, dtype=float)
    if len(series) == 0:
        return out
    out[0] = series[0]
    a = 2 / (n + 1)
    for i in range(1, len(series)):
        out[i] = a * series[i] + (1 - a) * out[i - 1]
    return out


def evaluate_tr_breakout_mm(
    df: pd.DataFrame,
    ranges: list[dict[str, Any]],
    atr_arr: np.ndarray,
    horizon_mult: float = 2.5,
) -> dict[str, Any]:
    """After range ends, look for breakout and whether MM is hit before opposite extreme."""
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    events = []
    for r in ranges:
        b1 = r["bar1"]
        rh, rl, height = r["high"], r["low"], r["height"]
        if height <= 0:
            continue
        atr_m = float(atr_arr[min(b1, len(atr_arr) - 1)])
        # search breakout after range
        search_end = min(len(df) - 1, b1 + max(20, int(horizon_mult * (r["bar1"] - r["bar0"] + 1))))
        breakout_dir = None
        breakout_i = None
        for i in range(b1 + 1, search_end + 1):
            # close beyond boundary by 0.15 ATR
            if c[i] > rh + 0.15 * atr_m:
                breakout_dir = "up"
                breakout_i = i
                break
            if c[i] < rl - 0.15 * atr_m:
                breakout_dir = "down"
                breakout_i = i
                break
        if breakout_dir is None:
            events.append({"outcome": "no_breakout", **{k: r[k] for k in ("height", "n_legs", "bar0", "bar1")}})
            continue

        # failed breakout if reclaim opposite side within 8 bars
        fail = False
        for j in range(breakout_i + 1, min(len(df), breakout_i + 9)):
            if breakout_dir == "up" and c[j] < rh:
                fail = True
                break
            if breakout_dir == "down" and c[j] > rl:
                fail = True
                break

        if breakout_dir == "up":
            mm = breakout_i and (c[breakout_i] + height)  # approx
            mm = float(c[breakout_i] + height)
            # also classic: high + height from range high
            mm = rh + height
            hit = False
            ruined = False
            for j in range(breakout_i + 1, search_end + 1):
                if h[j] >= mm:
                    hit = True
                    break
                if l[j] < rl:  # full reverse through range
                    ruined = True
                    break
        else:
            mm = rl - height
            hit = False
            ruined = False
            for j in range(breakout_i + 1, search_end + 1):
                if l[j] <= mm:
                    hit = True
                    break
                if h[j] > rh:
                    ruined = True
                    break

        if fail:
            outcome = "failed_breakout"
        elif hit:
            outcome = "mm_hit"
        elif ruined:
            outcome = "reversed_before_mm"
        else:
            outcome = "mm_miss_timeout"

        # extension beyond breakout in ATR
        if breakout_dir == "up":
            ext = float(h[breakout_i:search_end + 1].max() - rh) / height if height else 0
        else:
            ext = float(rl - l[breakout_i:search_end + 1].min()) / height if height else 0

        events.append(
            {
                "outcome": outcome,
                "dir": breakout_dir,
                "ext_ratio": ext,
                "fail": fail,
                "hit_mm": outcome == "mm_hit",
                "n_legs": r["n_legs"],
                "height": height,
                "bar0": r["bar0"],
                "bar1": r["bar1"],
                "bo_i": breakout_i,
            }
        )

    ctr = Counter(e["outcome"] for e in events)
    with_bo = [e for e in events if e["outcome"] != "no_breakout"]
    non_fail = [e for e in with_bo if e["outcome"] != "failed_breakout"]
    mm_rate_all_bo = (sum(1 for e in with_bo if e.get("hit_mm")) / len(with_bo)) if with_bo else None
    mm_rate_surviving = (sum(1 for e in non_fail if e.get("hit_mm")) / len(non_fail)) if non_fail else None
    fail_rate = (sum(1 for e in with_bo if e["outcome"] == "failed_breakout") / len(with_bo)) if with_bo else None
    return {
        "n_ranges": len(ranges),
        "n_events": len(events),
        "counts": dict(ctr),
        "breakout_fail_rate": fail_rate,
        "mm_hit_rate_given_breakout": mm_rate_all_bo,
        "mm_hit_rate_given_surviving_breakout": mm_rate_surviving,
        "mean_extension_ratio": float(np.mean([e["ext_ratio"] for e in with_bo])) if with_bo else None,
        "sample_events": events[:8],
    }


def evaluate_spike_aftermath(df: pd.DataFrame, atr_arr: np.ndarray, step: int = 5) -> dict[str, Any]:
    """When regime becomes spike, classify next 30-bar regime bucket."""
    n = len(df)
    outcomes = Counter()
    samples = 0
    i = 40
    while i < n - 35:
        reg = classify_regime_window(df, atr_arr, i, win=40)
        if reg != "spike":
            i += step
            continue
        # find spike start approx: look back while still spike-like
        fut = classify_regime_window(df, atr_arr, min(n - 1, i + 30), win=30)
        # map to Brooks buckets
        if fut in ("tight_channel", "normal_channel", "broad_channel"):
            bucket = "channel"
        elif fut in ("trading_range", "extreme_tr"):
            bucket = "trading_range"
        elif fut == "spike":
            # continued spike ~ still trend; count as channel-like continuation
            bucket = "channel"
        else:
            bucket = "other"
        # reversal: price reverse > 50% of spike move vs prior 15 bars
        c = df["close"].to_numpy(float)
        pre = c[i - 15]
        spike_move = c[i] - pre
        fut_move = c[min(n - 1, i + 30)] - c[i]
        if spike_move != 0 and (fut_move * spike_move < 0) and abs(fut_move) > 0.5 * abs(spike_move):
            bucket = "reversal"
        outcomes[bucket] += 1
        samples += 1
        i += 25  # skip ahead to reduce overlap
    total = sum(outcomes.values()) or 1
    return {
        "n_spikes": samples,
        "distribution": {k: v / total for k, v in outcomes.items()},
        "counts": dict(outcomes),
        "brooks_claim": {"channel": 0.60, "trading_range": 0.30, "reversal": 0.10},
    }


def regime_series(df: pd.DataFrame, atr_arr: np.ndarray, step: int = 10, win: int = 40) -> list[tuple[int, str]]:
    out = []
    for i in range(win, len(df), step):
        out.append((i, classify_regime_window(df, atr_arr, i, win=win)))
    return out


def markov_and_predictability(series: list[tuple[int, str]]) -> dict[str, Any]:
    trans = defaultdict(Counter)
    for (_, a), (_, b) in zip(series, series[1:]):
        trans[a][b] += 1
    probs = {}
    for a, ctr in trans.items():
        s = sum(ctr.values()) or 1
        probs[a] = {b: ctr[b] / s for b in ctr}
    # persistence baseline accuracy
    same = sum(1 for (_, a), (_, b) in zip(series, series[1:]) if a == b)
    n = max(1, len(series) - 1)
    persist_acc = same / n
    # majority-class null
    labels = [s for _, s in series]
    maj = Counter(labels).most_common(1)[0][0]
    maj_acc = sum(1 for s in labels[1:] if s == maj) / n
    # one-step Markov predictor (in-sample; still informative of structure)
    correct = 0
    for (_, a), (_, b) in zip(series, series[1:]):
        pred = max(trans[a], key=trans[a].get) if trans[a] else a
        if pred == b:
            correct += 1
    markov_acc = correct / n
    # entropy of transitions (bits)
    ent = {}
    for a, pmap in probs.items():
        e = 0.0
        for p in pmap.values():
            if p > 0:
                e -= p * math.log2(p)
        ent[a] = e
    return {
        "transition_probs": probs,
        "transition_entropy_bits": ent,
        "persistence_accuracy": persist_acc,
        "majority_null_accuracy": maj_acc,
        "markov_accuracy": markov_acc,
        "lift_vs_majority": markov_acc - maj_acc,
        "lift_vs_persistence": markov_acc - persist_acc,
        "n_steps": n,
        "regime_counts": dict(Counter(labels)),
    }


def early_detection_lag(df: pd.DataFrame, atr_arr: np.ndarray, step: int = 5) -> dict[str, Any]:
    """When true regime changes (non-overlapping windows), how many bars until new label sticks?"""
    series = regime_series(df, atr_arr, step=step, win=40)
    lags = []
    for k in range(1, len(series)):
        i0, r0 = series[k - 1]
        i1, r1 = series[k]
        if r0 == r1:
            continue
        # from i0, find first bar where classify == r1 for 2 consecutive checks
        found = None
        for j in range(i0 + 1, min(len(df) - 1, i1 + 40)):
            if j % step != 0:
                continue
            a = classify_regime_window(df, atr_arr, j, 40)
            b = classify_regime_window(df, atr_arr, min(len(df) - 1, j + step), 40)
            if a == r1 and b == r1:
                found = j - i0
                break
        if found is not None:
            lags.append(found)
    if not lags:
        return {"n_changes": 0}
    arr = np.array(lags, float)
    return {
        "n_changes": len(lags),
        "lag_mean_bars": float(arr.mean()),
        "lag_median_bars": float(np.median(arr)),
        "lag_p25": float(np.percentile(arr, 25)),
        "lag_p75": float(np.percentile(arr, 75)),
        "note": "lag measured in bars from previous regime sample to first sticky new label",
    }


def forward_returns_by_regime(df: pd.DataFrame, atr_arr: np.ndarray, horizon: int = 20) -> dict[str, Any]:
    c = df["close"].to_numpy(float)
    rows = defaultdict(list)
    for i in range(40, len(df) - horizon, 10):
        reg = classify_regime_window(df, atr_arr, i, 40)
        # signed by ema slope
        e = ema(c[max(0, i - 60) : i + 1], 20)
        slope = e[-1] - e[0]
        direction = 1.0 if slope >= 0 else -1.0
        fut = (c[i + horizon] - c[i]) / max(float(atr_arr[i]), 1e-9)
        rows[reg].append(fut * direction)  # positive = with-trend favorable
        rows[reg + "__abs"].append(abs(fut))
    out = {}
    for k, vals in rows.items():
        if k.endswith("__abs"):
            continue
        abs_key = k + "__abs"
        out[k] = {
            "n": len(vals),
            "mean_signed_atr": float(np.mean(vals)),
            "hit_rate_gt0": float(np.mean(np.array(vals) > 0)),
            "mean_abs_move_atr": float(np.mean(rows[abs_key])),
        }
    return out


def nesting_stats(df: pd.DataFrame, swings: list[Swing], legs: list[dict[str, Any]], atr_arr: np.ndarray) -> dict[str, Any]:
    push_counts = Counter()
    nested_examples = []
    for leg in legs:
        p = count_pushes_in_leg(df, leg, atr_arr)
        push_counts[p] += 1
        if 3 <= p <= 5 and leg["bars"] >= 15:
            nested_examples.append(
                {
                    "bar0": leg["i0"],
                    "bar1": leg["i1"],
                    "dir": leg["dir"],
                    "pushes": p,
                    "bars": leg["bars"],
                    "range": leg["range"],
                    "t0": str(df["dt"].iloc[leg["i0"]]),
                    "t1": str(df["dt"].iloc[leg["i1"]]),
                }
            )
    ranges = detect_ranges(legs, atr_arr)
    # nested: range composed of legs that themselves have 3+ pushes
    nested_ranges = 0
    for r in ranges:
        sub = legs[r["leg_i0"] : r["leg_i1"] + 1]
        if sum(1 for lg in sub if count_pushes_in_leg(df, lg, atr_arr) >= 3) >= 2:
            nested_ranges += 1
    return {
        "n_swings": len(swings),
        "n_legs": len(legs),
        "push_count_hist": {str(k): v for k, v in sorted(push_counts.items())},
        "pct_legs_with_3plus_pushes": (
            sum(v for k, v in push_counts.items() if k >= 3) / max(1, sum(push_counts.values()))
        ),
        "pct_legs_with_3or4_pushes": (
            sum(v for k, v in push_counts.items() if k in (3, 4)) / max(1, sum(push_counts.values()))
        ),
        "n_ranges": len(ranges),
        "pct_ranges_with_nested_3push_legs": nested_ranges / max(1, len(ranges)),
        "nested_leg_examples": nested_examples[:12],
        "ranges": ranges,
    }


def find_case_studies(df: pd.DataFrame, atr_arr: np.ndarray, nest: dict[str, Any], mm: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick concrete examples: nested range then MM-hit breakout."""
    cases = []
    events = mm.get("sample_events") or []
    # recompute full events for better selection
    ranges = nest["ranges"]
    full = evaluate_tr_breakout_mm(df, ranges, atr_arr)
    # monkey: get all by re-running detailed
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    for r in ranges:
        b1 = r["bar1"]
        rh, rl, height = r["high"], r["low"], r["height"]
        atr_m = float(atr_arr[min(b1, len(atr_arr) - 1)])
        search_end = min(len(df) - 1, b1 + max(20, int(2.5 * (r["bar1"] - r["bar0"] + 1))))
        bo_dir = None
        bo_i = None
        for i in range(b1 + 1, search_end + 1):
            if c[i] > rh + 0.15 * atr_m:
                bo_dir, bo_i = "up", i
                break
            if c[i] < rl - 0.15 * atr_m:
                bo_dir, bo_i = "down", i
                break
        if bo_dir is None:
            continue
        fail = False
        for j in range(bo_i + 1, min(len(df), bo_i + 9)):
            if bo_dir == "up" and c[j] < rh:
                fail = True
                break
            if bo_dir == "down" and c[j] > rl:
                fail = True
                break
        if fail:
            continue
        if bo_dir == "up":
            mm_px = rh + height
            hit = any(h[j] >= mm_px for j in range(bo_i + 1, search_end + 1))
        else:
            mm_px = rl - height
            hit = any(l[j] <= mm_px for j in range(bo_i + 1, search_end + 1))
        # nesting quality
        sub_legs = []
        # approximate: use nest push examples overlapping
        for ex in nest["nested_leg_examples"]:
            if ex["bar0"] >= r["bar0"] - 2 and ex["bar1"] <= r["bar1"] + 2:
                sub_legs.append(ex)
        if r["n_legs"] >= 2 and (hit or r["n_legs"] >= 3):
            cases.append(
                {
                    "t_range_start": str(df["dt"].iloc[r["bar0"]]),
                    "t_range_end": str(df["dt"].iloc[r["bar1"]]),
                    "t_breakout": str(df["dt"].iloc[bo_i]),
                    "range_high": rh,
                    "range_low": rl,
                    "height": height,
                    "n_legs": r["n_legs"],
                    "breakout_dir": bo_dir,
                    "mm_target": mm_px,
                    "mm_hit": hit,
                    "nested_micro_legs": sub_legs[:4],
                    "bar0": r["bar0"],
                    "bar1": r["bar1"],
                    "bo_i": bo_i,
                }
            )
        if len(cases) >= 6:
            break
    # prefer mm_hit cases first
    cases.sort(key=lambda x: (not x["mm_hit"], -x["n_legs"]))
    return cases[:5]


def analyze_one(symbol: str, tf: str, path: Path) -> dict[str, Any]:
    df = load_ohlc(path, MAX_BARS.get(tf, 40000))
    atr_arr = atr(df, 14)
    swings = find_swings(df, atr_arr, atr_mult=1.15)
    legs = legs_from_swings(swings)
    nest = nesting_stats(df, swings, legs, atr_arr)
    mm = evaluate_tr_breakout_mm(df, nest["ranges"], atr_arr)
    spike = evaluate_spike_aftermath(df, atr_arr)
    series = regime_series(df, atr_arr, step=10, win=40)
    markov = markov_and_predictability(series)
    lag = early_detection_lag(df, atr_arr, step=5)
    fwd = forward_returns_by_regime(df, atr_arr, horizon=20)
    cases = find_case_studies(df, atr_arr, nest, mm)

    # channel after range breakout style (user scenario)
    channel_after_bo = 0
    checked = 0
    for r in nest["ranges"]:
        b1 = r["bar1"]
        if b1 + 45 >= len(df):
            continue
        # if breakout occurred
        rh, rl = r["high"], r["low"]
        atr_m = float(atr_arr[b1])
        c = df["close"].to_numpy(float)
        bo = None
        for i in range(b1 + 1, min(len(df), b1 + 40)):
            if c[i] > rh + 0.15 * atr_m:
                bo = i
                break
            if c[i] < rl - 0.15 * atr_m:
                bo = i
                break
        if bo is None:
            continue
        checked += 1
        fut = classify_regime_window(df, atr_arr, min(len(df) - 1, bo + 35), win=35)
        if fut in ("tight_channel", "normal_channel", "broad_channel", "spike"):
            channel_after_bo += 1

    result = {
        "symbol": symbol,
        "timeframe": tf,
        "bars": len(df),
        "start": str(df["dt"].iloc[0]),
        "end": str(df["dt"].iloc[-1]),
        "nesting": {
            "n_swings": nest["n_swings"],
            "n_legs": nest["n_legs"],
            "push_count_hist": nest["push_count_hist"],
            "pct_legs_with_3plus_pushes": nest["pct_legs_with_3plus_pushes"],
            "pct_legs_with_3or4_pushes": nest["pct_legs_with_3or4_pushes"],
            "n_ranges": nest["n_ranges"],
            "pct_ranges_with_nested_3push_legs": nest["pct_ranges_with_nested_3push_legs"],
            "nested_leg_examples": nest["nested_leg_examples"][:6],
        },
        "tr_breakout_mm": {k: v for k, v in mm.items() if k != "sample_events"},
        "spike_aftermath": spike,
        "markov": markov,
        "early_detection": lag,
        "forward_returns_by_regime": fwd,
        "pct_breakouts_followed_by_channelish": (channel_after_bo / checked) if checked else None,
        "n_breakouts_checked_for_channel": checked,
        "case_studies": cases,
    }
    return result


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(xs):
        xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
        return float(np.mean(xs)) if xs else None

    mm_all = avg([r["tr_breakout_mm"]["mm_hit_rate_given_breakout"] for r in results])
    mm_surv = avg([r["tr_breakout_mm"]["mm_hit_rate_given_surviving_breakout"] for r in results])
    fail = avg([r["tr_breakout_mm"]["breakout_fail_rate"] for r in results])
    nest3 = avg([r["nesting"]["pct_legs_with_3or4_pushes"] for r in results])
    nest_rng = avg([r["nesting"]["pct_ranges_with_nested_3push_legs"] for r in results])
    persist = avg([r["markov"]["persistence_accuracy"] for r in results])
    markov_acc = avg([r["markov"]["markov_accuracy"] for r in results])
    maj = avg([r["markov"]["majority_null_accuracy"] for r in results])
    lag = avg([r["early_detection"].get("lag_median_bars") for r in results if r["early_detection"].get("n_changes")])
    ch_after = avg([r["pct_breakouts_followed_by_channelish"] for r in results])

    # spike pooled
    spike_counts = Counter()
    for r in results:
        spike_counts.update(r["spike_aftermath"]["counts"])
    st = sum(spike_counts.values()) or 1
    spike_dist = {k: v / st for k, v in spike_counts.items()}

    return {
        "n_datasets": len(results),
        "avg_mm_hit_given_breakout": mm_all,
        "avg_mm_hit_given_surviving_breakout": mm_surv,
        "avg_breakout_fail_rate": fail,
        "avg_pct_legs_3or4_push": nest3,
        "avg_pct_ranges_nested": nest_rng,
        "avg_persistence_accuracy": persist,
        "avg_markov_accuracy": markov_acc,
        "avg_majority_null_accuracy": maj,
        "avg_lag_median_bars": lag,
        "avg_breakout_then_channelish": ch_after,
        "pooled_spike_aftermath": spike_dist,
        "pooled_spike_counts": dict(spike_counts),
        "brooks_claims_vs_data": {
            "tr_breakout_fail_claimed_80pct": {"claim": 0.80, "observed_fail_rate": fail},
            "mm_after_valid_bo_claimed_60pct": {"claim": 0.60, "observed": mm_surv},
            "spike_to_channel_60": {"claim": 0.60, "observed": spike_dist.get("channel")},
            "spike_to_tr_30": {"claim": 0.30, "observed": spike_dist.get("trading_range")},
            "spike_to_reversal_10": {"claim": 0.10, "observed": spike_dist.get("reversal")},
        },
    }


def main() -> None:
    results = []
    for symbol, tf, fname in DATASETS:
        path = DATA_DIR / fname
        if not path.exists():
            print("MISSING", path)
            continue
        print(f"Analyzing {symbol} {tf} ...")
        r = analyze_one(symbol, tf, path)
        results.append(r)
        out_path = OUT_DIR / f"{symbol}_{tf}_result.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(
            f"  bars={r['bars']} ranges={r['nesting']['n_ranges']} "
            f"mm_surv={r['tr_breakout_mm']['mm_hit_rate_given_surviving_breakout']} "
            f"fail={r['tr_breakout_mm']['breakout_fail_rate']} "
            f"persist={r['markov']['persistence_accuracy']:.3f}"
        )

    summary = {
        "aggregate": aggregate(results),
        "per_dataset": results,
        "method_notes": {
            "swing": "fractal L/R=2 + ATR*1.15 zigzag filter",
            "pushes": "finer ATR*0.55 swings inside each macro leg",
            "regime": "efficiency/overlap/pullback/run-length on 40-bar window",
            "mm": "range height projection after close beyond boundary by 0.15 ATR",
            "failed_breakout": "reclaim inside range within 8 bars",
            "caveat": "labels are algorithmic proxies of Brooks language, not human PA reading",
        },
    }
    with open(OUT_DIR / "SUMMARY.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("Wrote", OUT_DIR / "SUMMARY.json")


if __name__ == "__main__":
    main()
