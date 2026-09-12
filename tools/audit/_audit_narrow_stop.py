# -*- coding: utf-8 -*-
"""Audit: stop-gap distribution of real order signals across trade records."""
import csv, glob, os
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

rows = []
for path in glob.glob("trade_records/*_15m.csv"):
    sym = os.path.basename(path).replace("_15m.csv", "")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            r["_symbol"] = sym
            rows.append(r)
print("总行数:", len(rows))
if rows:
    print("时间范围:", min(r["record_time"] for r in rows), "->", max(r["record_time"] for r in rows))
print("order_type:", dict(Counter(r.get("order_type", "") for r in rows)))
print("terminal_outcome:", dict(Counter(r.get("terminal_outcome", "") for r in rows)))

def dec(x):
    try:
        return Decimal(str(x).strip())
    except Exception:
        return None

signals = []
for r in rows:
    ot = r.get("order_type", "")
    if ot not in ("限价单", "市价单"):
        continue
    e, s = dec(r.get("entry_price")), dec(r.get("stop_loss_price"))
    if e is None or s is None or e <= 0:
        continue
    gap = abs(s - e) / e * 100
    r["_gap_pct"] = float(gap)
    signals.append(r)
signals.sort(key=lambda x: x["record_time"])
print("下单信号数:", len(signals))
narrow = [r for r in signals if r["_gap_pct"] < 0.45]
print("窄止损(<0.45%%):", len(narrow), "(%.0f%%)" % (len(narrow) / max(1, len(signals)) * 100))
print("gap 分布: <0.2:", sum(1 for r in signals if r["_gap_pct"] < 0.2),
      "| 0.2-0.45:", sum(1 for r in signals if 0.2 <= r["_gap_pct"] < 0.45),
      "| 0.45-1:", sum(1 for r in signals if 0.45 <= r["_gap_pct"] < 1),
      "| 1-2:", sum(1 for r in signals if 1 <= r["_gap_pct"] < 2),
      "| >2:", sum(1 for r in signals if r["_gap_pct"] >= 2))

def parse(t):
    try:
        return datetime.fromisoformat(t.replace("Z", ""))
    except Exception:
        return None

cutoff = datetime.now() - timedelta(days=7)
recent = [r for r in signals if (parse(r["record_time"]) or datetime.min) > cutoff]
narrow_recent = [r for r in recent if r["_gap_pct"] < 0.45]
print("近7天下单信号:", len(recent), "| 其中窄止损:", len(narrow_recent))
for r in narrow_recent[-40:]:
    print(" | ".join([
        r["record_time"][:16], r["_symbol"], r["order_type"], str(r["order_direction"]),
        "gap=%.3f%%" % r["_gap_pct"], "e=" + str(r["entry_price"]), "s=" + str(r["stop_loss_price"]),
        "win=" + str(r.get("estimated_win_rate")), "outcome=" + str(r.get("terminal_outcome")),
        "s2=" + str(r.get("s2_always_in")), "prev=" + str(r.get("prev_plan_relation", ""))[:10],
    ]))
