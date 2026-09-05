# ruff: noqa: RUF002
"""Simulate trailing/breakeven stop rules over real fills + 1m klines.

Replays each closed trade (reconstructed from Binance USDM testnet fills,
paired FIFO/LIFO per pa-entry signal with its decision stop/target) over
1-minute bars and compares static stop-loss/TP exits against alternative
protective rules (breakeven after N×R profit, peak-trailing at N×R).

Usage:
    python tools/sim_trailing_stop.py                  # last 5 days, all rules
    python tools/sim_trailing_stop.py --days 14 --rules be1r,be_tp
    python tools/sim_trailing_stop.py --interval 5m --out result.json

Read-only: fetches account orders/trades (settings.json credentials) and
public 1m klines. Writes nothing except an optional --out json.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# make pa_agent importable when run from anywhere
HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pa_agent.config.settings import load_settings
from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient

TZ = timezone(timedelta(hours=8))
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ZECUSDT"]
RULES = ["be05r", "be1r", "trail05r", "trail1r", "be_tp"]
_KL_URL = "https://fapi.binance.com/fapi/v1/klines"


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M")


# ---------- decision material hash (mirrors binance_usdm_testnet._signal_id)
def _num_forms(v):
    if v is None or str(v).strip() == "":
        return [None]
    f = float(str(v))
    s = {f}
    if f == int(f):
        s.add(int(f))
    s.add(str(f))
    return list(s)


def cid_variants(sym, direction, otype, entry, stop, target):
    out = []
    for e in _num_forms(entry):
        for s in _num_forms(stop):
            for t in _num_forms(target):
                if e is None or s is None or t is None:
                    continue
                mat = {"symbol": sym, "direction": direction, "type": otype,
                       "entry": e, "stop": s, "target": t}
                h = hashlib.sha256(json.dumps(mat, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                out.append("pa-entry-" + h[:27])
    return out


def load_decision_index(symbols: list[str]) -> tuple[dict, list]:
    """clientOrderId -> decision dict (pending records first, CSV fallback).
    Also returns ordered pending rows (sym, ts, inner) for nearest-time fallback.
    """
    pending = []
    for fp in sorted(Path("records/pending").glob("*.json")):
        p = fp.name[:-5].split("_")
        if len(p) != 4 or p[2] not in symbols:
            continue
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        s2 = raw.get("stage2_decision") or {}
        inner = s2.get("decision") if isinstance(s2, dict) else None
        if not isinstance(inner, dict):
            continue
        ts = (raw.get("meta") or {}).get("timestamp_local_ms")
        if ts and inner.get("order_type") in ("市价单", "限价单"):
            pending.append((p[2], int(ts), inner))
    pending.sort(key=lambda x: x[1])
    csv_rows = []
    for f in sorted(Path("trade_records").glob("*.csv")):
        sym = f.stem.split("_")[0].upper()
        if sym not in symbols:
            continue
        for row in csv.DictReader(open(f, encoding="utf-8-sig")):
            rt = str(row.get("record_time") or "").strip()
            if not rt or row.get("order_type") not in ("市价单", "限价单"):
                continue
            try:
                ts = int(datetime.strptime(rt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp() * 1000)
            except ValueError:
                continue
            csv_rows.append((sym, ts, row))
    idx = {}
    for (sym, _ts, inner) in pending:
        for c in cid_variants(sym, inner.get("order_direction"), inner.get("order_type"),
                              inner.get("entry_price"), inner.get("stop_loss_price"),
                              inner.get("take_profit_price")):
            idx.setdefault(c, inner)
    for (sym, _ts, row) in csv_rows:
        for c in cid_variants(sym, row.get("order_direction"), row.get("order_type"),
                              row.get("entry_price"), row.get("stop_loss_price"),
                              row.get("take_profit_price")):
            idx.setdefault(c, row)
    return idx, pending


def fetch_account_data(client, symbols: list[str], start_ms: int, cache_dir: Path) -> None:
    oa, ut = {}, {}
    for sym in symbols:
        orders, cur = [], None
        while True:
            p = {"symbol": sym, "limit": 1000}
            if cur:
                p["orderId"] = cur
            batch = client._request("GET", "/fapi/v1/allOrders", p, signed=True)
            if not batch:
                break
            orders += batch
            if len(batch) < 1000 or min(x["time"] for x in batch) < start_ms:
                break
            cur = min(x["orderId"] for x in batch) - 1
            time.sleep(0.08)
        fills, cur = [], None
        while True:
            p = {"symbol": sym, "limit": 1000}
            if cur:
                p["fromId"] = cur
            batch = client._request("GET", "/fapi/v1/userTrades", p, signed=True)
            if not batch:
                break
            fills += batch
            if len(batch) < 1000:
                break
            cur = max(x["id"] for x in batch) + 1
            time.sleep(0.08)
        oa[sym], ut[sym] = orders, fills
    json.dump(oa, open(cache_dir / "orders.json", "w"))
    json.dump(ut, open(cache_dir / "user_trades.json", "w"))


def fetch_klines(symbols: list[str], start_ms: int, end_ms: int, interval: str, cache_dir: Path) -> None:
    out = {}
    for sym in symbols:
        bars, cursor = [], start_ms
        while cursor < end_ms:
            url = ("%s?symbol=%s&interval=%s&startTime=%d&endTime=%d&limit=1500"
                   % (_KL_URL, sym, interval, cursor, end_ms))
            req = urllib.request.Request(url, headers={"User-Agent": "PA_Agent/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                batch = json.loads(resp.read())
            if not batch:
                break
            bars += batch
            nxt = batch[-1][0] + 60000 * (1 if interval == "1m" else 5 if interval == "5m" else 15)
            if nxt <= cursor:
                break
            cursor = nxt
            time.sleep(0.1)
        out[sym] = [[b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4])] for b in bars]
    json.dump(out, open(cache_dir / "klines.json", "w"))


def rebuild_trades(cache_dir: Path, symbols: list[str], start_ms: int, dec_idx, pending_rows):
    """LIFO fill pairing; each trade = one pa-entry signal. Returns trades with
    decision stop/target attached (closed trades in window only)."""
    orders_all = json.load(open(cache_dir / "orders.json", encoding="utf-8"))
    trades_all = json.load(open(cache_dir / "user_trades.json", encoding="utf-8"))
    order_by_id = {(s, o["orderId"]): o for s, lst in orders_all.items() for o in lst}
    order_time = {}
    for s, lst in orders_all.items():
        for o in lst:
            c = str(o.get("clientOrderId") or "")
            if c:
                order_time.setdefault(c, (s, o["time"]))
    all_trades = []
    for sym in symbols:
        queue = []
        for fl in sorted(trades_all.get(sym, []), key=lambda x: (x["time"], x["id"])):
            qty = float(fl["qty"])
            delta = qty if fl["side"] == "BUY" else -qty
            px = float(fl["price"])
            rpnl = float(fl.get("realizedPnl") or 0)
            comm = -abs(float(fl.get("commission") or 0))
            o = order_by_id.get((sym, fl["orderId"]), {})
            cid = str(o.get("clientOrderId") or "")
            is_entry = cid.startswith("pa-entry-")
            rem = qty
            while rem > 1e-12 and queue and queue[-1]["side"] != (1 if delta > 0 else -1):
                E = queue[-1]
                take = min(rem, E["rem"])
                E["rem"] -= take
                E["realized"] += rpnl * (take / qty)
                E["fees"] += comm * (take / qty)
                E["close_px"] = px
                rem -= take
                if E["rem"] < 1e-12:
                    E["closed_at"] = fl["time"]
                    all_trades.append(E)
                    queue.pop()
            if rem > 1e-12:
                side = 1 if delta > 0 else -1
                last = queue[-1] if queue else None
                if last is not None and last["cid"] == cid and last["side"] == side:
                    tot = last["orig"] + rem
                    last["avg"] = (last["avg"] * last["orig"] + px * rem) / tot
                    last["orig"] = tot
                    last["rem"] += rem
                    last["fees"] += comm * (rem / qty)
                else:
                    queue.append({"sym": sym, "side": side, "orig": rem, "rem": rem,
                                  "avg": px, "realized": 0.0, "fees": comm * (rem / qty),
                                  "cid": cid if is_entry else "manual",
                                  "opened_at": fl["time"], "closed_at": None, "close_px": None})
        all_trades += queue
    out = []
    for t in all_trades:
        if not t.get("closed_at") or t["opened_at"] < start_ms:
            continue
        inner = dec_idx.get(t["cid"]) if t["cid"] and t["cid"] != "manual" else None
        if inner is None:
            dr = "做多" if t["side"] == 1 else "做空"
            ref = order_time.get(t["cid"], (None, t["opened_at"]))[1]
            best, bd = None, 1e18
            for (sym2, ts2, it2) in pending_rows:
                if sym2 != t["sym"] or it2.get("order_direction") != dr:
                    continue
                d = abs(ts2 - ref)
                if d < bd:
                    bd, best = d, it2
            inner = best
        if inner is None:
            continue
        try:
            stop = float(inner.get("stop_loss_price"))
            target = float(inner.get("take_profit_price"))
        except (TypeError, ValueError):
            continue
        if not stop or not target:
            continue
        out.append({"sym": t["sym"], "side": t["side"], "qty": t["orig"], "entry": t["avg"],
                    "stop": stop, "target": target, "open": t["opened_at"], "close": t["closed_at"],
                    "close_px": t["close_px"], "realized": t["realized"], "fees": t["fees"]})
    return out


def replay(t, rule, klines):
    """Simulate one trade under *rule* over bars in [open, close] (+2 min pad).
    Returns (exit_px, kind) or None when the path never triggers (actual exit
    was not a static SL/TP hit, e.g. rollback/manual close)."""
    bars = sorted([b for b in klines[t["sym"]] if t["open"] - 120000 <= b[0] <= t["close"] + 120000],
                  key=lambda b: b[0])
    if not bars:
        return None
    entry, stop0, tp = t["entry"], t["stop"], t["target"]
    risk = abs(entry - stop0)
    if t["side"] == 1:
        sl, peak = stop0, entry
        for b in bars:
            _ts, _o, h, l, _c = b
            peak = max(peak, h)
            if rule == "be05r" and peak >= entry + 0.5 * risk:
                sl = max(sl, entry)
            elif rule == "be1r" and peak >= entry + risk:
                sl = max(sl, entry)
            elif rule == "trail05r":
                sl = max(sl, peak - 0.5 * risk)
            elif rule == "trail1r":
                sl = max(sl, peak - 1.0 * risk)
            elif rule == "be_tp" and peak >= tp:
                sl = max(sl, entry)
            if h >= tp:
                return (tp, "tp")
            if l <= sl:
                return (sl, "sl")
    else:
        sl, trough = stop0, entry
        for b in bars:
            _ts, _o, h, l, _c = b
            trough = min(trough, l)
            if rule == "be05r" and trough <= entry - 0.5 * risk:
                sl = min(sl, entry)
            elif rule == "be1r" and trough <= entry - risk:
                sl = min(sl, entry)
            elif rule == "trail05r":
                sl = min(sl, trough + 0.5 * risk)
            elif rule == "trail1r":
                sl = min(sl, trough + 1.0 * risk)
            elif rule == "be_tp" and trough <= tp:
                sl = min(sl, entry)
            if l <= tp:
                return (tp, "tp")
            if h >= sl:
                return (sl, "sl")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=5, help="analysis window in days (default 5)")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="comma-separated symbols (default: 5-symbol pool)")
    ap.add_argument("--interval", default="1m", choices=["1m", "5m", "15m"], help="kline interval (default 1m)")
    ap.add_argument("--rules", default=",".join(RULES), help="comma-separated rules to simulate")
    ap.add_argument("--big-win-usdt", type=float, default=8.0,
                    help="net-usdt threshold defining big winners (default 8)")
    ap.add_argument("--out", default="", help="optional json output path for per-rule summary")
    ap.add_argument("--detail", action="store_true", help="print per-trade diffs (first 12)")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rules = [r for r in args.rules.split(",") if r in RULES]
    if not rules:
        raise SystemExit("no valid rules; choose from " + ",".join(RULES))
    settings = load_settings()
    cfg = settings.binance_usdm_testnet
    if not cfg.api_key or not cfg.api_secret:
        raise SystemExit("Binance Testnet API key/secret missing in settings.json")

    tz_day_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int((tz_day_start - timedelta(days=args.days - 1)).timestamp() * 1000)
    fetch_start_ms = int((tz_day_start - timedelta(days=args.days)).timestamp() * 1000)
    end_ms = int((tz_day_start + timedelta(days=1)).timestamp() * 1000)

    with tempfile.TemporaryDirectory(prefix="pa-trail-sim-") as tmp:
        cache = Path(tmp)
        client = BinanceUSDMTestnetClient(cfg.api_key, cfg.api_secret)
        print("fetching orders/trades since %s ..." % fmt_ms(fetch_start_ms))
        fetch_account_data(client, symbols, fetch_start_ms, cache)
        print("fetching %s klines ..." % args.interval)
        fetch_klines(symbols, fetch_start_ms, end_ms, args.interval, cache)
        dec_idx, pending_rows = load_decision_index(symbols)
        trades = rebuild_trades(cache, symbols, start_ms, dec_idx, pending_rows)
        klines = json.loads(open(cache / "klines.json", encoding="utf-8").read())
        print("closed trades with decision in window: %d" % len(trades))

        actual_total = sum(t["realized"] + t["fees"] for t in trades)
        big_total = sum(1 for t in trades if t["realized"] + t["fees"] > args.big_win_usdt)
        print("actual closed net: %+.2f | big winners >%.0f: %d"
              % (actual_total, args.big_win_usdt, big_total))
        print()
        rows = []
        print("%-9s %10s %5s %5s %5s %5s %10s" % ("rule", "net", "tp", "sl", "miss", "kept", "vs-actual"))
        for rule in rules:
            net = tp_n = sl_n = miss = kept = 0
            diffs = []
            for t in trades:
                res = replay(t, rule, klines)
                if res is None:
                    miss += 1
                    continue
                px, kind = res
                if kind == "tp":
                    tp_n += 1
                else:
                    sl_n += 1
                sim = (px - t["entry"]) * t["qty"] * t["side"] + t["fees"]
                net += sim
                diffs.append(sim - (t["realized"] + t["fees"]))
                if (t["realized"] + t["fees"]) > args.big_win_usdt and sim > 0:
                    kept += 1
            print("%-9s %+10.2f %5d %5d %5d %5d %+10.2f"
                  % (rule, net, tp_n, sl_n, miss, kept, net - actual_total))
            rows.append({"rule": rule, "net": net, "tp": tp_n, "sl": sl_n,
                         "miss": miss, "big_kept": kept, "vs_actual": net - actual_total})
            if args.detail and diffs:
                ds = sorted(diffs)
                print("   mean-diff %+6.2f | worse>1 %d | better>1 %d | worst %+7.2f | best %+7.2f"
                      % (sum(ds) / len(ds),
                         sum(1 for d in ds if d < -1),
                         sum(1 for d in ds if d > 1), ds[0], ds[-1]))
        if args.out:
            payload = {"window_days": args.days, "symbols": symbols, "interval": args.interval,
                       "actual_net": actual_total, "big_win_usdt": args.big_win_usdt,
                       "trades": len(trades), "rules": rows}
            Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            print("summary written:", args.out)


if __name__ == "__main__":
    main()