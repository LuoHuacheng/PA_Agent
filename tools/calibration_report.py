#!/usr/bin/env python3
"""Confidence calibration report over the accumulated outcomes.csv (Phase A).

Reads trade_records/outcomes.csv produced by the feedback outcome store and
prints / writes:
  - overall and per-strategy/cycle base rates (win rate, avg R, confidence);
  - confidence buckets vs realized win rate plus the Brier score;
  - a suggested decision_confidence_threshold and an up/keep/down hint.

The sample is CONDITIONED: it only contains decisions that passed the order
opportunity gate and were auto-executed on the Testnet (see review R9).

Usage:
    python tools/calibration_report.py                     # default 30d
    python tools/calibration_report.py --days 90 --min-samples 20
    python tools/calibration_report.py --out report.txt
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.feedback.calibration import brier_score, bucket_table, suggest_threshold
from pa_agent.feedback.rule_stats import build_group_stats

TZ8 = timezone(timedelta(hours=8))
DEFAULT_OUTCOMES = ROOT / "trade_records" / "outcomes.csv"
GROUP_MODES = [
    ("strategy_file", "策略文件"),
    ("cycle_direction", "周期/方向"),
]
_FIELDS = {
    "strategy_file": ("strategy_file",),
    "cycle_direction": ("cycle_position", "diag_direction"),
}


def _parse_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_files(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        return [str(f) for f in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def load_rows(path: Path, days: int) -> list[dict]:
    if not path.exists():
        return []
    now_ms = datetime.now(TZ8).timestamp() * 1000
    boundary = now_ms - days * 86400_000
    rows: list[dict] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            ts = _parse_float(raw.get("ts_open"))
            if ts is not None and ts < boundary:
                continue
            outcome = str(raw.get("outcome") or "")
            if outcome not in ("win", "loss", "open"):
                continue
            rows.append({
                "strategy_files": tuple(_parse_files(raw.get("strategy_files"))),
                "cycle_position": str(raw.get("cycle_position") or ""),
                "diag_direction": str(raw.get("diag_direction") or ""),
                "conf": _parse_float(raw.get("conf")),
                "outcome": outcome,
                "win_r": _parse_float(raw.get("win_r")),
                "source": str(raw.get("source") or ""),
            })
    return rows


def _closed(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["outcome"] in ("win", "loss")]


def _render_table(name: str, rows: list[dict], min_samples: int) -> list[str]:
    lines = [f"[{name}]"]
    fields = _FIELDS[name]
    groups = build_group_stats(rows, group_by=fields, min_samples=min_samples)
    items = sorted(groups.values(), key=lambda g: (g["n"], g.get("avg_r") or 0.0),
                   reverse=True)
    if not items:
        lines.append("  (样本不足或无匹配组)")
    for g in items:
        wr = "-" if g["win_rate"] is None else f"{g['win_rate'] * 100:.0f}%"
        avg = "-" if g["avg_r"] is None else f"{g['avg_r']:+.2f}R"
        conf = "-" if g["avg_confidence"] is None else f"{g['avg_confidence']:.0f}"
        lines.append(
            f"  {g['key']}: 平仓 {g['n']} 持仓 {g['open_n']} "
            f"胜率 {wr} 均盈亏 {avg} 均置信 {conf}"
        )
    return lines


def render_report(
    rows: list[dict], *, days: int, min_samples: int, current_threshold: int
) -> str:
    closed = _closed(rows)
    out: list[str] = []
    out.append("=" * 60)
    out.append("PA Agent 置信度校准报告")
    out.append(f"窗口: 近 {days} 天 | 样本口径: 通过机会闸门且自动成交(条件样本, R9)")
    out.append(f"共 {len(rows)} 行(平仓 {len(closed)}, 持仓 {len(rows) - len(closed)})")
    src = {}
    for r in rows:
        src[r["source"]] = src.get(r["source"], 0) + 1
    if src:
        out.append("来源: " + ", ".join(f"{k}={v}" for k, v in sorted(src.items())))
    wins = sum(1 for r in closed if r["outcome"] == "win")
    losses = len(closed) - wins
    wr = (wins / len(closed) * 100) if closed else None
    out.append(f"总体: 胜 {wins} / 负 {losses} / 胜率 "
               + (f"{wr:.1f}%" if wr is not None else "-"))
    pairs = [(float(r["conf"]), 1 if r["outcome"] == "win" else 0)
             for r in closed if r["conf"] is not None]
    brier = brier_score(pairs)
    if brier is not None:
        out.append(f"Brier score(conf/100 vs 实际): {brier:.4f} "
                   f"({'偏乐观' if brier > 0.25 else '未见系统性乐观'})")
    for name, _label in GROUP_MODES:
        out.extend(_render_table(name, rows, min_samples))
    table = bucket_table(pairs)
    out.append("[置信度分桶 (5 点步长, 已平仓)]")
    for lo in sorted(table):
        b = table[lo]
        bw = "-" if b["win_rate"] is None else f"{b['win_rate'] * 100:.0f}%"
        out.append(f"  conf {lo}-{lo + 4}: n={b['n']} 胜率={bw} "
                   f"均置信={b['avg_conf']:.0f}")
    suggestion = suggest_threshold(table, min_n=min_samples)
    if suggestion is None:
        out.append("阈值建议: 样本不足或未达到任一校准边界(维持现状观察)")
    elif suggestion > current_threshold:
        out.append(f"阈值建议: 上调 decision_confidence_threshold 至 {suggestion}"
                   f"(当前 {current_threshold})")
    elif suggestion < current_threshold:
        out.append(f"阈值建议: 可考虑下调至 {suggestion} (当前 {current_threshold})")
    else:
        out.append(f"阈值建议: 保持 {current_threshold}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-samples", type=int, default=10)
    ap.add_argument("--current-threshold", type=int, default=40)
    ap.add_argument("--outcomes", type=Path, default=DEFAULT_OUTCOMES)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rows = load_rows(args.outcomes, args.days)
    if not rows:
        msg = (
            f"outcomes.csv 为空或不存在: {args.outcomes}\n"
            "请先运行结果合并(带成交源)积累样本; dry_run/disabled 期间为空属正常。"
        )
        print(msg)
        return 0
    report = render_report(
        rows,
        days=args.days,
        min_samples=args.min_samples,
        current_threshold=args.current_threshold,
    )
    print(report, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print("written:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
