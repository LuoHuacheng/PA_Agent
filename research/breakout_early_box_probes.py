#!/usr/bin/env python3
"""Brooks-like TR breakout attempts: probes of an EARLY-established box during range life.

Key fix vs prior study: do NOT only score the post-completion breakout
(that one often ends the range → success looks too high).
"""
from __future__ import annotations

import json
from pathlib import Path

from pa_cycle_nesting_experiment import (
    DATA_DIR,
    MAX_BARS,
    atr,
    detect_ranges,
    find_swings,
    legs_from_swings,
    load_ohlc,
)

OUT = Path(__file__).resolve().parent / "out"


def main():
    datasets = [
        ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
        ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
        ("US100", "H1", "US100.cash_H1.parquet"),
        ("US500", "H1", "US500.cash_H1.parquet"),
    ]

    # buckets
    # early_box_probes: after box fixed from first 40% bars, count wick/close probes until range end
    # terminal_close015: first close+0.15ATR beyond FINAL envelope after range end (old study)
    pool = {
        "early_wick_probe": {"n": 0, "fail_back8": 0},
        "early_close_probe": {"n": 0, "fail_back8": 0},
        "terminal_close015": {"n": 0, "fail_back8": 0, "fail_back20": 0, "mm": 0, "clean_mm": 0},
        "terminal_wick": {"n": 0, "fail_back8": 0, "fail_back20": 0, "mm": 0, "clean_mm": 0},
    }
    per = {}

    for sym, tf, fn in datasets:
        df = load_ohlc(DATA_DIR / fn, MAX_BARS.get(tf, 40000))
        atr_arr = atr(df)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        swings = find_swings(df, atr_arr, atr_mult=1.15)
        legs = legs_from_swings(swings)
        ranges = detect_ranges(legs, atr_arr)

        local = {k: {kk: 0 for kk in v} for k, v in pool.items()}

        for r in ranges:
            b0, b1 = r["bar0"], r["bar1"]
            if b1 - b0 < 12:
                continue
            # Early box from first 40% of range bars (established TR)
            cut = b0 + max(5, int(0.4 * (b1 - b0)))
            box_h = float(h[b0 : cut + 1].max())
            box_l = float(l[b0 : cut + 1].min())
            height = box_h - box_l
            if height <= 0:
                continue
            atr_m = float(atr_arr[cut])

            # Probes during remainder of range life (cut+1 .. b1)
            last_w = last_c = -999
            for i in range(cut + 1, b1 + 1):
                # wick probe
                if (h[i] > box_h or l[i] < box_l) and i - last_w >= 3:
                    last_w = i
                    local["early_wick_probe"]["n"] += 1
                    d_up = h[i] > box_h and not (l[i] < box_l)
                    d_dn = l[i] < box_l and not (h[i] > box_h)
                    # fail = back inside within 8 bars (or never close outside)
                    failed = False
                    if d_up or (h[i] > box_h and c[i] >= (box_h + box_l) / 2):
                        if c[i] <= box_h and not any(c[j] > box_h for j in range(i, min(b1 + 1, i + 3))):
                            failed = True
                        if any(c[j] <= box_h and c[j] >= box_l for j in range(i + 1, min(b1 + 1, i + 9))):
                            # back in box
                            if not any(c[j] > box_h for j in range(i + 1, min(b1 + 1, i + 9))):
                                failed = True
                            # if came back into box at all after pierce
                            if any(box_l <= c[j] <= box_h for j in range(i + 1, min(b1 + 1, i + 9))):
                                failed = True
                    else:
                        if c[i] >= box_l and not any(c[j] < box_l for j in range(i, min(b1 + 1, i + 3))):
                            failed = True
                        if any(box_l <= c[j] <= box_h for j in range(i + 1, min(b1 + 1, i + 9))):
                            failed = True
                    # Simpler unified fail: within 8 bars, close is back inside box
                    failed = any(box_l <= c[j] <= box_h for j in range(i + 1, min(len(df), i + 9)))
                    # wick-only with no outside close in 3 bars also fail
                    if h[i] > box_h and not any(c[j] > box_h for j in range(i, min(len(df), i + 4))):
                        failed = True
                    if l[i] < box_l and not any(c[j] < box_l for j in range(i, min(len(df), i + 4))):
                        failed = True
                    if failed:
                        local["early_wick_probe"]["fail_back8"] += 1

                # close probe
                if (c[i] > box_h or c[i] < box_l) and i - last_c >= 3:
                    last_c = i
                    local["early_close_probe"]["n"] += 1
                    failed = any(box_l <= c[j] <= box_h for j in range(i + 1, min(len(df), i + 9)))
                    if failed:
                        local["early_close_probe"]["fail_back8"] += 1

            # Terminal breakout vs FINAL envelope (old definition universe)
            rh, rl = r["high"], r["low"]
            fheight = rh - rl
            search_end = min(len(df) - 1, b1 + max(30, int(3.0 * (b1 - b0 + 1))))

            def terminal(pred, bucket):
                for i in range(b1 + 1, search_end + 1):
                    ok, d = pred(i)
                    if not ok:
                        continue
                    bucket["n"] += 1
                    fail8 = any(rl <= c[j] <= rh for j in range(i + 1, min(len(df), i + 9)))
                    fail20 = any(rl <= c[j] <= rh for j in range(i + 1, min(len(df), i + 21)))
                    if fail8:
                        bucket["fail_back8"] += 1
                    if fail20:
                        bucket["fail_back20"] += 1
                    hit = (
                        any(h[j] >= rh + fheight for j in range(i + 1, search_end + 1))
                        if d == "up"
                        else any(l[j] <= rl - fheight for j in range(i + 1, search_end + 1))
                    )
                    if hit:
                        bucket["mm"] += 1
                        if not fail8:
                            bucket["clean_mm"] += 1
                    break

            terminal(
                lambda i: (
                    (True, "up" if c[i] > rh else "down")
                    if (c[i] > rh + 0.15 * atr_arr[i] or c[i] < rl - 0.15 * atr_arr[i])
                    else (False, "")
                ),
                local["terminal_close015"],
            )
            terminal(
                lambda i: (
                    (
                        True,
                        "up"
                        if h[i] > rh and not (l[i] < rl)
                        else (
                            "down"
                            if l[i] < rl and not (h[i] > rh)
                            else ("up" if c[i] >= (rh + rl) / 2 else "down")
                        ),
                    )
                    if (h[i] > rh or l[i] < rl)
                    else (False, "")
                ),
                local["terminal_wick"],
            )

        def summarize(s):
            a = max(1, s["n"])
            out = {"n": s["n"], "fail8": s["fail_back8"] / a}
            if "fail_back20" in s:
                out["fail20"] = s["fail_back20"] / a
                out["mm_any"] = s["mm"] / a
                out["clean_mm"] = s["clean_mm"] / a
                out["fail_vs_clean_mm"] = 1 - s["clean_mm"] / a
            return out

        per[f"{sym}_{tf}"] = {k: summarize(v) for k, v in local.items()}
        print(f"\n=== {sym} {tf} ===")
        for k, v in per[f"{sym}_{tf}"].items():
            print(k, {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in v.items()})

        for k in pool:
            for kk in pool[k]:
                pool[k][kk] += local[k][kk]

    pooled = {}
    for k, s in pool.items():
        a = max(1, s["n"])
        pooled[k] = {"n": s["n"], "fail8": s["fail_back8"] / a}
        if "fail_back20" in s:
            pooled[k]["fail20"] = s["fail_back20"] / a
            pooled[k]["mm_any"] = s["mm"] / a
            pooled[k]["clean_mm"] = s["clean_mm"] / a
            pooled[k]["fail_vs_clean_mm"] = 1 - s["clean_mm"] / a

    print("\n=== POOLED ===")
    for k, v in pooled.items():
        print(k, {kk: round(vv, 3) if isinstance(vv, float) else vv for kk, vv in v.items()})

    (OUT / "BREAKOUT_EARLY_BOX.json").write_text(
        json.dumps({"pooled": pooled, "per": per}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
