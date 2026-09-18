"""观察窗口进度: 按 docs/正期望验证规程.md §2.13 给结论.

数的是**最近一次配置变更之后**的: 分析次数 / 入场尝试 / 挂单数 / 成交数,
然后套用 §2.13 的分辨规则(2 天内 >=2 笔过闸门成交 => 乐观情景)。

用法(仓库根):  PYTHONPATH=. .venv/bin/python scripts/window_progress.py
"""
from __future__ import annotations

import csv
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_gate import describe, is_entry_attempt, load_records
from tools.expectancy_report import GATE_START, parse_cutoff

TZ8 = timezone(timedelta(hours=8))

#: §2.13 的分辨窗口: 60 分钟节拍(120 次分析/天)下 3 天起功效可接受(0.69), 5 天最佳(0.91)。
MIN_DAYS = 3


def verdict(fills, attempts, days):
    """§2.13 的判定规则 -> (情景, 说明).

    days 至少 MIN_DAYS 天才算"可分辨"; 不足时返回"观察中"。
    """
    if days < MIN_DAYS:
        return "观察中", f"才 {days:.2f} 天, {MIN_DAYS} 天后才可分辨"
    if fills >= 2:
        return "乐观", f"{days:.1f} 天 {fills} 笔过闸门成交 -> 实盘口径(约 0.80 笔/天)"
    if fills == 0 and attempts == 0:
        return "瓶颈不在节拍", "0 成交且 0 入场尝试 -> 查模型/闸门, 不要叠加变更"
    return "悲观", f"{days:.1f} 天仅 {fills} 笔过闸门成交 -> 回放口径(约 0.136 笔/天)"


def count_orders(ledger_glob, cutoff_ms):
    n = 0
    for path in glob.glob(ledger_glob):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(rec.get("recorded_ms") or 0) >= cutoff_ms and str(rec.get("family")) == "pa-entry":
                    n += 1
    return n


def count_fills(outcomes, cutoff_ms):
    with open(outcomes, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    n = 0
    for r in rows:
        try:
            ts = float(r.get("ts_close") or "")
        except (TypeError, ValueError):
            continue
        if ts >= cutoff_ms:
            n += 1
    return n


def main():
    cutoff = parse_cutoff(GATE_START)
    now_ms = datetime.now(tz=TZ8).timestamp() * 1000
    days = (now_ms - cutoff) / 86400_000

    records = load_records("records/pending/*.json", cutoff)
    attempts = 0
    for path, doc in records:
        info = describe(path, doc)
        if is_entry_attempt(info["order_type"], info["direction"]):
            attempts += 1
    orders = count_orders("records/orders/*.jsonl", cutoff)
    fills = count_fills("trade_records/outcomes.csv", cutoff)

    scen, why = verdict(fills, attempts, days)
    print(f"窗口起点(最近一次配置变更): {GATE_START}  (已 {days:.2f} 天)")
    print(f"分析(记录)={len(records)}  入场尝试={attempts}  入场挂单={orders}  成交={fills}")
    print(f"判定: [{scen}] {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
