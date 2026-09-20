# ruff: noqa: RUF001, RUF002, RUF003 - Chinese product copy
"""汇总 ab_intent.py 的 jsonl 结果：按臂统计意图率 + 两两比较（Fisher 精确检验）+ 逐条一致性。

用法：
    .venv/bin/python tools/ab_intent_report.py logs/ab_intent_run1.jsonl logs/ab_intent_run2.jsonl
"""
from __future__ import annotations

import collections
import json
import math
import sys
from pathlib import Path


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """双侧 Fisher 精确检验（2x2：a/b = 臂1 下单/观望，c/d = 臂2 下单/观望）。"""
    n = a + b + c + d
    if n == 0:
        return 1.0
    def prob(x: int) -> float:
        return (math.comb(a + b, x) * math.comb(c + d, a + c - x)) / math.comb(n, a + c)             if 0 <= x <= min(a + b, a + c) else 0.0
    p_obs = prob(a)
    lo = max(0, a + c - (c + d))
    hi = min(a + b, a + c)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= p_obs + 1e-12))


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] or [Path("logs/ab_intent_run1.jsonl")]
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            print(f"缺文件: {p}")
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    if not rows:
        print("没有结果")
        return 1

    # 去重（同一条记录同一臂可能在两次 run 里都出现 —— 保留两次，作为重复测量）
    by_arm: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)

    print(f"共 {len(rows)} 条结果，来自 {len(paths)} 个文件\n")
    print(f"{'臂':<10}{'调用':>5}{'成功':>5}{'下单':>5}{'意图率':>8}{'中位耗时':>10}")
    summary: dict[str, tuple[int, int]] = {}
    for arm, rs in sorted(by_arm.items()):
        ok = [r for r in rs if r.get("ok")]
        intent = [r for r in ok if r.get("intent")]
        lat = sorted(r.get("latency_s", 0) for r in ok) or [0]
        summary[arm] = (len(intent), len(ok))
        print(f"{arm:<10}{len(rs):>5}{len(ok):>5}{len(intent):>5}"
              f"{(100 * len(intent) / len(ok) if ok else 0):>7.1f}%{lat[len(lat) // 2]:>9.0f}s")

    print("\n=== 两两比较（Fisher 双侧，配对样本当独立计——保守）===")
    arms = sorted(summary)
    for i, a1 in enumerate(arms):
        for a2 in arms[i + 1:]:
            x1, n1 = summary[a1]
            x2, n2 = summary[a2]
            p = fisher_exact(x1, n1 - x1, x2, n2 - x2)
            print(f"  {a1:<9} {x1}/{n1} vs {a2:<9} {x2}/{n2}  p={p:.3f}")

    print("\n=== 逐条配对（每条记录，各臂是否下单）===")
    per_rec: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for r in rows:
        per_rec[r["record"]][r["arm"]] = r
    for rec in sorted(per_rec):
        cells = []
        for arm in arms:
            r = per_rec[rec].get(arm)
            if not r:
                cells.append(f"{arm}=—")
            elif not r.get("ok"):
                cells.append(f"{arm}=ERR")
            else:
                cells.append(f"{arm}={'✔' if r.get('intent') else '·'}")
        print("  " + rec[:30].ljust(32) + " ".join(cells))

    print("\n=== 只看零意图窗口 vs 阳性对照（对照=09-19 00:00-12:59）===")
    for arm, rs in sorted(by_arm.items()):
        ctrl = [r for r in rs if r.get("ok") and r["record"] < "2026-09-19_13"]
        win = [r for r in rs if r.get("ok") and r["record"] >= "2026-09-19_13"]
        ci = sum(1 for r in ctrl if r.get("intent"))
        wi = sum(1 for r in win if r.get("intent"))
        print(f"  {arm:<10} 对照 {ci}/{len(ctrl)}  零窗口 {wi}/{len(win)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
