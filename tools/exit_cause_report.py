"""Why did each position actually end? Excursion-level exit attribution.

The duration report says the sub-30-minute bucket bleeds (-89.87 over 36
positions). It cannot say *why*, because a fill only records the exit price.
This tool replays every closed position against 1m candles and separates the
two competing explanations:

  * entry was wrong -> the position never showed a meaningful favourable
    excursion, and price kept going after the stop fired
  * stop was too tight -> price traded back at the entry price soon after the
    stop fired, without continuing against the position

Candles come from the TESTNET endpoint: fills are testnet fills, and mainnet
prices differ by ~0.02%, the same order as the stop distances being judged.

Usage:
    python tools/exit_cause_report.py --days 14
    python tools/exit_cause_report.py --days 14 --focus-min 30 --out logs/exit_cause.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import time
from pathlib import Path

from _pa_sim_common import (
    DEFAULT_SYMBOLS,
    fetch_account_data,
    fetch_klines,
    fmt_ms,
    load_decision_index,
    make_client,
    rebuild_trades,
    window_bounds,
)

MINUTE_MS = 60_000
#: How long after an exit to keep watching price, to judge the exit itself.
POST_WINDOW_MS = 4 * 3600 * 1000
#: |exit - stop| within this fraction of entry counts as a stop-out.
STOP_MATCH_TOL = 0.0008
#: Favourable excursion below this share of the planned stop distance means the
#: position never worked: the stop was not the problem, the entry was.
NEVER_WORKED_RATIO = 0.25
#: Stop distance below this multiple of the 1m noise scale sits inside candle
#: noise, so an ordinary bar can take it out.
TIGHT_STOP_RATIO = 1.0

BUCKETS = [
    ("<30m", 0, 30),
    ("30-60m", 30, 60),
    ("1-3h", 60, 180),
    ("3-6h", 180, 360),
    ("6-12h", 360, 720),
    ("12-24h", 720, 1440),
    (">24h", 1440, 10 ** 9),
]


def bucket_of(minutes: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= minutes < hi:
            return name
    return ">24h"


def net_of(pos: dict) -> float:
    return pos["realized"] + pos["fees"]


def slice_bars(bars: list[dict], times: list[int], start_ms: int, end_ms: int) -> list[dict]:
    return bars[bisect.bisect_left(times, start_ms):bisect.bisect_right(times, end_ms)]


def excursion(bars: list[dict], side: int, entry: float, opened_at: int, closed_at: int) -> dict:
    """Worst/best excursion from entry over the position's life, in percent.

    mae_pct is signed against the position (negative = adverse), mfe_pct is
    signed in favour of it. Entry is the quantity-weighted average of the fills
    that opened the position, so a scaled-in position measures from that average.
    """
    span = [b for b in bars if b["time"] + MINUTE_MS > opened_at and b["time"] <= closed_at]
    if not span or entry <= 0:
        return {"mae_pct": None, "mfe_pct": None, "t_mae_min": None, "t_mfe_min": None}
    if side > 0:
        adverse_px, favourable_px = min(b["low"] for b in span), max(b["high"] for b in span)
    else:
        adverse_px, favourable_px = max(b["high"] for b in span), min(b["low"] for b in span)
    edge = "low" if side > 0 else "high"
    t_mae = next(b["time"] for b in span if b[edge] == adverse_px)
    best_edge = "high" if side > 0 else "low"
    t_mfe = next(b["time"] for b in span if b[best_edge] == favourable_px)
    return {
        "mae_pct": (adverse_px - entry) / entry * 100 * side,
        "mfe_pct": (favourable_px - entry) / entry * 100 * side,
        "t_mae_min": (t_mae - opened_at) / MINUTE_MS,
        "t_mfe_min": (t_mfe - opened_at) / MINUTE_MS,
    }


def noise_scale(bars: list[dict], opened_at: int, *, lookback_min: int = 60) -> float | None:
    """Median 1m (high-low)/close percent over the hour before entry.

    A stop distance measured in these units answers 'could one ordinary candle
    have taken me out?' without needing a volatility model.
    """
    prior = [b for b in bars if opened_at - lookback_min * MINUTE_MS <= b["time"] < opened_at]
    ranges = [(b["high"] - b["low"]) / b["close"] * 100 for b in prior if b["close"] > 0]
    if len(ranges) < 20:
        return None
    return statistics.median(ranges)


def post_exit(bars: list[dict], side: int, entry: float, exit_px: float, closed_at: int) -> dict:
    """What price did after the exit, over POST_WINDOW_MS.

    post_adverse_pct is the worst further move against the (now flat) position:
    deeply negative means the exit saved money. recover_min is how long until
    price traded back at the entry price -- a short value means the stop was
    noise, a missing value means price never came back.
    """
    after = [b for b in bars if closed_at < b["time"] <= closed_at + POST_WINDOW_MS]
    if not after or exit_px <= 0:
        return {"post_adverse_pct": None, "post_best_pct": None, "recover_min": None}
    if side > 0:
        worst, best = min(b["low"] for b in after), max(b["high"] for b in after)
        hit = next((b for b in after if b["high"] >= entry), None)
        adverse = (worst - exit_px) / exit_px * 100
        bestp = (best - exit_px) / exit_px * 100
    else:
        worst, best = max(b["high"] for b in after), min(b["low"] for b in after)
        hit = next((b for b in after if b["low"] <= entry), None)
        adverse = (exit_px - worst) / exit_px * 100
        bestp = (exit_px - best) / exit_px * 100
    return {
        "post_adverse_pct": adverse,
        "post_best_pct": bestp,
        "recover_min": ((hit["time"] - closed_at) / MINUTE_MS) if hit else None,
    }


def exit_kind(pos: dict) -> str:
    """Classify the price that ended the position against the planned levels."""
    stop, target, px = pos.get("stop"), pos.get("target"), pos.get("close_px") or 0
    entry = pos["entry"] or 1
    if px <= 0:
        return "unknown"
    if stop and abs(px - stop) <= STOP_MATCH_TOL * entry:
        return "stop"
    if target and abs(px - target) <= STOP_MATCH_TOL * entry:
        return "tp"
    if stop:
        return "signal"
    return "no_stop"


#: clientOrderId prefix -> who closed the position. Only a pa-* id is
#: self-identifying (pa-entry / pa-sl / pa-tp / pa-rollback). A Binance-generated
#: x-* id is NOT evidence of an outside order: measured on this account, x-*
#: orders run 09-11..09-13 and pa-* orders start 09-13, so x-* is simply what an
#: earlier build of this bot produced before the id convention landed. An empty
#: id means the order is outside /fapi/v1/allOrders, which only lists ~7 days
#: (measured: 09-11 14:00 onward on a 14-day window), and consumed conditional
#: orders have no queryable history endpoint (algoOrders/historicalOrders 404 on
#: testnet). Nothing here is attributable before 09-13, so exit_kind -- which
#: compares the exit price against the planned stop/target -- is the only
#: order-record-independent classifier.
_CID_SOURCE = (
    ("pa-sl", "pa-sl(交易所止损)"),
    ("pa-tp", "pa-tp(交易所止盈)"),
    ("pa-rollback", "pa-rollback(主动平仓)"),
    ("pa-entry", "pa-entry(反手)"),
)


def close_source(pos: dict) -> str:
    """Who closed the position, per the closing order's clientOrderId."""
    exits = pos.get("exits") or []
    if not exits:
        return "unknown(无成交记录)"
    last = max(exits, key=lambda e: e.get("time") or 0)
    cid = str(last.get("cid") or "")
    for prefix, label in _CID_SOURCE:
        if cid.startswith(prefix):
            return label
    return "无pa-cid(无法归属)" if cid else "订单不可查(超出allOrders)"


