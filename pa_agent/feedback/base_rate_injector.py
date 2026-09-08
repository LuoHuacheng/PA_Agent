"""Base-rate prompt injector (Task A4).

Reads the accumulated outcomes.csv produced by outcome_store, computes
strategy/cycle base rates and renders a compact statistics block appended to
the stage-2 user prompt. Only factual numbers are rendered — no advice, no
"no data" filler; groups below min_samples stay hidden entirely.

The block is dynamic and sits at the END of the user turn (right before the
final output reminder) so the byte-identical static prefix keeps KV-cache
hits.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pa_agent.feedback.rule_stats import build_group_stats

OUTCOMES_CSV_PATH = Path("trade_records") / "outcomes.csv"

_MODE_TO_FIELDS = {
    "strategy_file": ("strategy_file",),
    "cycle_direction": ("cycle_position", "diag_direction"),
}


def render_base_rate_block(
    groups: dict[str, dict],
    *,
    max_lines: int = 6,
    days: int = 30,
) -> str:
    """Render stats groups as a compact markdown block ('' when nothing)."""
    items = [g for g in groups.values() if g.get("win_rate") is not None]
    if not items:
        return ""
    items.sort(key=lambda g: (g.get("n") or 0, g.get("avg_r") or 0.0), reverse=True)
    lines = [f"## 历史胜率先验(程序统计 近{days}天)"]
    for g in items[:max_lines]:
        key = str(g.get("key") or "")
        if not key:
            continue
        segs = [f"平仓 {g['n']}", f"持仓 {g.get('open_n') or 0}",
                f"胜率 {g['win_rate'] * 100:.0f}%"]
        if g.get("avg_r") is not None:
            segs.append(f"均盈亏 {g['avg_r']:+.2f}R")
        if g.get("avg_confidence") is not None:
            segs.append(f"均置信 {g['avg_confidence']:.0f}")
        lines.append(f"- {key}: " + " ".join(segs))
    return "\n".join(lines)


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


def _load_rows(outcomes_path: Path) -> list[dict]:
    with open(outcomes_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for row in reader:
            if str(row.get("outcome") or "") not in ("win", "loss", "open"):
                continue
            rows.append({
                "strategy_files": tuple(_parse_files(row.get("strategy_files"))),
                "cycle_position": str(row.get("cycle_position") or ""),
                "diag_direction": str(row.get("diag_direction") or ""),
                "conf": _parse_float(row.get("conf")),
                "outcome": str(row.get("outcome") or ""),
                "win_r": _parse_float(row.get("win_r")),
            })
    return rows


def load_stats_block(
    feedback: Any,
    *,
    outcomes_path: Path | None = None,
) -> str:
    """Full block for stage-2 injection; '' when disabled or no data yet."""
    if not getattr(feedback, "enabled", False):
        return ""
    path = outcomes_path or OUTCOMES_CSV_PATH
    if not path.exists():
        return ""
    try:
        rows = _load_rows(path)
        if not rows:
            return ""
        min_samples = int(getattr(feedback, "min_samples", 10) or 10)
        days = int(getattr(feedback, "days", 30) or 30)
        max_lines = int(getattr(feedback, "max_prompt_lines", 6) or 6)
        modes = list(getattr(feedback, "group_bys", None) or ["strategy_file"])
        merged: dict[str, dict] = {}
        for mode in modes:
            fields = _MODE_TO_FIELDS.get(str(mode))
            if fields is None:
                continue
            groups = build_group_stats(rows, group_by=fields, min_samples=min_samples)
            for key, stats in groups.items():
                merged.setdefault(f"{mode}:{key}", dict(stats))
        return render_base_rate_block(merged, max_lines=max_lines, days=days)
    except (OSError, ValueError, TypeError):
        return ""
