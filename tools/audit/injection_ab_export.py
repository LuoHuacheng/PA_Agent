#!/usr/bin/env python3
"""VergeX 移植文件（29/30）注入前后的 A/B 明细导出。

背景
----
2026-09-14 起 prompt_engineering/文件29-利弗莫尔趋势跟随移植.txt 与
文件30-三层评分趋势移植.txt 成为 stage2 常驻基础注入
（pa_agent/ai/prompt_assembler.py 的 STAGE2_BASE_PROMPT_TXT_FILES）。
本工具把 records/pending 的决策明细按"文件在场 / 不在场"分桶导出 CSV，
并按 record_file 左连接 trade_records/outcomes.csv 回填成交结局，
同时打印两桶的下单率 / 置信度 / prompt 体量对比。

口径与限制（详见 docs/vergex移植策略shadow验证清单.md）
--------------------------------------------------------
- 这是**前后对比**，不是随机对照：在场桶只能落在接线重启之后的样本。
- 成交账本由 tools/outcome_merge.py 追加；没跑过它时结局列会是空的。
- 异常记录（exception.type 非空）无 stage2 决策，统计时按 complete 过滤。

CLI
---
    python tools/audit/injection_ab_export.py
    python tools/audit/injection_ab_export.py --since 2026-09-13 --out logs/injection_ab_0916.csv
    python tools/audit/injection_ab_export.py --no-backfill
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TZ8 = timezone(timedelta(hours=8))

#: 判定"注入在场"的标记文件：两个都在场才算 post。改动接线时同步这里。
MARKER_FILES: tuple[str, ...] = (
    "文件29-利弗莫尔趋势跟随移植.txt",
    "文件30-三层评分趋势移植.txt",
)

_ORDER_TYPES = ("不下单", "限价单", "突破单", "市价单")

FIELDNAMES: tuple[str, ...] = (
    "bucket", "record_file", "ts_local", "symbol", "timeframe",
    "s1_cycle", "s1_direction", "s1_gate_result",
    "s2_cycle", "s2_direction", "bar_type", "always_in",
    "order_type", "order_direction", "entry_price", "stop_loss_price",
    "take_profit_price", "rr", "trade_confidence", "diagnosis_confidence",
    "estimated_win_rate", "terminal_outcome", "terminal_node",
    "execution_status", "gate_reasons", "exception_type", "exception_stage",
    "strategy_files_count", "stage2_prompt_chars", "usage_total_tokens",
    # 由 outcomes.csv 回填
    "filled", "close_reason", "outcome", "win_r", "net_usdt",
)

_OUTCOME_FIELDS = ("close_reason", "outcome", "win_r", "net_usdt")


def _chars(messages: object) -> int:
    if not isinstance(messages, list):
        return 0
    return sum(
        len(str(m.get("content") or "")) for m in messages if isinstance(m, dict)
    )


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rr(decision: dict) -> float | None:
    entry = _num(decision.get("entry_price"))
    stop = _num(decision.get("stop_loss_price"))
    target = _num(decision.get("take_profit_price"))
    if entry is None or stop is None or target is None or entry == stop:
        return None
    return round(abs(target - entry) / abs(entry - stop), 2)


def _iso_local(ms: object) -> str:
    value = _num(ms)
    if value is None or value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, TZ8).strftime("%Y-%m-%d %H:%M:%S")


def extract_row(record: dict, filename: str) -> dict:
    """把一份 records/pending 记录压成一行明细。纯函数，便于测试。"""
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    stage2 = record.get("stage2_decision")
    stage2 = stage2 if isinstance(stage2, dict) else {}
    decision = stage2.get("decision") if isinstance(stage2.get("decision"), dict) else {}
    diag = stage2.get("diagnosis_summary") if isinstance(stage2.get("diagnosis_summary"), dict) else {}
    bars = stage2.get("bar_analysis") if isinstance(stage2.get("bar_analysis"), dict) else {}
    terminal = stage2.get("terminal") if isinstance(stage2.get("terminal"), dict) else {}
    stage1 = record.get("stage1_diagnosis") if isinstance(record.get("stage1_diagnosis"), dict) else {}
    execution = stage2.get("execution") if isinstance(stage2.get("execution"), dict) else {}
    exception = record.get("exception") if isinstance(record.get("exception"), dict) else {}
    usage = record.get("usage_total") if isinstance(record.get("usage_total"), dict) else {}

    names = set(record.get("strategy_files_used") or [])
    reasons = execution.get("gate_reasons")
    rr = _rr(decision)
    row = {
        "bucket": "post_injection" if set(MARKER_FILES) <= names else "pre_injection",
        "record_file": filename,
        "ts_local": _iso_local(meta.get("timestamp_local_ms")),
        "symbol": meta.get("symbol", ""),
        "timeframe": meta.get("timeframe", ""),
        "s1_cycle": stage1.get("cycle_position", ""),
        "s1_direction": stage1.get("direction", ""),
        "s1_gate_result": stage1.get("gate_result", ""),
        "s2_cycle": diag.get("cycle_position", ""),
        "s2_direction": diag.get("direction", ""),
        "bar_type": bars.get("bar_type", ""),
        "always_in": bars.get("always_in", ""),
        "order_type": decision.get("order_type", "") or "",
        "order_direction": decision.get("order_direction") or "",
        "entry_price": decision.get("entry_price", ""),
        "stop_loss_price": decision.get("stop_loss_price", ""),
        "take_profit_price": decision.get("take_profit_price", ""),
        "rr": rr if rr is not None else "",
        "trade_confidence": decision.get("trade_confidence", ""),
        "diagnosis_confidence": decision.get("diagnosis_confidence", ""),
        "estimated_win_rate": decision.get("estimated_win_rate", ""),
        "terminal_outcome": terminal.get("outcome", ""),
        "terminal_node": terminal.get("node_id", ""),
        "execution_status": execution.get("status", ""),
        "gate_reasons": "; ".join(str(r) for r in reasons) if isinstance(reasons, list) else "",
        "exception_type": exception.get("type", ""),
        "exception_stage": exception.get("stage", ""),
        "strategy_files_count": len(names),
        "stage2_prompt_chars": _chars(record.get("stage2_messages")),
        "usage_total_tokens": usage.get("total_tokens", ""),
    }
    row.update({field: "" for field in _OUTCOME_FIELDS})
    row["filled"] = ""
    return row


def load_rows(pending_dir: Path, *, since: str) -> list[dict]:
    """读取 since（含）之后的 pending 记录，按文件名升序。"""
    rows: list[dict] = []
    for path in sorted(pending_dir.glob("*.json")):
        if path.name < since:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            rows.append(extract_row(record, path.name))
    return rows


def load_outcomes(csv_path: Path) -> dict[str, dict]:
    """outcomes.csv -> {record_file: row}。缺失/不可读返回 {}。"""
    if not csv_path.exists():
        return {}
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            return {
                str(row.get("record_file") or ""): row
                for row in csv.DictReader(fh)
                if row.get("record_file")
            }
    except OSError:
        return {}


def backfill_outcomes(rows: list[dict], outcomes: dict[str, dict]) -> int:
    """按 record_file 左连接 outcomes，回填结局列；返回命中行数。"""
    hits = 0
    for row in rows:
        match = outcomes.get(row.get("record_file", ""))
        if not match:
            continue
        hits += 1
        row["filled"] = "yes"
        for field in _OUTCOME_FIELDS:
            value = match.get(field)
            row[field] = "" if value is None else value
    return hits


def _num_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    return _num(value)


def summarize(rows: list[dict]) -> dict:
    """分桶统计。完整分析 = 无 exception 且 order_type 合法。"""
    stats: dict[str, dict] = {}
    for bucket in ("post_injection", "pre_injection"):
        group = [r for r in rows if r.get("bucket") == bucket]
        complete = [
            r for r in group
            if not r.get("exception_type") and r.get("order_type") in _ORDER_TYPES
        ]
        orders = [r for r in complete if r.get("order_type") != "不下单"]
        conf = [c for c in (_num_or_none(r.get("trade_confidence")) for r in complete) if c is not None]
        chars = [_num_or_none(r.get("stage2_prompt_chars")) or 0 for r in complete]
        files = [_num_or_none(r.get("strategy_files_count")) or 0 for r in complete]
        stats[bucket] = {
            "records": len(group),
            "complete": len(complete),
            "exceptions": len(group) - len(complete),
            "orders": len(orders),
            "order_rate": (len(orders) / len(complete)) if complete else None,
            "conf_median": st.median(conf) if conf else None,
            "conf_mean": st.mean(conf) if conf else None,
            "prompt_chars_mean": st.mean(chars) if chars else None,
            "files_mean": st.mean(files) if files else None,
        }
    return stats


def _fmt(value: object, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def print_summary(stats: dict) -> None:
    header = (
        f"{'桶':14s} {'记录':>5s} {'完整':>5s} {'异常':>5s} {'下单':>5s} "
        f"{'下单率':>7s} {'conf中位':>8s} {'conf均':>7s} {'s2字符均':>9s} {'文件数均':>8s}"
    )
    print(header)
    for bucket in ("post_injection", "pre_injection"):
        s = stats[bucket]
        rate = "-" if s["order_rate"] is None else f"{s['order_rate'] * 100:.1f}%"
        print(
            f"{bucket:14s} {s['records']:5d} {s['complete']:5d} {s['exceptions']:5d} "
            f"{s['orders']:5d} {rate:>7s} {_fmt(s['conf_median'], 0):>8s} "
            f"{_fmt(s['conf_mean']):>7s} {_fmt(s['prompt_chars_mean'], 0):>9s} "
            f"{_fmt(s['files_mean']):>8s}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pending-dir", default=str(ROOT / "records" / "pending"))
    ap.add_argument("--outcomes", default=str(ROOT / "trade_records" / "outcomes.csv"))
    ap.add_argument("--since", default="2026-09-13", help="文件名起始日期(含), 默认 2026-09-13")
    ap.add_argument("--out", default="", help="输出 CSV 路径; 默认 logs/injection_ab_<今日>.csv")
    ap.add_argument("--no-backfill", action="store_true", help="不连接 outcomes.csv")
    args = ap.parse_args(argv)

    rows = load_rows(Path(args.pending_dir), since=args.since)
    if not rows:
        print("没有匹配的记录:", args.pending_dir, "since", args.since)
        return 0

    hits = 0
    if not args.no_backfill:
        hits = backfill_outcomes(rows, load_outcomes(Path(args.outcomes)))

    out_path = Path(args.out) if args.out else (
        ROOT / "logs" / f"injection_ab_{datetime.now(TZ8).strftime('%Y%m%d')}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(FIELDNAMES), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    stats = summarize(rows)
    print_summary(stats)
    print()
    print("明细:", out_path, "| 行数:", len(rows), "| outcomes 回填命中:", hits)
    if not args.no_backfill and hits == 0:
        print("提示: 结局列全空 = 该窗口没有对应成交; 先跑 tools/outcome_merge.py 补账本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
