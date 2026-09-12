# -*- coding: utf-8 -*-
"""Audit: closed-trade PnL bucketed by decision stop-gap (entry->SL %)."""
import sys, tempfile
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, "tools"); sys.path.insert(0, ".")
from _pa_sim_common import (  # noqa: E402
    fetch_account_data, load_decision_index, make_client,
    rebuild_trades, window_bounds, fmt_ms,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ZECUSDT",
           "BNBUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "ADAUSDT"]
DAYS = 5
start_ms, end_ms = window_bounds(DAYS)
fetch_start = start_ms - 24 * 3600 * 1000

print("窗口:", fmt_ms(start_ms), "->", fmt_ms(end_ms - 1), "| 币数:", len(SYMBOLS))
settings, client = make_client()
with tempfile.TemporaryDirectory(prefix="pa-gap-") as tmp:
    cache = Path(tmp)
    print("fetching since", fmt_ms(fetch_start), "...")
    fetch_account_data(client, SYMBOLS, fetch_start, cache)
    dec_idx, pending_rows = load_decision_index(SYMBOLS)
    trades = rebuild_trades(cache, SYMBOLS, dec_idx, pending_rows)

def bucket(g):
    if g < 0.1: return "<0.1%"
    if g < 0.2: return "0.1-0.2%"
    if g < 0.3: return "0.2-0.3%"
    if g < 0.45: return "0.3-0.45%"
    if g < 1.0: return "0.45-1%"
    return ">=1%"

buckets = defaultdict(list)
for t in trades:
    if not (start_ms <= t["opened_at"] < end_ms):
        continue
    stop, entry = t.get("stop"), t.get("entry")
    if stop is None or not entry or entry <= 0 or stop <= 0:
        continue
    g = abs(stop - entry) / entry * 100
    buckets[bucket(g)].append(t)

print("\n== 各止损距离桶的成交表现 (窗口内开仓) ==")
print("%-10s %4s %5s %5s %6s %9s %8s %9s" %
      ("gap桶", "总", "已平", "持仓", "胜率", "已平净U", "均/笔U", "最大亏U"))
order = ["<0.1%", "0.1-0.2%", "0.2-0.3%", "0.3-0.45%", "0.45-1%", ">=1%"]
tot_all = sum(len(v) for v in buckets.values())
for b in order:
    ts = buckets.get(b, [])
    closed = [t for t in ts if t.get("closed_at")]
    nets = [t["realized"] + t["fees"] for t in closed]
    wins = [n for n in nets if n > 0]
    wr = (len(wins) / len(nets) * 100) if nets else None
    avg = (sum(nets) / len(nets)) if nets else None
    worst = min(nets) if nets else None
    print("%-10s %4d %5d %5d %5s %+9.2f %+8.3f %+9.2f" % (
        b, len(ts), len(closed), len(ts) - len(closed),
        ("%.0f" % wr) if wr is not None else "-",
        sum(nets), avg if avg is not None else 0.0, worst if worst is not None else 0.0))

print("\n== 窄(<0.45%) 明细 ==")
for t in sorted([t for b in ["<0.1%", "0.1-0.2%", "0.2-0.3%", "0.3-0.45%"] for t in buckets.get(b, [])],
                key=lambda x: x["opened_at"]):
    st = "CLOSE" if t.get("closed_at") else "OPEN "
    net = t["realized"] + t["fees"]
    print(" %s %s %s %s qty=%.5g entry=%-10.5g stop=%-10.5g gap=%.2f%% net=%+.3fU conf=%s" % (
        st, fmt_ms(t["opened_at"]), t["sym"], "多" if t["side"] == 1 else "空",
        t["qty"], t["entry"], t["stop"],
        abs(t["stop"] - t["entry"]) / t["entry"] * 100,
        net, t.get("conf")))
