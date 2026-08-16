#!/usr/bin/env python3
"""Count ALL boundary probes DURING range life — Brooks-like attempt universe."""
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


def probes_during_and_after(df, ranges, atr_arr):
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)

    # during: wick beyond high/low between bar0+5 and bar1 (range still "alive")
    # after: first wick/close beyond after bar1
    during = {"probes": 0, "fail8": 0, "mm_same_dir": 0}
    after_wick = {"probes": 0, "fail8": 0, "fail20": 0, "mm": 0, "mm_and_no_fail8": 0}
    after_close015 = {"probes": 0, "fail8": 0, "fail20": 0, "mm": 0, "mm_and_no_fail8": 0}

    for r in ranges:
        b0, b1 = r["bar0"], r["bar1"]
        rh, rl, height = r["high"], r["low"], r["height"]
        if height <= 0 or b1 - b0 < 8:
            continue
        atr_m = float(atr_arr[min(b1, len(atr_arr) - 1)])
        # Use almost-final envelope: for during-probes, use running approx of eventual bounds
        # (same rh/rl — slight look-ahead; acceptable for definition study)
        last_probe_i = -999
        for i in range(b0 + 3, b1 + 1):
            pierced_up = h[i] > rh
            pierced_dn = l[i] < rl
            if not pierced_up and not pierced_dn:
                continue
            # debounce: ignore probes within 3 bars of previous
            if i - last_probe_i < 3:
                continue
            last_probe_i = i
            d = "up" if pierced_up and not pierced_dn else ("down" if pierced_dn and not pierced_up else ("up" if c[i] >= (rh + rl) / 2 else "down"))
            during["probes"] += 1
            # fail: within 8 bars close back inside
            failed = False
            for j in range(i + 1, min(b1 + 1, i + 9)):
                if rl <= c[j] <= rh:
                    failed = True
                    break
            # also fail if never leaves on close
            if d == "up" and c[i] <= rh:
                # wick-only probe — count as immediate fail-type unless closes out soon
                closed = any(c[j] > rh for j in range(i, min(b1 + 1, i + 4)))
                if not closed:
                    failed = True
            if d == "down" and c[i] >= rl:
                closed = any(c[j] < rl for j in range(i, min(b1 + 1, i + 4)))
                if not closed:
                    failed = True
            if failed:
                during["fail8"] += 1
            else:
                # rare mid-range successful expansion — check mm inside remaining window
                if d == "up" and any(h[j] >= rh + height for j in range(i + 1, b1 + 1)):
                    during["mm_same_dir"] += 1
                if d == "down" and any(l[j] <= rl - height for j in range(i + 1, b1 + 1)):
                    during["mm_same_dir"] += 1

        # after-range first attempts
        search_end = min(len(df) - 1, b1 + max(30, int(3.0 * (b1 - b0 + 1))))

        def eval_after(i, d, bucket):
            bucket["probes"] += 1
            fail8 = any(rl <= c[j] <= rh for j in range(i + 1, min(len(df), i + 9)))
            fail20 = any(rl <= c[j] <= rh for j in range(i + 1, min(len(df), i + 21)))
            if fail8:
                bucket["fail8"] += 1
            if fail20:
                bucket["fail20"] += 1
            if d == "up":
                hit = any(h[j] >= rh + height for j in range(i + 1, search_end + 1))
            else:
                hit = any(l[j] <= rl - height for j in range(i + 1, search_end + 1))
            if hit:
                bucket["mm"] += 1
                if not fail8:
                    bucket["mm_and_no_fail8"] += 1

        # first wick after
        for i in range(b1 + 1, search_end + 1):
            if h[i] > rh or l[i] < rl:
                if h[i] > rh and not (l[i] < rl):
                    d = "up"
                elif l[i] < rl and not (h[i] > rh):
                    d = "down"
                else:
                    d = "up" if c[i] >= (rh + rl) / 2 else "down"
                eval_after(i, d, after_wick)
                break

        # first close+0.15ATR after
        for i in range(b1 + 1, search_end + 1):
            if c[i] > rh + 0.15 * atr_m or c[i] < rl - 0.15 * atr_m:
                d = "up" if c[i] > rh else "down"
                eval_after(i, d, after_close015)
                break

    def pack(s, with20=False):
        a = max(1, s["probes"])
        out = {
            "n": s["probes"],
            "fail8": s["fail8"] / a,
            "success_complement_fail8": 1 - s["fail8"] / a,
        }
        if "fail20" in s:
            out["fail20"] = s["fail20"] / a
        if "mm" in s:
            out["mm_any"] = s["mm"] / a
            out["mm_without_fail8"] = s["mm_and_no_fail8"] / a
            out["fail_if_success_means_clean_mm"] = 1 - s["mm_and_no_fail8"] / a
        if "mm_same_dir" in s:
            out["mm_rare"] = s["mm_same_dir"] / a
        return out

    return {
        "during_range_wick_probes": pack(during),
        "after_range_first_wick": pack(after_wick, True),
        "after_range_first_close015": pack(after_close015, True),
    }