def verdict(kind: str, stop_dist_pct: float | None, post: dict) -> str:
    """Attribute one exit: did the exit protect us, or was it noise?"""
    if kind == "no_stop":
        return "缺止损"
    if kind != "stop":
        return "策略/止盈平仓"
    adverse = post.get("post_adverse_pct")
    recover = post.get("recover_min")
    if adverse is not None and stop_dist_pct and adverse <= -0.5 * stop_dist_pct:
        return "止损正确(继续逆向)"
    if recover is not None and recover <= 60:
        return "止损过紧(1h内回到入场)"
    return "止损后部分回收"


def analyse(pos: dict, bars: list[dict], times: list[int]) -> dict:
    """One closed position -> excursion + exit-causation record."""
    side, entry = pos["side"], pos["entry"]
    opened_at, closed_at = pos["opened_at"], pos["closed_at"]
    exit_px = pos.get("close_px") or entry
    row = {
        "sym": pos["sym"],
        "side": "多" if side > 0 else "空",
        "conf": pos.get("conf"),
        "opened_local": fmt_ms(opened_at),
        "closed_local": fmt_ms(closed_at),
        "dur_min": (closed_at - opened_at) / MINUTE_MS,
        "entry": entry,
        "close_px": pos.get("close_px"),
        "stop": pos.get("stop"),
        "target": pos.get("target"),
        "legs": pos.get("legs", len(pos.get("exits") or [])),
        "net": net_of(pos),
    }
    row["stop_dist_pct"] = abs(entry - row["stop"]) / entry * 100 if row["stop"] else None
    if row["stop"] and row["target"] and abs(entry - row["stop"]) > 0:
        row["rr"] = abs(row["target"] - entry) / abs(entry - row["stop"])
    else:
        row["rr"] = None
    row.update(
        excursion(
            slice_bars(bars, times, opened_at - MINUTE_MS, closed_at),
            side,
            entry,
            opened_at,
            closed_at,
        )
    )
    row["noise_pct"] = noise_scale(
        slice_bars(bars, times, opened_at - 60 * MINUTE_MS, opened_at - 1), opened_at
    )
    row["stop_vs_noise"] = (
        row["stop_dist_pct"] / row["noise_pct"]
        if row["stop_dist_pct"] and row["noise_pct"] else None
    )
    row["exit_kind"] = exit_kind(pos)
    row.update(
        post_exit(
            slice_bars(bars, times, closed_at + 1, closed_at + POST_WINDOW_MS),
            side,
            entry,
            exit_px,
            closed_at,
        )
    )
    row["entry_quality"] = (
        "入场即错(几乎无浮盈)"
        if row["mfe_pct"] is not None and row["stop_dist_pct"]
        and row["mfe_pct"] < NEVER_WORKED_RATIO * row["stop_dist_pct"]
        else "曾浮盈"
    )
    row["verdict"] = verdict(row["exit_kind"], row["stop_dist_pct"], row)
    row["close_source"] = close_source(pos)
    return row


