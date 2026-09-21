"""配对显著性检验: 把 sim_trailing_stop 的逐笔 diff 转成"可采纳/不可采纳"判定.

动机: 出场规则的改进必须**配对**比较同一批交易。未配对的逐笔净盈亏标准差极大
(实测 n=102, std=11.03 U, SE=1.09 U -> 单笔可检出效应约 1.8 U), 配对后 SE 通常
降到 1/3~1/10, 才可能检出真实改进。

判定规则(事前冻结):
  可采纳  <=>  mean(diff) > 0  且  mean(diff) > 1.645 * SE(diff)
即单侧 95% 水平上均值差显著为正。除此之外一律"不足以采纳"。

纯只读: 读 detail json, 只打印/可选写一个输出 json。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

#: 单侧 95% 正态分位
Z_ONE_SIDED_95 = 1.645


def paired_stats(diffs: list[float]) -> dict:
    """配对差的均值/标准差/标准误/单侧 95% 可检出效应."""
    n = len(diffs)
    if n == 0:
        return {"n": 0, "mean": 0.0, "std": 0.0, "se": 0.0, "mde": 0.0}
    mean = sum(diffs) / n
    if n < 2:
        return {"n": n, "mean": mean, "std": 0.0, "se": float("nan"), "mde": float("nan")}
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    std = math.sqrt(var)
    se = std / math.sqrt(n)
    return {"n": n, "mean": mean, "std": std, "se": se, "mde": Z_ONE_SIDED_95 * se}


def verdict(stats: dict) -> tuple[bool, str]:
    """按冻结规则判定."""
    if stats["n"] < 2 or not math.isfinite(stats["se"]):
        return False, "样本不足"
    if stats["mean"] <= 0:
        return False, "方向为负"
    if stats["mean"] <= stats["mde"]:
        return False, f"未超配对 MDE ({stats['mean']:+.3f} <= {stats['mde']:.3f})"
    return True, "可采纳"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="paired_rule_significance",
        description="对出场规则做配对显著性检验(规则已冻结, 只读)",
    )
    ap.add_argument("detail_json", help="sim_trailing_stop.py --detail-json 的产物")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    payload = json.loads(Path(args.detail_json).read_text(encoding="utf-8"))
    pairs = payload.get("pairs") or {}
    if not pairs:
        print("detail json 里没有 pairs", file=sys.stderr)
        return 2

    print(
        f"窗口 {payload.get('window_days')} 天 | 成交 {payload.get('trades')} 笔 "
        f"| 实际净 {payload.get('actual_net', 0):+.2f} U"
    )
    print(f"判定规则(冻结): mean(diff) > 0 且 > {Z_ONE_SIDED_95} x SE(diff)")
    print()
    header = f"{'规则':<10}{'n':>5}{'均值差':>10}{'标准差':>10}{'SE':>9}{'配对MDE':>10}{'合计差':>11}  判定"
    print(header)
    print("-" * len(header))
    out: dict = {}
    order = sorted(pairs, key=lambda r: -sum(p["diff"] for p in pairs[r]))
    for rule in order:
        rows = pairs[rule]
        diffs = [float(p["diff"]) for p in rows]
        st = paired_stats(diffs)
        ok, note = verdict(st)
        print(
            f"{rule:<10}{st['n']:>5}{st['mean']:>+10.3f}{st['std']:>10.3f}"
            f"{st['se']:>9.3f}{st['mde']:>10.3f}{sum(diffs):>+11.2f}  {note}"
        )
        out[rule] = {**st, "total": sum(diffs), "adoptable": ok, "note": note}

    adoptable = [r for r in order if out[r]["adoptable"]]
    print()
    print("=" * 66)
    print(f"可采纳的规则: {len(adoptable)} / {len(order)}")
    for rule in adoptable:
        print(f"  {rule}  均值差 {out[rule]['mean']:+.3f} U/笔 (MDE {out[rule]['mde']:.3f})")
    if not adoptable:
        print("  无 —— 所有规则的配对改进都在噪声范围内")
    print("=" * 66)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
