"""Rule-grouped outcome statistics (Task A3).

Groups outcome rows by strategy file or by (cycle_position, direction) and
computes base rates for the prompt injector and the calibration report:

- win_rate over closed rows only (net==0 already classified as loss upstream);
- avg_r / expectancy_r as the mean realized win_r over closed rows that carry
  a risk-defined R (wins positive, losses negative — the per-trade edge);
- avg_confidence over closed rows with a confidence value;
- metrics are None (never fabricated) below min_samples closed rows.

A multi-file decision counts once per file group it routed.
"""
from __future__ import annotations

import statistics
from typing import Any

_MISSING_FILE_KEY = "<none>"
_CLOSED = ("win", "loss")


def _group_keys(group_by: tuple[str, ...], row: dict) -> list[str]:
    if group_by == ("strategy_file",):
        files = row.get("strategy_files") or ()
        return [str(f) if str(f) else _MISSING_FILE_KEY for f in files]
    values = []
    for field in group_by:
        values.append(str(row.get(field) or ""))
    return ["|".join(values)]


def _stats_for(rows: list[dict], min_samples: int) -> dict[str, Any]:
    closed = [r for r in rows if r.get("outcome") in _CLOSED]
    n = len(closed)
    n_open = sum(1 for r in rows if r.get("outcome") == "open")
    wins = sum(1 for r in closed if r.get("outcome") == "win")
    confs = [float(r["conf"]) for r in closed if r.get("conf") is not None]
    rs = [float(r["win_r"]) for r in closed if r.get("win_r") is not None]
    enough = n >= min_samples and n > 0
    return {
        "n": n,
        "open_n": n_open,
        "win_rate": (wins / n) if enough else None,
        "avg_r": (statistics.fmean(rs) if rs and enough else None),
        "expectancy_r": (statistics.fmean(rs) if rs and enough else None),
        "avg_confidence": (statistics.fmean(confs) if confs and enough else None),
    }


def build_group_stats(
    rows: list[dict],
    *,
    group_by: tuple[str, ...] = ("strategy_file",),
    min_samples: int = 10,
) -> dict[str, dict]:
    """Group outcome rows and compute base rates; {} when no rows."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        for key in _group_keys(group_by, row):
            buckets.setdefault(key, []).append(row)
    out: dict[str, dict] = {}
    for key in sorted(buckets):
        stats = _stats_for(buckets[key], min_samples)
        stats["key"] = key
        out[key] = stats
    return out