def main():
    datasets = [
        ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
        ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
        ("US100", "H1", "US100.cash_H1.parquet"),
        ("US500", "H1", "US500.cash_H1.parquet"),
    ]
    pool = {
        "during": {"probes": 0, "fail8": 0, "mm_same_dir": 0},
        "after_wick": {"probes": 0, "fail8": 0, "fail20": 0, "mm": 0, "mm_and_no_fail8": 0},
        "after_close015": {"probes": 0, "fail8": 0, "fail20": 0, "mm": 0, "mm_and_no_fail8": 0},
    }
    per = {}
    for sym, tf, fn in datasets:
        df = load_ohlc(DATA_DIR / fn, MAX_BARS.get(tf, 40000))
        atr_arr = atr(df)
        swings = find_swings(df, atr_arr, atr_mult=1.15)
        legs = legs_from_swings(swings)
        ranges = detect_ranges(legs, atr_arr)
        # need raw accumulation — rewrite quick inline
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        local = {
            "during": {"probes": 0, "fail8": 0, "mm_same_dir": 0},
            "after_wick": {"probes": 0, "fail8": 0, "fail20": 0, "mm": 0, "mm_and_no_fail8": 0},
            "after_close015": {"probes": 0, "fail8": 0, "fail20": 0, "mm": 0, "mm_and_no_fail8": 0},
        }
        for r in ranges:
            b0, b1 = r["bar0"], r["bar1"]
            rh, rl, height = r["high"], r["low"], r["height"]
            if height <= 0 or b1 - b0 < 8:
                continue
            atr_m = float(atr_arr[min(b1, len(atr_arr) - 1)])
            last = -999
            for i in range(b0 + 3, b1 + 1):
                up, dn = h[i] > rh, l[i] < rl
                if not up and not dn:
                    continue
                if i - last < 3:
                    continue
                last = i
                d = "up" if up and not dn else ("down" if dn and not up else ("up" if c[i] >= (rh + rl) / 2 else "down"))
                local["during"]["probes"] += 1
                failed = any(rl <= c[j] <= rh for j in range(i + 1, min(b1 + 1, i + 9)))
                if d == "up" and c[i] <= rh and not any(c[j] > rh for j in range(i, min(b1 + 1, i + 4))):
                    failed = True
                if d == "down" and c[i] >= rl and not any(c[j] < rl for j in range(i, min(b1 + 1, i + 4))):
                    failed = True
                if failed:
                    local["during"]["fail8"] += 1
                elif d == "up" and any(h[j] >= rh + height for j in range(i + 1, b1 + 1)):
                    local["during"]["mm_same_dir"] += 1
                elif d == "down" and any(l[j] <= rl - height for j in range(i + 1, b1 + 1)):
                    local["during"]["mm_same_dir"] += 1

            search_end = min(len(df) - 1, b1 + max(30, int(3.0 * (b1 - b0 + 1))))

            def do_after(start_cond, bucket):
                for i in range(b1 + 1, search_end + 1):
                    ok, d = start_cond(i)
                    if not ok:
                        continue
                    bucket["probes"] += 1
                    fail8 = any(rl <= c[j] <= rh for j in range(i + 1, min(len(df), i + 9)))
                    fail20 = any(rl <= c[j] <= rh for j in range(i + 1, min(len(df), i + 21)))
                    if fail8:
                        bucket["fail8"] += 1
                    if fail20:
                        bucket["fail20"] += 1
                    hit = (
                        any(h[j] >= rh + height for j in range(i + 1, search_end + 1))
                        if d == "up"
                        else any(l[j] <= rl - height for j in range(i + 1, search_end + 1))
                    )
                    if hit:
                        bucket["mm"] += 1
                        if not fail8:
                            bucket["mm_and_no_fail8"] += 1
                    break

            do_after(
                lambda i: (
                    (True, "up" if h[i] > rh and not (l[i] < rl) else ("down" if l[i] < rl and not (h[i] > rh) else ("up" if c[i] >= (rh + rl) / 2 else "down")))
                    if (h[i] > rh or l[i] < rl)
                    else (False, "")
                ),
                local["after_wick"],
            )
            do_after(
                lambda i: (
                    (True, "up" if c[i] > rh else "down")
                    if (c[i] > rh + 0.15 * atr_m or c[i] < rl - 0.15 * atr_m)
                    else (False, "")
                ),
                local["after_close015"],
            )

        for k in pool:
            for kk in pool[k]:
                pool[k][kk] += local[k][kk]

        def fmt(s, kind):
            a = max(1, s["probes"])
            if kind == "during":
                return {
                    "n": s["probes"],
                    "fail8": s["fail8"] / a,
                    "mm_rare": s["mm_same_dir"] / a,
                }
            return {
                "n": s["probes"],
                "fail8": s["fail8"] / a,
                "fail20": s["fail20"] / a,
                "mm_any": s["mm"] / a,
                "clean_mm": s["mm_and_no_fail8"] / a,
                "fail_vs_clean_mm": 1 - s["mm_and_no_fail8"] / a,
            }

        per[f"{sym}_{tf}"] = {k: fmt(local[k], k if k == "during" else "after") for k in local}
        print(f"\n=== {sym} {tf} ===")
        for k, v in per[f"{sym}_{tf}"].items():
            print(k, {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()})

    pooled = {}
    for k, s in pool.items():
        a = max(1, s["probes"])
        if k == "during":
            pooled[k] = {"n": s["probes"], "fail8": s["fail8"] / a, "mm_rare": s["mm_same_dir"] / a}
        else:
            pooled[k] = {
                "n": s["probes"],
                "fail8": s["fail8"] / a,
                "fail20": s["fail20"] / a,
                "mm_any": s["mm"] / a,
                "clean_mm": s["mm_and_no_fail8"] / a,
                "fail_vs_clean_mm": 1 - s["mm_and_no_fail8"] / a,
            }
    print("\n=== POOLED ===")
    for k, v in pooled.items():
        print(k, {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()})

    (OUT / "BREAKOUT_DURING_VS_AFTER.json").write_text(
        json.dumps({"pooled": pooled, "per": per}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
