# ruff: noqa: RUF002
"""Per-signal P&L report: rebuild trades from Binance USDM testnet fills,
attach signal decisions (confidence), and group by confidence cut / day /
direction / symbol with an income-ledger reconciliation.

Usage:
    python tools/trade_pnl_report.py                     # last 5 days, cut 55
    python tools/trade_pnl_report.py --days 14 --symbols ETHUSDT,ZECUSDT
    python tools/trade_pnl_report.py --conf-cut 60 --detail --out report.json
"""
from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter, defaultdict

from _pa_sim_common import (
    DEFAULT_SYMBOLS,
    day_key,
    fetch_account_data,
    fmt_ms,
    load_decision_index,
    load_json,
    make_client,
    rebuild_trades,
    window_bounds,
)


def net(t):
    return t["realized"] + t["fees"]


def stat(ts, unreal_map):
    closed = [t for t in ts if t.get("closed_at")]
    opened = [t for t in ts if not t.get("closed_at")]
    nets = [net(t) for t in closed]
    wins = [n for n in nets if n > 0]
    loss = [n for n in nets if n < 0]
    unreal = sum(unreal_map.get(t["sym"], 0.0) for t in opened)
    return {
        "n": len(ts), "closed": len(closed), "open": len(opened),
        "win": len(wins), "loss": len(loss),
        "winrate": (len(wins) / len(closed) * 100) if closed else None,
        "realized": sum(nets),
        "fees": sum(t["fees"] for t in ts),
        "unrealized": unreal,
        "total": sum(nets) + unreal,
        "avg": (sum(nets) / len(closed)) if closed else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(loss) / len(loss)) if loss else None,
        "best": max(nets) if nets else None,
        "worst": min(nets) if nets else None,
    }


def print_stat(label, g):
    wr = ("%.1f%%" % g["winrate"]) if g["winrate"] is not None else "-"
    avg = ("%+.3f" % g["avg"]) if g["avg"] is not None else "-"
    print("%-16s n=%3d (closed %3d, open %d)  胜=%d 负=%d 胜率=%s"
          % (label, g["n"], g["closed"], g["open"], g["win"], g["loss"], wr))
    print("   已平净=%+.2f (realized %+.2f, fees %+.2f) 浮盈=%+.2f 合计=%+.2f 平均/笔=%s"
          % (g["realized"], g["realized"] - g["fees"] + sum(0 for _ in []),
             g["fees"], g["unrealized"], g["total"], avg))
    if g["avg_win"] is not None:
        print("   平均盈单=%+.2f 平均亏单=%+.2f 最大=%+.2f 最小=%+.2f"
              % (g["avg_win"], g["avg_loss"], g["best"], g["worst"]))


