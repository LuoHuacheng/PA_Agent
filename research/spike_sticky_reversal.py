#!/usr/bin/env python3
"""Sticky reversal: origin-break that STILL holds later — closer to Brooks 'true reversal'."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np

from pa_cycle_nesting_experiment import DATA_DIR, MAX_BARS, atr, load_ohlc
from spike_aftermath_audit_fast import SPIKE_DEFS, aftermath, bucket, feat, find_spikes

OUT = Path(__file__).resolve().parent / "out"

DATASETS = [
    ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
    ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
    ("XAUUSD", "D1", "XAUUSD_D1.parquet"),
    ("US100", "H1", "US100.cash_H1.parquet"),
    ("US500", "H1", "US500.cash_H1.parquet"),
]


def sticky_classify(o, h, l, c, atr_arr, ev):
    """Multi-horizon classification for one spike."""
    # early labels
    lab20 = aftermath(o, h, l, c, atr_arr, ev, 20, "strict_brooks")
    lab30 = aftermath(o, h, l, c, atr_arr, ev, 30, "strict_brooks")
    lab40 = aftermath(o, h, l, c, atr_arr, ev, 40, "strict_brooks")

    end = ev["end"]
    d = ev["dir"]
    origin = ev["origin"]
    move = max(ev["move"], 1e-12)
    if end + 40 >= len(c):
        return None

    # sticky reversal: strict reversal at h20 AND still wrong side / beyond origin at h40
    if d > 0:
        beyond40 = float(c[end + 40]) < origin
        deep40 = float(l[end + 1 : end + 41].min()) < origin - 0.25 * move
    else:
        beyond40 = float(c[end + 40]) > origin
        deep40 = float(h[end + 1 : end + 41].max()) > origin + 0.25 * move

    sticky_rev = bucket(lab20) == "reversal" and beyond40 and deep40

    # failed reversal / bull|bear flag: hit origin early but recover with-trend by h40
    early_rev = bucket(lab20) == "reversal" or bucket(lab30) == "reversal"
    recovered = (float(c[end + 40]) - float(c[end])) * d > 0.15 * move
    failed_rev_then_channel = early_rev and recovered and not sticky_rev

    # final bucket preference
    if sticky_rev:
        final = "reversal_sticky"
    elif failed_rev_then_channel:
        final = "failed_reversal_to_channel"
    else:
        final = bucket(lab30)
        if final == "reversal":
            # late-only wipe that didn't stick
            if recovered:
                final = "failed_reversal_to_channel"
            else:
                final = "reversal_sticky" if beyond40 else "trading_range"

    return {
        "lab20": bucket(lab20),
        "lab30": bucket(lab30),
        "lab40": bucket(lab40),
        "final": final,
        "sticky_rev": sticky_rev,
        "failed_rev_flag": failed_rev_then_channel,
    }


def main():
    pool_by_spike = {name: Counter() for name in ("standard", "strict", "very_strict")}
    pool_raw30 = {name: Counter() for name in ("standard", "strict", "very_strict")}
    per = []

    for sym, tf, fn in DATASETS:
        print(f"{sym} {tf}", flush=True)
        df = load_ohlc(DATA_DIR / fn, MAX_BARS.get(tf, 40000))
        atr_arr = atr(df)
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        row = {"symbol": sym, "timeframe": tf, "defs": {}}

        for sname in ("standard", "strict", "very_strict"):
            events = find_spikes(o, h, l, c, atr_arr, *SPIKE_DEFS[sname])
            ctr = Counter()
            raw30 = Counter()
            for ev in events:
                if ev["end"] + 40 >= len(c):
                    continue
                # raw 30
                lab30 = bucket(aftermath(o, h, l, c, atr_arr, ev, 30, "strict_brooks"))
                raw30[lab30] += 1
                res = sticky_classify(o, h, l, c, atr_arr, ev)
                if res is None:
                    continue
                ctr[res["final"]] += 1
            pool_by_spike[sname].update(ctr)
            pool_raw30[sname].update(raw30)
            total = sum(ctr.values()) or 1
            dist = {k: ctr.get(k, 0) / total for k in ctr}
            row["defs"][sname] = {"n": total, "distribution": dist, "counts": dict(ctr), "raw30": dict(raw30)}
            print(
                f"  {sname}: n={total} sticky_rev={dist.get('reversal_sticky',0):.1%} "
                f"failRev->ch={dist.get('failed_reversal_to_channel',0):.1%} "
                f"ch={dist.get('channel',0):.1%} tr={dist.get('trading_range',0):.1%}",
                flush=True,
            )
        per.append(row)

    print("\n=== POOLED sticky taxonomy ===", flush=True)
    pooled = {}
    for sname, ctr in pool_by_spike.items():
        total = sum(ctr.values()) or 1
        dist = {k: ctr.get(k, 0) / total for k in sorted(ctr)}
        # map to Brooks 3-bucket
        brooks = {
            "channel": dist.get("channel", 0) + dist.get("failed_reversal_to_channel", 0) + dist.get("continued_spike", 0),
            "trading_range": dist.get("trading_range", 0),
            "reversal": dist.get("reversal_sticky", 0) + dist.get("reversal", 0),
        }
        raw = pool_raw30[sname]
        rt = sum(raw.values()) or 1
        pooled[sname] = {
            "n": total,
            "sticky_distribution": dist,
            "brooks_mapped": brooks,
            "raw_strict30": {k: raw.get(k, 0) / rt for k in ("channel", "trading_range", "reversal", "other")},
            "counts": dict(ctr),
        }
        print(
            f"{sname}: n={total} mapped ch={brooks['channel']:.1%} tr={brooks['trading_range']:.1%} "
            f"rev={brooks['reversal']:.1%} | raw30 rev={pooled[sname]['raw_strict30']['reversal']:.1%}",
            flush=True,
        )

    out = {
        "note": "sticky reversal = strict origin-break by ~20 bars AND still beyond origin at bar+40",
        "brooks_claim": {"channel": 0.60, "trading_range": 0.30, "reversal": 0.10},
        "pooled": pooled,
        "per": per,
        "interpretation": {
            "prior_37pct": "matched loose spike + loose_55 net-against; artifactual",
            "strict_30bar_origin_break": "still ~25-32% — many are temporary wipeouts",
            "sticky_reversal": "closer to 'actual reversal'; compare to Brooks 10%",
            "failed_reversal_to_channel": "Brooks teaching: most reversal attempts become flags",
        },
    }
    (OUT / "SPIKE_STICKY_REVERSAL.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT / "SPIKE_STICKY_REVERSAL.json", flush=True)


if __name__ == "__main__":
    main()
