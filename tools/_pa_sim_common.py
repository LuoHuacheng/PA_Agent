"""Shared plumbing for PA trade-analysis CLI tools (fetch, decision matching, fill pairing)."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
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
_KL_URL = "https://fapi.binance.com/fapi/v1/klines"


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d")


def window_bounds(days: int):
    """Local-day window: start of (days-1) days ago to start of tomorrow."""
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _num_forms(v):
    """Candidate numeric forms for the signal-id hash."""
    if v is None or str(v).strip() == "":
        return [None]
    f = float(str(v))
    s = {f}
    if f == int(f):
        s.add(int(f))
    s.add(str(f))
    return list(s)


def cid_variants(sym, direction, otype, entry, stop, target):
    """clientOrderId candidates from the decision material hash."""
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


def load_decision_index(symbols):
    """Map clientOrderId -> decision; returns (idx, ordered pending rows)."""
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


def fetch_account_data(client, symbols, start_ms, cache_dir):
    """Fetch orders/userTrades plus income/positions (best effort)."""
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
    try:
        inc = client.income_history(start_ms=start_ms)
        json.dump(inc, open(cache_dir / "income.json", "w"))
        pos = client._request("GET", "/fapi/v2/positionRisk", {}, signed=True)
        json.dump(pos, open(cache_dir / "positions.json", "w"))
    except Exception as exc:
        print("warning: income/positions fetch failed:", exc)


def _match_decision(t, dec_idx, pending_rows, order_time):
    """Decision record for a trade (hash match, else nearest-time fallback)."""
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
    return inner


def rebuild_trades(cache_dir, symbols, dec_idx, pending_rows):
    """LIFO pairing; one trade per pa-entry signal (open trades included)."""
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
        inner = _match_decision(t, dec_idx, pending_rows, order_time)
        rec = {"sym": t["sym"], "side": t["side"], "qty": t["orig"], "entry": t["avg"],
               "realized": t["realized"], "fees": t["fees"], "cid": t["cid"],
               "opened_at": t["opened_at"], "closed_at": t["closed_at"],
               "close_px": t["close_px"], "conf": None, "stop": None,
               "target": None, "manual": t["cid"] == "manual"}
        if inner is not None:
            tc = inner.get("trade_confidence")
            try:
                rec["conf"] = int(float(tc)) if tc not in (None, "") else None
            except (TypeError, ValueError):
                pass
            try:
                rec["stop"] = float(inner.get("stop_loss_price"))
                rec["target"] = float(inner.get("take_profit_price"))
            except (TypeError, ValueError):
                pass
        out.append(rec)
    return out


def make_client(settings=None):
    """Return (settings, client) built from settings.json credentials."""
    s = settings or load_settings()
    cfg = s.binance_usdm_testnet
    if not cfg.api_key or not cfg.api_secret:
        raise SystemExit("Binance Testnet API key/secret missing in settings.json")
    return s, BinanceUSDMTestnetClient(cfg.api_key, cfg.api_secret)


def load_json(cache_dir, name):
    """Read a cached json file."""
    return json.load(open(cache_dir / name, encoding="utf-8"))