def attach_unrealized(opened, positions):
    pos = {}
    for p in positions:
        amt = float(p.get("positionAmt") or 0)
        if amt != 0:
            pos[p["symbol"]] = float(p.get("unRealizedProfit") or 0)
    by_sym = defaultdict(list)
    for t in opened:
        by_sym[t["sym"]].append(t)
    unreal_map = {}
    for sym, ts in by_sym.items():
        total_qty = sum(t["qty"] for t in ts) or 1.0
        for t in ts:
            unreal_map[t["sym"]] = pos.get(sym, 0.0) * (t["qty"] / total_qty)
    return unreal_map, pos


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--conf-cut", type=int, default=55, help="confidence split point (default 55)")
    ap.add_argument("--detail", action="store_true", help="print per-trade rows")
    ap.add_argument("--out", default="", help="optional json output path")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms, end_ms = window_bounds(args.days)
    fetch_start = start_ms - 24 * 3600 * 1000

    settings, client = make_client()
    with tempfile.TemporaryDirectory(prefix="pa-pnl-") as tmp:
        from pathlib import Path

        cache = Path(tmp)
        print("fetching orders/trades since %s ..." % fmt_ms(fetch_start))
        fetch_account_data(client, symbols, fetch_start, cache)
        dec_idx, pending_rows = load_decision_index(symbols)
        trades = rebuild_trades(cache, symbols, dec_idx, pending_rows)
        positions = load_json(cache, "positions.json")
        income = load_json(cache, "income.json")
        orders = load_json(cache, "orders.json")

    in_win = [t for t in trades if start_ms <= t["opened_at"] < end_ms]
    unreal_map, _pos = attach_unrealized([t for t in in_win if not t.get("closed_at")], positions)
    cut = args.conf_cut
    groups = [
        ("置信度>=%d" % cut, [t for t in in_win if t["conf"] is not None and t["conf"] >= cut]),
        ("置信度<%d" % cut, [t for t in in_win if t["conf"] is not None and t["conf"] < cut]),
        ("无信号(手动/外部)", [t for t in in_win if t["conf"] is None]),
        ("全部", in_win),
    ]
    print("窗口 %s ~ %s | %d 币 | 交易 %d (平 %d / 持 %d)" % (
          fmt_ms(start_ms), fmt_ms(end_ms - 1), len(symbols), len(in_win),
          sum(1 for t in in_win if t.get("closed_at")),
          sum(1 for t in in_win if not t.get("closed_at"))))
    print("")
    for label, ts in groups:
        print_stat(label, stat(ts, unreal_map))
        print("")

    by_day = defaultdict(list)
    by_dir = defaultdict(list)
    by_sym = defaultdict(list)
    for t in in_win:
        by_day[day_key(t["opened_at"])].append(t)
        by_dir["做多" if t["side"] == 1 else "做空"].append(t)
        by_sym[t["sym"]].append(t)
    print("按开仓日：")
    for d in sorted(by_day):
        g = stat(by_day[d], unreal_map)
        wr = ("%.0f%%" % g["winrate"]) if g["winrate"] is not None else "-"
        print("  %s: n=%d 平=%d 持=%d 胜率=%s 已平净=%+.2f 合计=%+.2f"
              % (d, g["n"], g["closed"], g["open"], wr, g["realized"], g["total"]))
    print("按方向：")
    for d in ("做多", "做空"):
        g = stat(by_dir.get(d, []), unreal_map)
        wr = ("%.0f%%" % g["winrate"]) if g["winrate"] is not None else "-"
        print("  %s: n=%d 平=%d 持=%d 胜率=%s 已平净=%+.2f 合计=%+.2f"
              % (d, g["n"], g["closed"], g["open"], wr, g["realized"], g["total"]))
    print("按币种：")
    for sym in symbols:
        g = stat(by_sym.get(sym, []), unreal_map)
        wr = ("%.0f%%" % g["winrate"]) if g["winrate"] is not None else "-"
        print("  %-9s n=%3d 平=%3d 持=%d 胜率=%s 已平净=%+.2f 合计=%+.2f"
              % (sym, g["n"], g["closed"], g["open"], wr, g["realized"], g["total"]))
    print("")
    print("income 对账 (REALIZED_PNL/COMMISSION 窗口内)：")
    inc_sym = defaultdict(float)
    for r in income:
        if r["symbol"] in symbols and start_ms <= int(r["time"]) < end_ms and r["incomeType"] in ("REALIZED_PNL", "COMMISSION"):
            inc_sym[r["symbol"]] += float(r["income"])
    trade_r = defaultdict(float)
    trade_f = defaultdict(float)
    for t in in_win:
        trade_r[t["sym"]] += t["realized"]
        trade_f[t["sym"]] += t["fees"]
    for sym in symbols:
        diff = (trade_r[sym] + trade_f[sym]) - inc_sym[sym]
        print("  %-9s income=%+9.3f trades=%+9.3f diff=%+.3f"
              % (sym, inc_sym[sym], trade_r[sym] + trade_f[sym], diff))
    funding = sum(float(r["income"]) for r in income
                  if r["symbol"] in symbols and start_ms <= int(r["time"]) < end_ms
                  and r["incomeType"] == "FUNDING_FEE")
    print("  资金费(5币窗口合计): %+.4f" % funding)
    print("")
    attempts = Counter()
    for sym in symbols:
        for o in orders.get(sym, []):
            cid = str(o.get("clientOrderId") or "")
            if start_ms <= o["time"] < end_ms and cid.startswith("pa-entry-"):
                attempts[(o["status"], o["type"])] += 1
    print("入场订单(pa-entry)状态:", dict(attempts))

    if args.detail:
        print("")
        print("逐笔明细(窗口内):")
        for t in sorted(in_win, key=lambda x: x["opened_at"]):
            st = "OPEN" if not t.get("closed_at") else "CLOSE"
            conf = t["conf"] if t["conf"] is not None else "-"
            print("  %s %s %s open=%s qty=%.6g entry=%.6g real=%+.2f fees=%+.2f conf=%s"
                  % (st, t["sym"], "多" if t["side"] == 1 else "空",
                     fmt_ms(t["opened_at"]), t["qty"], t["entry"],
                     t["realized"], t["fees"], conf))
    if args.out:
        payload = {"window": [start_ms, end_ms], "symbols": symbols,
                   "conf_cut": cut,
                   "groups": {label: stat(ts, unreal_map) for label, ts in groups},
                   "by_day": {d: stat(by_day[d], unreal_map) for d in sorted(by_day)},
                   "by_symbol": {s: stat(by_sym.get(s, []), unreal_map) for s in symbols}}
        open(args.out, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False, indent=1))
        print("summary written:", args.out)


if __name__ == "__main__":
    main()

