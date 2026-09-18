"""Local order ledger: remember every order we place after the exchange forgets it.

Why this exists (measured 2026-09-18)
-------------------------------------
* /fapi/v1/allOrders answered with orders from 09-11 14:00 onward only, for a
  14-day request: the listing is bounded to roughly a week, so any order older
  than that is gone.
* Consumed conditional (algo) orders have no queryable history at all:
  /fapi/v1/algoOrders and /fapi/v1/historicalOrders both answer HTTP 404 on
  testnet. 28 of 92 closing fills in the window referenced an orderId that no
  endpoint could resolve.
* POST /fapi/v1/algoOrder answers {"code":200,"msg":"success"} -- the exchange
  never hands back the algoId it assigned.

Consequence: attribution of an exit to "the protective stop" vs "an active
close" died for anything older than a week (only 16 of 92 closed positions had a
resolvable closing order). This module records one JSONL line per placement, so
that question stays answerable forever, and offers a time-corridor fallback for
the algo fills whose orderId cannot be looked up.

Rules
-----
* never raise: a ledger write must not be able to fail a trade;
* append-only JSONL, one file per local day, so a bad line costs one day;
* store the plan inputs next to the ids, so cid_variants can be replayed.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))
DEFAULT_LEDGER_DIR = Path("records/orders")

#: POST paths whose successful result is an order we should remember.
ORDER_POST_PATHS = ("/fapi/v1/order", "/fapi/v1/algoOrder")

#: clientOrderId prefixes this repo stamps (longest first, so pa-entry and
#: pa-rollback cannot shadow each other).
CID_FAMILIES = (
    ("pa-entry", "pa-entry"),
    ("pa-rollback", "pa-rollback"),
    ("pa-sl", "pa-sl"),
    ("pa-tp", "pa-tp"),
)

#: families that reduce or close an existing position.
EXIT_FAMILIES = frozenset({"pa-sl", "pa-tp", "pa-rollback"})


def order_family(client_id: object) -> str:
    """Map a clientOrderId (or clientAlgoId) to its family name."""
    cid = str(client_id or "")
    for prefix, family in CID_FAMILIES:
        if cid.startswith(prefix):
            return family
    return "external" if cid else "unknown"


def ledger_file(ts_ms: int | None = None, *, ledger_dir: Path | str | None = None) -> Path:
    """Path of the ledger file covering *ts_ms* (one file per local day)."""
    base = Path(ledger_dir) if ledger_dir is not None else DEFAULT_LEDGER_DIR
    stamp = ts_ms if ts_ms is not None else time.time() * 1000
    moment = datetime.fromtimestamp(stamp / 1000, TZ)
    return base / f"orders-{moment.strftime('%Y%m%d')}.jsonl"


#: response/request keys copied into a ledger row.
_ORDER_KEYS = (
    "orderId", "algoId", "clientOrderId", "clientAlgoId", "symbol", "side", "type",
    "status", "origQty", "executedQty", "price", "avgPrice", "stopPrice",
    "triggerPrice", "reduceOnly", "closePosition", "time", "updateTime",
)


def record_order(
    symbol: str,
    result: object,
    *,
    request: dict | None = None,
    source: str = "",
    ledger_dir: Path | str | None = None,
    now_ms: int | None = None,
) -> bool:
    """Append one placement to the daily ledger; True when a line was written.

    Never raises: an order that reached the exchange is a fact even when the
    ledger cannot record it.
    """
    try:
        response = result if isinstance(result, dict) else {}
        req = request if isinstance(request, dict) else {}
        row: dict = {
            "recorded_ms": int(now_ms if now_ms is not None else time.time() * 1000),
            "symbol": str(symbol or response.get("symbol") or req.get("symbol") or ""),
            "source": str(source or ""),
        }
        for key in _ORDER_KEYS:
            value = response.get(key, req.get(key))
            if value is not None and value != "":
                row[key] = value
        cid = row.get("clientOrderId") or row.get("clientAlgoId") or ""
        row["family"] = order_family(cid)
        if not row["symbol"] and not cid:
            return False
        path = ledger_file(row["recorded_ms"], ledger_dir=ledger_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # a ledger write must never fail a trade
        logger.warning("order ledger write failed for %s: %s", symbol, exc)
        return False


def ledger_paths(
    *, ledger_dir: Path | str | None = None, since_ms: int | None = None
) -> list[Path]:
    """Ledger files, oldest first; *since_ms* drops the days that cannot matter."""
    base = Path(ledger_dir) if ledger_dir is not None else DEFAULT_LEDGER_DIR
    if not base.is_dir():
        return []
    files = sorted(base.glob("orders-*.jsonl"))
    if since_ms is not None:
        cut = ledger_file(since_ms, ledger_dir=base).name
        files = [fp for fp in files if fp.name >= cut]
    return files


def iter_ledger(*, ledger_dir: Path | str | None = None, since_ms: int | None = None) -> Iterator[dict]:
    """Yield every ledger row; unreadable files and bad lines are skipped."""
    for path in ledger_paths(ledger_dir=ledger_dir, since_ms=since_ms):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("order ledger unreadable (%s): %s", path, exc)
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _cid_of(row: dict) -> str:
    return str(row.get("clientOrderId") or row.get("clientAlgoId") or "")


def index_by_order_id(
    *, ledger_dir: Path | str | None = None, since_ms: int | None = None
) -> dict[tuple[str, int], str]:
    """(symbol, orderId) -> clientOrderId, from local placements."""
    out: dict[tuple[str, int], str] = {}
    for row in iter_ledger(ledger_dir=ledger_dir, since_ms=since_ms):
        oid, sym, cid = _int_or_none(row.get("orderId")), str(row.get("symbol") or ""), _cid_of(row)
        if oid is None or not sym or not cid:
            continue
        out[(sym, oid)] = cid
    return out


def exit_placements(
    *, ledger_dir: Path | str | None = None, since_ms: int | None = None
) -> dict[str, list[tuple[int, str]]]:
    """symbol -> sorted [(recorded_ms, clientOrderId)] for exit-family orders."""
    out: dict[str, list[tuple[int, str]]] = {}
    for row in iter_ledger(ledger_dir=ledger_dir, since_ms=since_ms):
        if row.get("family") not in EXIT_FAMILIES:
            continue
        sym, cid, ts = str(row.get("symbol") or ""), _cid_of(row), _int_or_none(row.get("recorded_ms"))
        if not sym or not cid or ts is None:
            continue
        out.setdefault(sym, []).append((ts, cid))
    for rows in out.values():
        rows.sort()
    return out


def attribute_exit(
    symbol: str,
    ts_ms: int,
    placements: dict[str, list[tuple[int, str]]],
    *,
    max_age_ms: int = 24 * 3600 * 1000,
) -> str:
    """ClientOrderId of the exit-family placement that most likely produced a fill.

    The exchange never returns the algoId of a conditional order, so a fill from a
    consumed algo stop carries an orderId that nothing can resolve. The placement
    that produced it is still in the ledger: take the latest exit-family order
    placed on that symbol at or before the fill, within *max_age_ms*.

    A stop and a take-profit can rest at the same time, so the choice between two
    same-position legs is a guess -- which is enough to answer "did our exit logic
    act, or did a protective order fire", the question the callers ask. Price vs
    plan still decides which leg it was.
    """
    best = ""
    for recorded, cid in placements.get(symbol) or ():
        if recorded > ts_ms:
            break
        if ts_ms - recorded <= max_age_ms:
            best = cid
    return best


def merge_into_orders(
    orders_by_symbol: dict[str, list[dict]],
    *,
    ledger_dir: Path | str | None = None,
    since_ms: int | None = None,
) -> int:
    """Fill gaps in an /fapi/v1/allOrders shaped mapping from the ledger.

    The exchange listing wins whenever it knows an orderId (it also carries
    fields this ledger does not), so only unseen orderIds are added. Returns the
    number of rows added.
    """
    added = 0
    for row in iter_ledger(ledger_dir=ledger_dir, since_ms=since_ms):
        sym, oid, cid = str(row.get("symbol") or ""), _int_or_none(row.get("orderId")), _cid_of(row)
        bucket = orders_by_symbol.get(sym)
        if bucket is None or oid is None or not cid:
            continue
        if any(_int_or_none(order.get("orderId")) == oid for order in bucket):
            continue
        bucket.append(
            {
                "orderId": oid,
                "clientOrderId": cid,
                "time": row.get("recorded_ms"),
                "ledger": True,
            }
        )
        added += 1
    return added
