#!/usr/bin/env python3
"""Compare TR breakout failure rates under multiple attempt definitions."""
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


def analyze_breakout_defs(df, ranges, atr_arr):
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    keys = ["A_close015", "B_close0", "C_wick", "D_wick_or_close"]
    stats = {
        k: {
            "attempts": 0,
            "fail8": 0,
            "fail20": 0,
            "mm_hit": 0,
            "sustained20": 0,
            "never_closes_outside": 0,
        }
        for k in keys
    }

    for r in ranges:
        b1 = r["bar1"]
        rh, rl, height = r["high"], r["low"], r["height"]
        if height <= 0:
            continue
        atr_m = float(atr_arr[min(b1, len(atr_arr) - 1)])
        search_end = min(len(df) - 1, b1 + max(30, int(3.0 * (r["bar1"] - r["bar0"] + 1))))

        first: dict[str, tuple[int, str]] = {}
        for i in range(b1 + 1, search_end + 1):
            if "A_close015" not in first and (c[i] > rh + 0.15 * atr_m or c[i] < rl - 0.15 * atr_m):
                first["A_close015"] = (i, "up" if c[i] > rh else "down")
            if "B_close0" not in first and (c[i] > rh or c[i] < rl):
                first["B_close0"] = (i, "up" if c[i] > rh else "down")
            if "C_wick" not in first and (h[i] > rh or l[i] < rl):
                if h[i] > rh and l[i] < rl:
                    d = "up" if c[i] >= (rh + rl) / 2 else "down"
                elif h[i] > rh:
                    d = "up"
                else:
                    d = "down"
                first["C_wick"] = (i, d)
            if "D_wick_or_close" not in first and (h[i] > rh or l[i] < rl):
                if h[i] > rh and not (l[i] < rl):
                    d = "up"
                elif l[i] < rl and not (h[i] > rh):
                    d = "down"
                else:
                    d = "up" if c[i] >= (rh + rl) / 2 else "down"
                first["D_wick_or_close"] = (i, d)
            if len(first) == 4:
                break

        for key, (i, d) in first.items():
            stats[key]["attempts"] += 1

            # For wick attempts: did it ever even close outside?
            if key in ("C_wick", "D_wick_or_close"):
                closed_out = False
                for j in range(i, min(len(df), i + 21)):
                    if d == "up" and c[j] > rh:
                        closed_out = True
                        break
                    if d == "down" and c[j] < rl:
                        closed_out = True
                        break
                if not closed_out:
                    stats[key]["never_closes_outside"] += 1

            fail8 = fail20 = False
            for j in range(i + 1, min(len(df), i + 9)):
                if rl <= c[j] <= rh:
                    fail8 = True
                    break
            for j in range(i + 1, min(len(df), i + 21)):
                if rl <= c[j] <= rh:
                    fail20 = True
                    break
            if fail8:
                stats[key]["fail8"] += 1
            if fail20:
                stats[key]["fail20"] += 1

            if d == "up":
                mm = rh + height
                hit = any(h[j] >= mm for j in range(i + 1, search_end + 1))
                j20 = min(len(df) - 1, i + 20)
                sust = c[j20] > rh
            else:
                mm = rl - height
                hit = any(l[j] <= mm for j in range(i + 1, search_end + 1))
                j20 = min(len(df) - 1, i + 20)
                sust = c[j20] < rl
            if hit:
                stats[key]["mm_hit"] += 1
            if sust:
                stats[key]["sustained20"] += 1

    out = {}
    for k, s in stats.items():
        a = max(1, s["attempts"])
        out[k] = {
            "attempts": s["attempts"],
            "fail8_rate": s["fail8"] / a,
            "fail20_rate": s["fail20"] / a,
            "mm_hit_rate": s["mm_hit"] / a,
            "fail_not_reaching_mm": 1 - s["mm_hit"] / a,
            "sustained20_rate": s["sustained20"] / a,
            "never_closes_outside_rate": s["never_closes_outside"] / a
            if k in ("C_wick", "D_wick_or_close")
            else None,
            "raw": s,
        }
    return out


def main():
    datasets = [
        ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
        ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
        ("US100", "H1", "US100.cash_H1.parquet"),
        ("US500", "H1", "US500.cash_H1.parquet"),
    ]
    all_res = {}
    pool_raw = {
        k: {"attempts": 0, "fail8": 0, "fail20": 0, "mm_hit": 0, "sustained20": 0, "never_closes_outside": 0}
        for k in ["A_close015", "B_close0", "C_wick", "D_wick_or_close"]
    }

    for sym, tf, fn in datasets:
        df = load_ohlc(DATA_DIR / fn, MAX_BARS.get(tf, 40000))
        atr_arr = atr(df)
        swings = find_swings(df, atr_arr, atr_mult=1.15)
        legs = legs_from_swings(swings)
        ranges = detect_ranges(legs, atr_arr)
        res = analyze_breakout_defs(df, ranges, atr_arr)
        all_res[f"{sym}_{tf}"] = {k: {kk: vv for kk, vv in v.items() if kk != "raw"} for k, v in res.items()}
        print(f"\n=== {sym} {tf} ranges={len(ranges)} ===")
        for k, v in res.items():
            print(
                f"{k}: n={v['attempts']} fail8={v['fail8_rate']:.1%} fail20={v['fail20_rate']:.1%} "
                f"mm={v['mm_hit_rate']:.1%} not_mm={v['fail_not_reaching_mm']:.1%} "
                f"sust20={v['sustained20_rate']:.1%} never_close_out={v['never_closes_outside_rate']}"
            )
            raw = v["raw"]
            for kk in pool_raw[k]:
                pool_raw[k][kk] += raw[kk]

    print("\n=== POOLED ===")
    pooled = {}
    for k, s in pool_raw.items():
        a = max(1, s["attempts"])
        pooled[k] = {
            "attempts": s["attempts"],
            "fail8_rate": s["fail8"] / a,
            "fail20_rate": s["fail20"] / a,
            "mm_hit_rate": s["mm_hit"] / a,
            "fail_not_reaching_mm": 1 - s["mm_hit"] / a,
            "sustained20_rate": s["sustained20"] / a,
            "never_closes_outside_rate": (
                s["never_closes_outside"] / a if k in ("C_wick", "D_wick_or_close") else None
            ),
        }
        p = pooled[k]
        print(
            f"{k}: n={p['attempts']} fail8={p['fail8_rate']:.1%} fail20={p['fail20_rate']:.1%} "
            f"mm={p['mm_hit_rate']:.1%} not_mm={p['fail_not_reaching_mm']:.1%} sust20={p['sustained20_rate']:.1%}"
        )

    OUT.mkdir(exist_ok=True)
    (OUT / "BREAKOUT_DEF_COMPARE.json").write_text(
        json.dumps({"pooled": pooled, "per": all_res}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("wrote", OUT / "BREAKOUT_DEF_COMPARE.json")


if __name__ == "__main__":
    main()
