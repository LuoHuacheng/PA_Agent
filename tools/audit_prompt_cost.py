#!/usr/bin/env python3
"""Prompt cost audit over recorded analyses (Phase C, Task C1).

Reads records/pending/*.json and reports per-stage token usage (as billed:
prompt/completion, cached share), per-section static rule-file sizes, and
duplicated long text spans between prompt blocks (dedupe candidates for C2).

Usage:
    python tools/audit_prompt_cost.py                    # newest 200 records
    python tools/audit_prompt_cost.py --limit 500
    python tools/audit_prompt_cost.py --days 3
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.ai.token_counter import estimate_tokens  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
RECORDS_DIR = ROOT / "records" / "pending"
PROMPT_DIR = ROOT / "prompt_engineering"
_MIN_OVERLAP_TOKENS = 120


def _record_time_ms(fp: Path) -> int:
    parts = fp.name[:-5].split("_")
    if len(parts) >= 3:
        try:
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H-%M-%S")
            return int(dt.replace(tzinfo=TZ8).timestamp() * 1000)
        except ValueError:
            pass
    return 0


def _usage_of(resp: dict | None) -> dict:
    if not isinstance(resp, dict):
        return {}
    u = resp.get("usage") or {}
    return u if isinstance(u, dict) else {}


def _stage_stats(records: list[dict]) -> dict:
    rows = {"stage1_prompt": [], "stage1_cached_pct": [],
            "stage2_prompt": [], "stage2_cached_pct": [],
            "completion": []}
    for raw in records:
        u1 = _usage_of(raw.get("stage1_response"))
        u2 = _usage_of(raw.get("stage2_response"))
        for key, u in (("stage1", u1), ("stage2", u2)):
            prompt = int(u.get("prompt_tokens") or 0)
            cached = int(u.get("cached_prompt_tokens") or 0)
            if prompt > 0:
                rows[f"{key}_prompt"].append(prompt)
                rows[f"{key}_cached_pct"].append(100.0 * cached / prompt)
        comp = int(u1.get("completion_tokens") or 0) + int(u2.get("completion_tokens") or 0)
        if comp:
            rows["completion"].append(comp)
    out = {}
    for key, vals in rows.items():
        if not vals:
            out[key] = None
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
        out[key] = {"n": n, "median": round(median, 1),
                    "mean": round(sum(vals_sorted) / n, 1), "max": vals_sorted[-1]}
    return out


def _file_token_sizes() -> list[tuple[str, int]]:
    sizes = []
    if not PROMPT_DIR.is_dir():
        return sizes
    for fp in sorted(PROMPT_DIR.glob("*.txt")):
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        sizes.append((fp.name, estimate_tokens([{"role": "user", "content": text}])))
    return sizes


def _long_span_overlap(a: str, b: str, min_chars: int) -> str | None:
    """Longest common contiguous span of two texts (greedy upper bound)."""
    best = ""
    if len(a) > len(b):
        a, b = b, a
    for start in range(len(a)):
        lo, hi = 0, len(b) - start
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if a[start:start + mid] in b:
                lo = mid
            else:
                hi = mid - 1
        if lo > len(best):
            best = a[start:start + lo]
        if len(best) >= max(min_chars, len(a) // 2):
            break
    return best if len(best) >= min_chars else None


def _dedupe_candidates(blocks: list[tuple[str, str]]) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            span = _long_span_overlap(blocks[i][1], blocks[j][1], _MIN_OVERLAP_TOKENS * 2)
            if span:
                found.append((blocks[i][0], blocks[j][0],
                              estimate_tokens([{"role": "user", "content": span}])))
    found.sort(key=lambda x: -x[2])
    return found[:8]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--days", type=int, default=0,
                    help="only records from the last N days (name dates)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    files = sorted(RECORDS_DIR.glob("*.json"))
    if args.days:
        cutoff = datetime.now(TZ8) - timedelta(days=args.days)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        files = [f for f in files if _record_time_ms(f) >= cutoff_ms]
    files = files[-args.limit:] if args.limit > 0 else files

    records = []
    for fp in files:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        records.append(raw)
    if not records:
        print("no records read from", RECORDS_DIR)
        return 0

    lines = ["=" * 66, "PA Agent prompt 成本审计", f"记录数: {len(records)}",
             f"范围: {files[0].name} .. {files[-1].name}", ""]
    stats = _stage_stats(records)
    for key, label in (("stage1_prompt", "阶段一 prompt tokens"),
                       ("stage2_prompt", "阶段二 prompt tokens"),
                       ("completion", "completion tokens(两阶段合计)")):
        s = stats.get(key)
        if s:
            lines.append(f"{label}: 样本 {s['n']} 中位 {s['median']:.0f} "
                         f"均值 {s['mean']:.0f} 最大 {s['max']:.0f}")
    for key, label in (("stage1_cached_pct", "阶段一缓存命中率%"),
                       ("stage2_cached_pct", "阶段二缓存命中率%")):
        s = stats.get(key)
        if s:
            lines.append(f"{label}: 样本 {s['n']} 中位 {s['median']:.1f}%")
    lines.append("")
    lines.append("[静态规则文件 token 估算 Top 10]")
    for name, tokens in sorted(_file_token_sizes(), key=lambda x: -x[1])[:10]:
        lines.append(f"  {name}: {tokens}")
    lines.append("")
    lines.append("[提示块长文本重复对 (C2 去重候选, token 数按公共子串估算)]")
    blocks = []
    from pa_agent.ai import prompt_assembler as pa
    for attr in ("_LANGUAGE_ZH_RULE", "_PA_TERMINOLOGY_ZH", "_STAGE2_API_TASK_RULE",
                 "_OPENCLAW_AGENT_NO_TOOLS_RULE", "_THINKING_CONTENT_OUTPUT_RULE",
                 "_STAGE1_TAIL_REMINDER", "_STAGE2_TAIL_REMINDER",
                 "_STAGE1_OUTPUT_REMINDER", "_STAGE2_OUTPUT_CONTRACT",
                 "_INCREMENTAL_OUTPUT_HARD_RULES"):
        value = getattr(pa, attr, "")
        if isinstance(value, str) and len(value) > 200:
            blocks.append((attr, value))
    dups = _dedupe_candidates(blocks)
    if not dups:
        lines.append("  (未发现 >= 120 token 的重复公共子串)")
    for name_a, name_b, tokens in dups:
        lines.append(f"  {name_a} <-> {name_b}: ~{tokens} tokens")
    report = "\n".join(lines) + "\n"
    print(report, end="")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print("written:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
