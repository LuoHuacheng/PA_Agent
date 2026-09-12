# -*- coding: utf-8 -*-
"""Compare fixed vs ATR-based dynamic min_stop using real fills."""
import sys, json, time
from pathlib import Path
import urllib.request

sys.path.insert(0, "tools"); sys.path.insert(0, ".")
from _pa_sim_common import (  # noqa: E402
    fetch_account_data, load_decision_index, make_client,
    rebuild_trades, window_bounds,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ZECUSDT",
           "BNBUSDT", "DOGEUSDT", "TRXUSDT", "LINKUSDT", "ADAUSDT"]
DAYS = 5
start_ms, end_ms = window_bounds(DAYS)
fetch_start = start_ms - 24 * 3600 * 1000
cache = Path("logs/_pnl_cache")
cache.mkdir(parents=True, exist_ok=True)

settings, client = make_client()
if not (cache / "orders.json").exists():
    print("fetching fills...", flush=True)
    fetch_account_data(client, SYMBOLS, fetch_start, cache)
else:
    print("reuse cached fills", flush=True)
dec_idx, pending_rows = load_decision_index(SYMBOLS)
trades = rebuild_trades(cache, SYMBOLS, dec_idx, pending_rows)

# --- 15m ATR% per symbol (fapi public klines, last ~6d) ---
def fetch_klines(sym, limit=600):
    url = "https://fapi.binance.com/fapi/v1/klines?symbol=%s&interval=15m&limit=%d" % (sym, limit)
    req = urllib.request.Request(url, headers={"User-Agent": "PA_Agent/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def atr_pct(kl):
    trs = []
    for i in range(1, len(kl)):
        h, l, pc = float(kl[i][2]), float(kl[i][3]), float(kl[i - 1][4])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    n = min(14, len(trs))
    atr = sum(trs[-n:]) / n if n else 0.0
    close = float(kl[-1][4]) if kl else 1.0
    return atr / close * 100

atr_map = {}
for s in SYMBOLS:
    try:
        atr_map[s] = atr_pct(fetch_klines(s))
    except Exception as e:
        print("atr fail", s, e)
        atr_map[s] = None
    time.sleep(0.05)
print("品种 15m ATR%%:", {s: (round(v, 3) if v else None) for s, v in atr_map.items()})

# --- 窗口内已平仓成交单, attach gap ---
rows = []
for t in trades:
    if not (start_ms <= t["opened_at"] < end_ms):
        continue
    stop, entry = t.get("stop"), t.get("entry")
    if stop is None or not entry or entry <= 0 or stop <= 0:
        continue
    net = t.get("realized", 0.0) + t.get("fees", 0.0)
    rows.append({"sym": t["sym"], "gap": abs(stop - entry) / entry * 100,
                 "net": net, "closed": bool(t.get("closed_at")), "conf": t.get("conf")})
print("窗口内成交(带stop):", len(rows), "| 已平:", sum(1 for r in rows if r["closed"]))

def scheme_value(rows, floor_pct=None, atr_k=None):
    """被拦单的净影响: 返回 (拦数, 误杀好钱, 避开坏钱, 净避损)."""
    blocked = []
    for r in rows:
        if not r["closed"]:
            continue
        if atr_k is not None:
            base = atr_map.get(r["sym"])
            if base is None:
                continue
            lim = max(floor_pct or 0.0, atr_k * base)
        else:
            lim = floor_pct
        if r["gap"] < lim:
            blocked.append(r)
    kill_good = sum(r["net"] for r in blocked if r["net"] > 0)   # 误杀的好钱
    avoid_bad = -sum(r["net"] for r in blocked if r["net"] < 0)  # 避开的坏钱
    return (len(blocked), kill_good, avoid_bad, avoid_bad - kill_good)

print("\n方案对比 (仅已平仓单; 被拦=没成交):")
print("%-22s %5s %10s %10s %10s" % ("方案", "拦数", "误杀好钱U", "避开坏钱U", "净避损U"))
for label, kw in [
    ("固定 0.20%", dict(floor_pct=0.20)),
    ("固定 0.45%", dict(floor_pct=0.45)),
    ("动态 max(0.2, 0.5xATR)", dict(floor_pct=0.2, atr_k=0.5)),
    ("动态 max(0.2, 0.8xATR)", dict(floor_pct=0.2, atr_k=0.8)),
    ("动态 max(0.2, 1.0xATR)", dict(floor_pct=0.2, atr_k=1.0)),
    ("动态 max(0.3, 0.8xATR)", dict(floor_pct=0.3, atr_k=0.8)),
]:
    n, g, a, v = scheme_value(rows, **kw)
    print("%-22s %5d %10.2f %10.2f %10.2f" % (label, n, g, a, v))

print("\n各方案胜率对照(全部已平):")
closed = [r for r in rows if r["closed"]]
for label, lo, hi in [("<0.2%", 0, 0.2), ("0.2-0.45", 0.2, 0.45), ("0.45-1", 0.45, 1.0), (">=1%", 1.0, 99)]:
    grp = [r for r in closed if lo <= r["gap"] < hi]
    if grp:
        wr = sum(1 for r in grp if r["net"] > 0) / len(grp) * 100
        print("  %-8s n=%2d 胜率=%3.0f%% 净=%+8.2fU" % (label, len(grp), wr, sum(r["net"] for r in grp)))