def fmt_opt(v, pattern="%s"):
    return "-" if v is None else pattern % v


def print_rows(rows: list[dict]) -> None:
    print("  开仓时间          币       向 时长  入场->出场              止损% 噪声x   MAE%    MFE% 出场   平仓来源        判定")
    for r in sorted(rows, key=lambda x: x["opened_local"]):
        print(
            "  %s %-6s %s %5.0fm %10.6g->%-10.6g %s %s %s %s %-6s %-14s %s"
            % (
                r["opened_local"][5:],
                r["sym"].replace("USDT", ""),
                r["side"],
                r["dur_min"],
                r["entry"],
                r["close_px"] or 0,
                fmt_opt(r["stop_dist_pct"], "%5.3f"),
                fmt_opt(r["stop_vs_noise"], "%5.2f"),
                fmt_opt(r["mae_pct"], "%7.3f"),
                fmt_opt(r["mfe_pct"], "%7.3f"),
                r["exit_kind"],
                r["close_source"],
                r["verdict"],
            )
        )


def summarise(rows: list[dict], label: str) -> dict:
    """Aggregate one set of attribution rows (pure, so tests can pin it)."""
    if not rows:
        return {}
    nets = [r["net"] for r in rows]
    mae = [r["mae_pct"] for r in rows if r["mae_pct"] is not None]
    mfe = [r["mfe_pct"] for r in rows if r["mfe_pct"] is not None]
    stops = [r for r in rows if r["exit_kind"] == "stop"]
    tight = [
        r for r in stops
        if r["stop_vs_noise"] is not None and r["stop_vs_noise"] < TIGHT_STOP_RATIO
    ]
    never = [r for r in rows if r["entry_quality"].startswith("入场即错")]
    by_verdict: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    for r in rows:
        for bucket, key in ((by_verdict, r["verdict"]), (by_source, r["close_source"])):
            agg = bucket.setdefault(key, {"n": 0, "net": 0.0})
            agg["n"] += 1
            agg["net"] += r["net"]
    return {
        "label": label,
        "n": len(rows),
        "win": len([n for n in nets if n > 0]),
        "winrate": len([n for n in nets if n > 0]) / len(rows) * 100,
        "net": sum(nets),
        "mae_avg": statistics.fmean(mae) if mae else None,
        "mfe_avg": statistics.fmean(mfe) if mfe else None,
        "stops": len(stops),
        "stops_tight": len(tight),
        "never_worked": len(never),
        "net_never_worked": sum(r["net"] for r in never),
        "net_tight_stops": sum(r["net"] for r in tight),
        "by_verdict": by_verdict,
        "by_source": by_source,
    }


