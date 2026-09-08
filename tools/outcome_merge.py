#!/usr/bin/env python3
"""Outcome merge CLI (Phase A冒烟 step 1): fills -> outcomes.csv + audit.

Read-only: fetches the Testnet account payloads (orders/userTrades/income/
positions via the existing fetch_account_data plumbing), merges them with the
decision records (pending JSON + CSV fallback) through the feedback outcome
store, appends OutcomeRows to trade_records/outcomes.csv (uid idempotent) and
writes logs/outcome_audit_YYYYMMDD.json.

No API keys configured / network unavailable -> explains and exits 0.

Usage:
    python tools/outcome_merge.py --days 30
    python tools/outcome_merge.py --days 90 --symbols BTCUSDT,ETHUSDT
"""
from __future__ import annotations

import argparse
import datetime
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from _pa_sim_common import DEFAULT_SYMBOLS, fetch_account_data, make_client  # noqa: E402

from pa_agent.config.settings import load_settings  # noqa: E402
from pa_agent.feedback.outcome_store import (  # noqa: E402
    merge_all,
    symbols_scope,
    write_audit_json,
    write_outcome_csv,
)

TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--hours", type=int, default=0,
                    help="rolling window of the last N hours (overrides --days)")
    ap.add_argument("--symbols", default="", help="comma list; default whitelist + CSV")
    args = ap.parse_args()

    settings = load_settings()
    whitelist = list(getattr(settings.binance_usdm_testnet, "symbol_whitelist", None) or [])
    csv_dir = ROOT / "trade_records"
    symbols = symbols_scope(
        whitelist or None, csv_dir=csv_dir, default=list(DEFAULT_SYMBOLS)
    )
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        symbols = [s for s in symbols if s in wanted] or wanted

    if not getattr(settings.binance_usdm_testnet, "api_key", "") or             not getattr(settings.binance_usdm_testnet, "api_secret", ""):
        print("Testnet API key/secret missing: 无法拉取成交源, outcomes 保持为空。")
        return 0

    start_ms, _end = 0, 0
    try:
        if args.hours:
            end_ms = int(datetime.datetime.now(TZ8).timestamp() * 1000)
            start_ms = end_ms - args.hours * 3600 * 1000
        else:
            today = datetime.datetime.now(TZ8).replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int((today - datetime.timedelta(days=args.days - 1)).timestamp() * 1000)
    except OverflowError:
        pass
    fetch_start = start_ms - 24 * 3600 * 1000

    settings, client = make_client(settings)
    account = {"orders": {}, "user_trades": {}, "income": [], "positions": {}}
    skipped: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pa-outcome-") as td:
        cache = Path(td)
        for sym in symbols:
            try:
                fetch_account_data(client, [sym], fetch_start, cache)
            except Exception as exc:
                skipped.append(f"{sym}({exc.__class__.__name__})")
                continue
                continue
            orders = _load_json(cache / "orders.json").get(sym, [])
            fills = _load_json(cache / "user_trades.json").get(sym, [])
            income = _load_json(cache / "income.json")
            positions = _load_json(cache / "positions.json")
            if orders:
                account["orders"][sym] = orders
            if fills:
                account["user_trades"][sym] = fills
            account["income"].extend(income if isinstance(income, list) else [])
            if positions:
                account["positions"][sym] = positions
        symbols = [s for s in symbols if s not in {k.split("(")[0] for k in skipped}]
        if skipped:
            print("跳过无法拉取的币种:", ", ".join(skipped))
    rows, audit = merge_all(
        account,
        records_root=ROOT / "records" / "pending",
        csv_dir=csv_dir,
        symbols=symbols,
        days=args.days,
    )
    write_outcome_csv(rows, csv_dir / "outcomes.csv")
    audit_path = ROOT / "logs" / f"outcome_audit_{datetime.date.today():%Y%m%d}.json"
    write_audit_json(audit, audit_path)
    print(f"币种: {', '.join(symbols)}")
    print(f"决策记录: pending={audit['decisions_pending']} csv={audit['decisions_csv']}")
    print(f"归属: matched={audit['matched']} manual={audit['manual']} "
          f"missing={audit['missing_decision']} ambiguous={audit['ambiguous']}")
    print(f"新 OutcomeRows: {audit['rows_outcome']} (uid 幂等去重落盘)")
    print("income 对账:", audit["income_diff_by_symbol"])
    print("audit:", audit_path)
    return 0


def _load_json(path: Path) -> dict:
    import json

    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
