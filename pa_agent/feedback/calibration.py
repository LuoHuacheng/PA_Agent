"""Calibration math shared by the report tool and tests (Task A5).

Realization: win when net > 0, loss otherwise (net == 0 counts as loss,
Q3 decision). Confidence p = conf/100 is scored with the Brier score against
the 0/1 realization. Buckets are 5-point floors; the suggested threshold is
the lowest 5-point boundary whose bucket win rate reaches the boundary value
itself (i.e. the model is calibrated at least up to that point).
"""
from __future__ import annotations

from typing import Any


def conf_bucket(conf: int) -> int:
    return int(conf) // 5 * 5


def brier_score(pairs: list[tuple[float, int]]) -> float | None:
    """Mean squared error of confidence (0..100) against 0/1 realizations."""
    if not pairs:
        return None
    n = len(pairs)
    return sum(((conf / 100.0) - y) ** 2 for conf, y in pairs) / n


def bucket_table(pairs: list[tuple[float, int]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for conf, y in pairs:
        lo = conf_bucket(int(conf))
        b = out.setdefault(lo, {"n": 0, "wins": 0, "conf_sum": 0.0})
        b["n"] += 1
        b["wins"] += 1 if y else 0
        b["conf_sum"] += float(conf)
    for _lo, b in out.items():
        b["win_rate"] = b["wins"] / b["n"] if b["n"] else None
        b["avg_conf"] = b["conf_sum"] / b["n"] if b["n"] else None
    return out


def suggest_threshold(
    table: dict[int, dict[str, Any]], min_n: int = 5
) -> int | None:
    """Lowest 5-pt boundary with enough samples and win_rate >= boundary/100."""
    candidates = [
        lo
        for lo, b in sorted(table.items())
        if b["n"] >= min_n
        and b["win_rate"] is not None
        and b["win_rate"] >= lo / 100.0
    ]
    return candidates[0] if candidates else None