def print_summary(s: dict) -> None:
    print(
        "%-7s n=%3d 胜率=%5s 净=%+8.2f | 均MAE=%s 均MFE=%s | 止损出场=%2d(噪声级 %2d) 入场即错=%2d"
        % (
            s["label"],
            s["n"],
            ("%.1f%%" % s["winrate"]) if s["winrate"] is not None else "-",
            s["net"],
            fmt_opt(s["mae_avg"], "%+.3f"),
            fmt_opt(s["mfe_avg"], "%+.3f"),
            s["stops"],
            s["stops_tight"],
            s["never_worked"],
        )
    )
    for name, agg in sorted(s["by_verdict"].items(), key=lambda kv: kv[1]["net"]):
        print("          %-24s n=%3d 净=%+8.2f" % (name, agg["n"], agg["net"]))
    for name, agg in sorted(s["by_source"].items(), key=lambda kv: kv[1]["net"]):
        print("     平仓来源 %-24s n=%3d 净=%+8.2f" % (name, agg["n"], agg["net"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--hours", type=int, default=0, help="rolling window, overrides --days")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--focus-min", type=int, default=30, help="detail table for shorter positions")
    ap.add_argument("--cache-dir", default="logs/_pnl_cache")
    ap.add_argument("--kl-cache-dir", default="logs/_kl_cache")
    ap.add_argument("--reuse-cache", action="store_true", help="skip the account re-fetch")
    ap.add_argument("--out", default="", help="optional json output path")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms, end_ms = window_bounds(args.days, hours=args.hours or None)
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    if not args.reuse_cache:
        _settings, client = make_client()
        print("fetching orders/trades since %s ..." % fmt_ms(start_ms - 24 * 3600 * 1000))
        fetch_account_data(client, symbols, start_ms - 24 * 3600 * 1000, cache)
    dec_idx, pending_rows = load_decision_index(symbols)
    positions = rebuild_trades(cache, symbols, dec_idx, pending_rows, by_position=True)

    # Close-time basis, matching the income ledger and trade_pnl_report --by-close.
    closed = [p for p in positions if p.get("closed_at") and start_ms <= p["closed_at"] < end_ms]
    if not closed:
        raise SystemExit("no closed positions in window")
    kl_lo = min(p["opened_at"] for p in closed) - 2 * 3600 * 1000
    kl_hi = min(max(p["closed_at"] for p in closed) + POST_WINDOW_MS, int(time.time() * 1000))
    print(
        "window %s .. %s | closed=%d | candles %s .. %s"
        % (fmt_ms(start_ms), fmt_ms(end_ms), len(closed), fmt_ms(kl_lo), fmt_ms(kl_hi))
    )

    rows = []
    for sym in symbols:
        mine = [p for p in closed if p["sym"] == sym]
        if not mine:
            continue
        bars = fetch_klines(sym, kl_lo, kl_hi, cache_dir=args.kl_cache_dir)
        if not bars:
            print("warning: no candles for %s" % sym)
            continue
        times = [b["time"] for b in bars]
        for p in mine:
            rows.append(analyse(p, bars, times))

    print()
    for name, _lo, _hi in BUCKETS:
        mine = [r for r in rows if bucket_of(r["dur_min"]) == name]
        if mine:
            print_summary(summarise(mine, name))
    print()
    print_summary(summarise(rows, "全部"))

    focus = [r for r in rows if r["dur_min"] < args.focus_min]
    print()
    print("逐笔明细 (<%dm, n=%d):" % (args.focus_min, len(focus)))
    print_rows(focus)

    kept = [r for r in rows if r["dur_min"] >= args.focus_min]
    print()
    print(
        "反事实: 剔除 <%dm 的 %d 笔后 剩 n=%d 净=%+.2f (原本 %+.2f)"
        % (args.focus_min, len(focus), len(kept), sum(r["net"] for r in kept), sum(r["net"] for r in rows))
    )

    if args.out:
        payload = {
            "window": {"start_ms": start_ms, "end_ms": end_ms},
            "summaries": {
                name: summarise([r for r in rows if bucket_of(r["dur_min"]) == name], name)
                for name, _lo, _hi in BUCKETS
            },
            "rows": rows,
        }
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print("json -> %s" % args.out)


if __name__ == "__main__":
    main()
