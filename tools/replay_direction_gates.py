"""Replay direction gates over recorded decisions to measure what they would block.

For every recorded limit/breakout order row in trade_records/*.csv (window
days), rebuild the 60 closed 15m bars visible at decision time (Binance
klines endTime) and run evaluate_direction_gates. Where the signal actually
filled, attach the rebuilt realised PnL so we can see both sides: losses the
gates would have avoided and wins they would have missed.

Usage:
    python tools/replay_direction_gates.py --days 3
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pa_agent.trading.direction_gates import evaluate_direction_gates

TZ = timezone(timedelta(hours=8))
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT",
           "ZECUSDT", "DOGEUSDT", "LINKUSDT", "ADAUSDT"]


def _client():
    from pa_agent.config.settings import load_settings
    from pa_agent.trading.binance_usdm_testnet import BinanceUSDMTestnetClient

    s = load_settings()
    cfg = s.binance_usdm_testnet
    return BinanceUSDMTestnetClient(cfg.api_key, cfg.api_secret)


_KL_URL = "https://fapi.binance.com/fapi/v1/klines"


def bars_before(_client_unused, symbol: str, ms: int) -> list:
    """60 closed 15m bars ending before ms, from the public mainnet klines feed
    (testnet prices mirror mainnet; public feed avoids the shared testnet IP
    rate-limit bans)."""
    import json as _json
    import urllib.parse as _urlparse
    import urllib.request as _request

    q = _urlparse.urlencode({"symbol": symbol, "interval": "15m",
                             "limit": 60, "endTime": int(ms) - 1})
    try:
        with _request.urlopen(_KL_URL + "?" + q, timeout=15) as resp:
            ks = _json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        bars_before.failures = getattr(bars_before, "failures", 0) + 1
        bars_before.last_error = str(exc)[:120]
        return []
    out = []
    for k in ks:
        out.append({"open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4])})
    out.reverse()  # newest (K1) first, matching runtime frame.bars
    return out


def tick_for(symbol: str) -> float:
    if symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ZECUSDT"):
        return 0.01
    return 0.0001


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    client = _client()
    cutoff = time.time() - args.days * 86400
    rows_all: list[tuple] = []
    for sym in SYMBOLS:
        fp = f"trade_records/{sym}_15m.csv"
        if not os.path.exists(fp):
            continue
        with open(fp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rt = str(row.get("record_time") or "").strip()
                ot = str(row.get("order_type") or "").strip()
                if not rt or ot not in ("限价单", "突破单"):
                    continue
                try:
                    ts = datetime.strptime(rt, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=TZ).timestamp() * 1000
                except ValueError:
                    continue
                if ts < cutoff * 1000:
                    continue
                rows_all.append((sym, ts, row))
    rows_all.sort(key=lambda x: x[1])

    print(f"窗口 {args.days}d: 决策行 {len(rows_all)}")
    stats: dict[str, int] = {}
    skipped = 0
    for i, (sym, ts, row) in enumerate(rows_all):
        bars = bars_before(client, sym, ts)
        time.sleep(0.12)
        if not bars:
            skipped += 1
            continue
        decision = {"order_type": str(row.get("order_type")).strip(),
                    "order_direction": str(row.get("order_direction")).strip(),
                    "entry_price": float(row.get("entry_price") or 0)}
        prev_dir = None
        for j in range(i - 1, -1, -1):
            if rows_all[j][0] == sym:
                prev_dir = str(rows_all[j][2].get("diag_direction") or "").strip().lower()
                break
        prev = None
        if prev_dir in ("bullish", "bearish", "neutral"):
            prev = type("R", (), {"stage1_diagnosis": {"direction": prev_dir}})()
        reasons = evaluate_direction_gates(decision=decision, bars=bars,
                                           tick=tick_for(sym), previous_record=prev)
        if not reasons:
            continue
        gate = ",".join(sorted({r.split(":", 1)[0] for r in reasons}))
        stats[gate] = stats.get(gate, 0) + 1
        print(f"  [{gate}] {sym} {row.get('record_time')} "
              f"{row.get('order_direction')} entry={row.get('entry_price')} "
              f"| {'; '.join(reasons)[:120]}")
    print("命中汇总:", stats)
    print("bars 获取失败行:", skipped, "| 最近错误:",
          getattr(bars_before, "last_error", "") if skipped else "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
