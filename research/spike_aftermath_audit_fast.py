#!/usr/bin/env python3
"""Fast strict audit: spike aftermath vs Brooks 60/30/10."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from pa_cycle_nesting_experiment import DATA_DIR, MAX_BARS, atr, load_ohlc

OUT = Path(__file__).resolve().parent / "out"


def feat(o, h, l, c, atr_m):
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    d = 1 if c > o else (-1 if c < o else 0)
    return body, rng, d, body / max(atr_m, 1e-12)


def find_spikes(o, h, l, c, atr_arr, min_run, min_body, max_ov, require_ext, cooldown):
    n = len(c)
    events = []
    i = 30
    while i < n - 5:
        body, rng, d, ba = feat(o[i], h[i], l[i], c[i], atr_arr[i])
        if d == 0 or ba < min_body:
            i += 1
            continue
        run = [i]
        j = i + 1
        while j < n and len(run) < 10:
            bj, rj, dj, baj = feat(o[j], h[j], l[j], c[j], atr_arr[j])
            if dj != d or baj < min_body * 0.75:
                break
            # overlap with prev
            pi = run[-1]
            ov = max(0.0, min(h[pi], h[j]) - max(l[pi], l[j]))
            w = max(h[pi], h[j]) - min(l[pi], l[j])
            if w > 0 and ov / w > max_ov + 0.15:
                break
            if require_ext:
                if d > 0 and h[j] < h[pi] and c[j] < c[pi]:
                    break
                if d < 0 and l[j] > l[pi] and c[j] > c[pi]:
                    break
            run.append(j)
            j += 1
        if len(run) >= min_run:
            ovs = []
            for a, b in zip(run, run[1:]):
                ov = max(0.0, min(h[a], h[b]) - max(l[a], l[b]))
                w = max(h[a], h[b]) - min(l[a], l[b])
                ovs.append(ov / w if w else 1.0)
            if (float(np.mean(ovs)) if ovs else 1.0) <= max_ov:
                start, end = run[0], run[-1]
                # onset: previous not already strong same dir
                if start > 0:
                    _, _, pd, pba = feat(o[start - 1], h[start - 1], l[start - 1], c[start - 1], atr_arr[start - 1])
                    if pd == d and pba >= min_body:
                        i = end + 1
                        continue
                events.append(
                    {
                        "start": start,
                        "end": end,
                        "dir": d,
                        "n": len(run),
                        "move": abs(c[end] - c[start]),
                        "ext_h": float(h[run].max()) if hasattr(h[run], "max") else float(np.max(h[run])),
                        "ext_l": float(np.min(l[run])),
                        "origin": float(l[start]) if d > 0 else float(h[start]),
                    }
                )
                i = end + cooldown
                continue
        i += 1
    return events


def aftermath(o, h, l, c, atr_arr, ev, horizon, mode):
    end = ev["end"]
    fut = min(len(c) - 1, end + horizon)
    if fut <= end + 3:
        return "other"
    d = ev["dir"]
    move = max(ev["move"], 1e-12)
    origin = ev["origin"]
    ce = c[end]

    fut_h = h[end + 1 : fut + 1]
    fut_l = l[end + 1 : fut + 1]
    if d > 0:
        mae = max(0.0, ce - float(fut_l.min()))
        mfe = max(0.0, float(fut_h.max()) - ce)
        origin_break = any(c[j] < origin for j in range(end + 1, fut + 1))
    else:
        mae = max(0.0, float(fut_h.max()) - ce)
        mfe = max(0.0, ce - float(fut_l.min()))
        origin_break = any(c[j] > origin for j in range(end + 1, fut + 1))
    mae_r = mae / move
    mfe_r = mfe / move
    net = float(c[fut] - ce)
    net_with = net * d

    # continued spike
    cont = 0
    for j in range(end + 1, min(len(c), end + 6)):
        _, _, dj, baj = feat(o[j], h[j], l[j], c[j], atr_arr[j])
        if dj == d and baj >= 0.45:
            cont += 1
        else:
            break
    if cont >= 2 and mae_r < 0.35:
        return "continued_spike"

    is_rev = False
    if mode == "loose_55":
        is_rev = net_with < 0 and abs(net) > 0.55 * move
    elif mode == "mae_70":
        is_rev = mae_r >= 0.70 and net_with < 0
    elif mode == "strict_brooks":
        opp = 0
        opp_ok = False
        for j in range(end + 1, fut + 1):
            _, _, dj, baj = feat(o[j], h[j], l[j], c[j], atr_arr[j])
            if dj == -d and baj >= 0.5:
                opp += 1
                if opp >= 2 and origin_break:
                    opp_ok = True
                    break
            else:
                if opp < 2:
                    opp = 0
        is_rev = (mae_r >= 1.0 and origin_break) or opp_ok
    elif mode == "strict_hold":
        is_rev = mae_r >= 1.0 and origin_break and net_with < 0 and abs(net) > 0.25 * move
    else:
        raise ValueError(mode)

    if is_rev:
        return "reversal"

    # TR vs channel
    overlaps = []
    for j in range(end + 1, fut + 1):
        ov = max(0.0, min(h[j - 1], h[j]) - max(l[j - 1], l[j]))
        w = max(h[j - 1], h[j]) - min(l[j - 1], l[j])
        overlaps.append(ov / w if w else 1.0)
    ov = float(np.mean(overlaps))
    path = float(np.sum(h[end + 1 : fut + 1] - l[end + 1 : fut + 1]))
    eff = abs(net) / path if path > 0 else 0.0
    width_atr = float(fut_h.max() - fut_l.min()) / max(float(atr_arr[end]), 1e-12)

    if (0.15 <= mae_r <= 0.85 and (net_with > 0 or mfe_r >= 0.35)) and eff >= 0.12:
        return "channel"
    if ov > 0.55 or (eff < 0.12 and width_atr < 4.5):
        return "trading_range"
    if net_with > 0 or mfe_r >= 0.5:
        return "channel"
    if mae_r >= 0.35 and mfe_r < 0.25:
        return "trading_range"
    return "channel"


def bucket(lab):
    if lab == "continued_spike":
        return "channel"
    return lab if lab in ("channel", "trading_range", "reversal") else "other"


SPIKE_DEFS = {
    "loose": (2, 0.35, 0.55, False, 15),
    "standard": (3, 0.55, 0.35, True, 25),
    "strict": (3, 0.70, 0.25, True, 30),
    "very_strict": (4, 0.80, 0.22, True, 35),
}
REV_MODES = ["loose_55", "mae_70", "strict_brooks", "strict_hold"]
HORIZONS = [20, 30, 40]

DATASETS = [
    ("XAUUSD", "H1", "XAUUSD_H1.parquet"),
    ("XAUUSD", "M15", "XAUUSD_M15.parquet"),
    ("XAUUSD", "D1", "XAUUSD_D1.parquet"),
    ("US100", "H1", "US100.cash_H1.parquet"),
    ("US100", "D1", "US100.cash_D1.parquet"),
    ("US500", "H1", "US500.cash_H1.parquet"),
    ("US500", "D1", "US500.cash_D1.parquet"),
]


def main():
    pool = {}
    per = []
    for sym, tf, fn in DATASETS:
        print(f"Loading {sym} {tf} ...", flush=True)
        df = load_ohlc(DATA_DIR / fn, MAX_BARS.get(tf, 40000))
        atr_arr = atr(df)
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)

        spikes_by_def = {}
        for name, args in SPIKE_DEFS.items():
            spikes_by_def[name] = find_spikes(o, h, l, c, atr_arr, *args)
            print(f"  spikes[{name}]={len(spikes_by_def[name])}", flush=True)

        ds = {"symbol": sym, "timeframe": tf, "combos": {}}
        for sname, events in spikes_by_def.items():
            for mode in REV_MODES:
                for hz in HORIZONS:
                    ctr = Counter()
                    raw = Counter()
                    for ev in events:
                        if ev["end"] + hz >= len(c):
                            continue
                        lab = aftermath(o, h, l, c, atr_arr, ev, hz, mode)
                        raw[lab] += 1
                        ctr[bucket(lab)] += 1
                    total = sum(ctr.values()) or 1
                    key = f"{sname}|{mode}|h{hz}"
                    res = {
                        "n": total,
                        "distribution": {
                            k: ctr.get(k, 0) / total for k in ("channel", "trading_range", "reversal", "other")
                        },
                        "raw": dict(raw),
                        "counts": dict(ctr),
                    }
                    ds["combos"][key] = res
                    pc = pool.setdefault(key, Counter())
                    for k, v in ctr.items():
                        pc[k] += v
        per.append(ds)
        # print primary
        k = "standard|strict_brooks|h30"
        d = ds["combos"][k]["distribution"]
        print(
            f"  PRIMARY std/strict/h30 n={ds['combos'][k]['n']} "
            f"ch={d['channel']:.1%} tr={d['trading_range']:.1%} rev={d['reversal']:.1%}",
            flush=True,
        )

    pooled = {}
    for key, ctr in pool.items():
        total = sum(ctr.values()) or 1
        pooled[key] = {
            "n": total,
            "distribution": {k: ctr.get(k, 0) / total for k in ("channel", "trading_range", "reversal", "other")},
            "counts": dict(ctr),
        }

    print("\n======== POOLED h30 ========", flush=True)
    print(f"{'spike':12} {'rev':14} {'n':>7} {'chan':>8} {'TR':>8} {'REV':>8}", flush=True)
    for sname in SPIKE_DEFS:
        for mode in REV_MODES:
            key = f"{sname}|{mode}|h30"
            p = pooled[key]
            d = p["distribution"]
            print(
                f"{sname:12} {mode:14} {p['n']:7d} {d['channel']:8.1%} {d['trading_range']:8.1%} {d['reversal']:8.1%}",
                flush=True,
            )

    focus_keys = [
        "loose|loose_55|h30",
        "standard|loose_55|h30",
        "standard|mae_70|h30",
        "standard|strict_brooks|h30",
        "standard|strict_hold|h30",
        "strict|strict_brooks|h30",
        "very_strict|strict_brooks|h30",
        "strict|strict_brooks|h20",
        "strict|strict_brooks|h40",
        "standard|strict_brooks|h20",
        "standard|strict_brooks|h40",
    ]
    focus = {k: pooled[k] for k in focus_keys}
    print("\n======== FOCUS ========", flush=True)
    for k, p in focus.items():
        d = p["distribution"]
        print(f"{k}: n={p['n']} ch={d['channel']:.1%} tr={d['trading_range']:.1%} rev={d['reversal']:.1%}", flush=True)

    # Per-dataset primary
    print("\n======== PER-DS primary standard|strict_brooks|h30 ========", flush=True)
    for ds in per:
        d = ds["combos"]["standard|strict_brooks|h30"]["distribution"]
        n = ds["combos"]["standard|strict_brooks|h30"]["n"]
        print(
            f"{ds['symbol']} {ds['timeframe']}: n={n} ch={d['channel']:.1%} tr={d['trading_range']:.1%} rev={d['reversal']:.1%}",
            flush=True,
        )

    out = {
        "brooks_claim": {"channel": 0.60, "trading_range": 0.30, "reversal": 0.10},
        "definitions": {
            "spike_loose": "2+ bars, body>=0.35ATR, overlap<=0.55 (old proxy)",
            "spike_standard": "3+ bars, body>=0.55ATR, overlap<=0.35, new extreme",
            "spike_strict": "3+ bars, body>=0.70ATR, overlap<=0.25",
            "rev_loose_55": "net against >55% spike size (OLD)",
            "rev_mae_70": "MAE>=70% and net against",
            "rev_strict_brooks": "MAE>=100% AND close beyond spike origin, OR opposite 2-bar spike through origin",
            "rev_strict_hold": "strict + still wrong-side at horizon",
        },
        "focus_pooled": focus,
        "all_pooled": pooled,
        "per_dataset_primary": [
            {
                "symbol": ds["symbol"],
                "timeframe": ds["timeframe"],
                **ds["combos"]["standard|strict_brooks|h30"],
            }
            for ds in per
        ],
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "SPIKE_AFTERMATH_AUDIT.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT / "SPIKE_AFTERMATH_AUDIT.json", flush=True)


if __name__ == "__main__":
    main()
