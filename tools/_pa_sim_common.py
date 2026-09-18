"""Shared plumbing for PA trade-analysis CLI tools (fetch, decision matching, fill pairing)."""
from __future__ import annotations

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# make pa_agent importable when run from anywhere
HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pa_agent.config.settings import load_settings
from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient
from pa_agent.trading.order_ledger import (  # noqa: E402 - imports follow the sys.path fix
    attribute_exit,
    exit_placements,
    merge_into_orders,
)
from pa_agent.trading.replay_pairing import (
    cid_variants,
    rebuild_positions,
    rebuild_trades as _rebuild_trades_from_fills,
)

#: /fapi/v1/userTrades rejects startTime..endTime spans wider than 7 days.
USER_TRADES_MAX_SPAN_MS = 6 * 24 * 3600 * 1000

TZ = timezone(timedelta(hours=8))
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ZECUSDT"]
#: Fills live on testnet, so excursions must be measured with testnet candles:
#: mainnet prices differ by ~0.02%, the same order as the stop distances under
#: investigation (a mainnet candle can therefore fake a stop-out that never
#: happened). Mainnet REST stays available via the *base* argument.
KL_TESTNET_BASE = "https://testnet.binancefuture.com"
KL_MAINNET_BASE = "https://fapi.binance.com"


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d")


def window_bounds(days: int, *, hours: int | None = None):
    """Window as (start_ms, end_ms).

    Natural local-day window by default; pass hours for a rolling window
    ending now (start = now - hours).
    """
    if hours:
        end_ms = int(time.time() * 1000)
        return end_ms - hours * 3600 * 1000, end_ms
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def parse_kline(row) -> dict:
    """One REST kline row -> dict with ms/ohlc/volume."""
    return {
        "time": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
    }


def fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    interval: str = "1m",
    base: str = KL_TESTNET_BASE,
    cache_dir=None,
) -> list[dict]:
    """Klines covering [start_ms, end_ms], oldest first.

    Pages forward with startTime until a batch runs past *end_ms*. Pass
    *cache_dir* to memoise the exact request on disk: the same window is
    re-read by every analysis pass.
    """
    cache_fp = None
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_fp = cache_dir / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"
        if cache_fp.exists():
            return json.loads(cache_fp.read_text(encoding="utf-8"))
    out, cursor = [], int(start_ms)
    while cursor < end_ms:
        url = (
            f"{base}/fapi/v1/klines?symbol={symbol}&interval={interval}"
            f"&startTime={cursor}&endTime={end_ms}&limit=1500"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "pa-agent/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    batch = json.loads(resp.read())
                break
            except urllib.error.URLError:
                if attempt == 3:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if not batch:
            break
        out += [parse_kline(r) for r in batch]
        nxt = int(batch[-1][0]) + 1
        if nxt <= cursor:
            break  # no forward progress: stop instead of spinning
        cursor = nxt
        if len(batch) < 1500:
            break
        time.sleep(0.05)
    if cache_fp is not None:
        cache_fp.write_text(json.dumps(out), encoding="utf-8")
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
        # userTrades is capped at a SEVEN-DAY startTime..endTime span, and without
        # startTime it returns only the most recent page. Requesting 15 days in one
        # call therefore silently returned ~7 days (account commission looked 5x
        # smaller than the income ledger). Walk the range in <=7-day chunks, paging
        # each chunk forward by fromId.
        fills, seen = [], set()
        chunk_start = start_ms
        now_ms = int(time.time() * 1000)
        while chunk_start < now_ms:
            chunk_end = min(chunk_start + USER_TRADES_MAX_SPAN_MS, now_ms)
            cur = None
            while True:
                p = {"symbol": sym, "limit": 1000}
                if cur is None:
                    p["startTime"] = chunk_start
                    p["endTime"] = chunk_end
                else:
                    p["fromId"] = cur
                batch = client._request("GET", "/fapi/v1/userTrades", p, signed=True)
                if not batch:
                    break
                fresh = [f for f in batch if f.get("id") not in seen]
                seen.update(f.get("id") for f in fresh)
                fills += fresh
                if len(batch) < 1000:
                    break
                nxt = max(x["id"] for x in batch) + 1
                if cur is not None and nxt <= cur:
                    break  # no forward progress: stop instead of spinning
                cur = nxt
                time.sleep(0.08)
            chunk_start = chunk_end + 1
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


def rebuild_trades(
    cache_dir, symbols, dec_idx, pending_rows, *, by_position: bool = False, ledger_dir=None
):
    """LIFO pairing; one trade per pa-entry signal (open trades included).

    The LIFO rebuild itself lives in pa_agent.trading.replay_pairing
    (pure/dict based); this wrapper loads the cached account payloads, keeps
    the order_time index used by the nearest-time decision fallback, and
    reproduces the legacy per-trade decision attachment.

    Historic placements are recovered from the local order ledger before
    pairing: the exchange listing is bounded (~7 days measured) and consumed
    conditional orders have no history endpoint, so without the ledger most
    old exits cannot be attributed to the order that closed them.
    """
    orders_all = json.load(open(cache_dir / "orders.json", encoding="utf-8"))
    trades_all = json.load(open(cache_dir / "user_trades.json", encoding="utf-8"))
    merge_into_orders(orders_all, ledger_dir=ledger_dir)
    placements = exit_placements(ledger_dir=ledger_dir)
    order_time = {}
    for s, lst in orders_all.items():
        for o in lst:
            c = str(o.get("clientOrderId") or "")
            if c:
                order_time.setdefault(c, (s, o["time"]))
    out = []
    pair = rebuild_positions if by_position else _rebuild_trades_from_fills
    for t in pair(orders_all, trades_all, symbols):
        inner = _match_decision(t, dec_idx, pending_rows, order_time)
        rec = {"sym": t["sym"], "side": t["side"], "qty": t["qty"], "entry": t["entry"],
               "realized": t["realized"], "fees": t["fees"], "cid": t["cid"],
               "opened_at": t["opened_at"], "closed_at": t["closed_at"],
               "close_px": t["close_px"], "conf": None, "stop": None,
               "target": None, "manual": t["cid"] == "manual"}
        # Position pairing carries the per-position exits; the legacy LIFO pairing
        # does not. Dropping them here silently emptied partial-TP analysis.
        for extra in ("exits", "fills", "legs"):
            if extra in t:
                rec[extra] = t[extra]
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
        # An algo stop leaves a fill whose orderId no endpoint resolves; the
        # ledger knows which exit-family order was resting when it fired.
        for exit_row in rec.get("exits") or ():
            if not exit_row.get("cid") and exit_row.get("time"):
                exit_row["cid"] = attribute_exit(t["sym"], int(exit_row["time"]), placements)
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

