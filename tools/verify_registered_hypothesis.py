"""执行 docs/预注册-15m均值回归-2026-09-21.md 冻结的样本外验证协议.

本脚本连同预注册协议在**运行验证之前**提交。阈值、方向、切分点、判定标准全部
硬编码在下方的"事前冻结项"里, 看到验证集结果之后不得修改 —— 要改就另开一份协议、
另取一段数据。

纯只读: 只读 records/pending, 只写 --cache 指定的临时文件。
"""

from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.factor_audit import (
    build_extractors,
    collect_facts,
    ic_stats,
    load_frames,
    permutation_null,
)

# ── 事前冻结项(来自 docs/预注册-15m均值回归-2026-09-21.md, 注册于 f3ebab4) ──
FROZEN_MIN_IC_ABS = 0.06816  # 探索集族级 |IC| p95 (rounds=500, seed=7)
FROZEN_MIN_IR = 1.1063  # 探索集族级 |IR| p95; 仅报告, 不参与判定
FROZEN_SPLIT = 900  # 前 900 份为探索集, 其余为验证集
FROZEN_MIN_OBS = 30
FROZEN_ROUNDS = 500
FROZEN_SEED = 7
#: 被检验的假设因子 -> 事前声明的 IC 方向
FROZEN_HYPOTHESIS: dict[str, str] = {
    "market.dist_to_low_atr": "negative",
    "market.invalidation_short": "negative",
    "market.dist_to_high_atr": "positive",
}

_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(.+)_(\d+m|\d+h)\.json$")


def _combo(path: Path) -> tuple[str, str] | None:
    """从文件名解析 (symbol, timeframe)."""
    match = _NAME_RE.match(path.name)
    return None if match is None else (match.group(1), match.group(2))


def _verdict(stats, direction: str) -> tuple[bool, str]:
    """按冻结的判定标准逐条判定."""
    if stats.n_obs < FROZEN_MIN_OBS:
        return False, f"样本不足 (n_obs={stats.n_obs})"
    signed = stats.ic_mean if direction == "positive" else -stats.ic_mean
    if stats.ic_mean == 0.0 or signed <= 0:
        return False, "方向相反"
    if abs(stats.ic_mean) < FROZEN_MIN_IC_ABS:
        return False, f"量级不足 (|IC|={abs(stats.ic_mean):.5f})"
    return True, "PASS"


def _table(title: str, facts: dict[str, list], directions: dict[str, str]) -> tuple[int, list[str]]:
    print(f"\n=== {title} ===")
    header = f"{'因子':<32} {'观测':>6} {'IC':>9} {'方向':>4} {'判定':>6}  说明"
    print(header)
    print("-" * len(header))
    passed = 0
    lines: list[str] = []
    for name, rows in sorted(facts.items(), key=lambda kv: -abs(ic_stats(kv[0], kv[1]).ic_mean)):
        stats = ic_stats(name, rows)
        declared = directions.get(name, "positive")
        if name in FROZEN_HYPOTHESIS:
            ok, note = _verdict(stats, declared)
            passed += int(ok)
            mark = "是" if ok else "否"
        else:
            note = "(仅报告, 不参与判定)"
            mark = "-"
        print(
            f"{name:<32} {stats.n_obs:>6} {stats.ic_mean:>9.4f} {declared[:2]:>4} {mark:>6}  {note}"
        )
        lines.append(
            f"{name},{stats.n_obs},{stats.ic_mean},{stats.ic_std},{stats.ir},{declared},{mark}"
        )
    return passed, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_registered_hypothesis",
        description="执行冻结的样本外验证协议(只读, 判定标准不可改)",
    )
    parser.add_argument("--records", default="records/pending")
    parser.add_argument("--limit", type=int, default=0, help="只取验证集前 N 份(冒烟); 0=全量")
    parser.add_argument("--cache", default=None, help="facts 缓存路径; 默认按 limit 区分")
    parser.add_argument("--json-out", default=None, help="把逐因子结果写入 JSON")
    args = parser.parse_args(argv)

    root = Path(args.records)
    if not root.is_dir():
        print(f"路径不存在: {root}", file=sys.stderr)
        return 2

    all_paths = sorted(root.glob("*.json"))
    explore_paths = all_paths[:FROZEN_SPLIT]
    valid_paths = all_paths[FROZEN_SPLIT:]
    if args.limit:
        valid_paths = valid_paths[: args.limit]
    explore_combos = {c for c in (_combo(p) for p in explore_paths) if c}

    print(f"探索集 {len(explore_paths)} 份 / 验证集 {len(valid_paths)} 份")
    print(f"探索集组合数(同总体口径): {len(explore_combos)}")
    print(f"冻结阈值: min_ic_abs={FROZEN_MIN_IC_ABS}  (IR {FROZEN_MIN_IR} 仅报告, 不参与判定)")

    cache = Path(args.cache) if args.cache else Path(f"/tmp/facts_valid_{args.limit or 'all'}.pkl")
    if cache.exists():
        with cache.open("rb") as fh:
            facts_all = pickle.load(fh)
        print(f"复用缓存: {cache}")
    else:
        facts_all = collect_facts(load_frames(valid_paths), build_extractors())
        with cache.open("wb") as fh:
            pickle.dump(facts_all, fh)
        print(f"已缓存: {cache}")

    facts_a = {
        name: [row for row in rows if (row.symbol, row.timeframe) in explore_combos]
        for name, rows in facts_all.items()
    }

    passed_a, lines_a = _table("Layer A(主判据): 仅探索集出现过的组合", facts_a, FROZEN_HYPOTHESIS)
    passed_b, lines_b = _table("Layer B(泛化性参考): 验证集全部组合", facts_all, FROZEN_HYPOTHESIS)

    print("\n=== 附带输出(不参与判定) ===")
    null = permutation_null(facts_a, rounds=FROZEN_ROUNDS, seed=FROZEN_SEED, family_wise=True)
    print(
        f"验证集自身族级零分布 (rounds={null.rounds}, 样本 {null.samples}): "
        f"|IC| p95={null.ic_p95:.5f} p99={null.ic_p99:.5f}"
    )
    print("(仅供参考, 不得用它替换冻结阈值)")

    total = len(FROZEN_HYPOTHESIS)
    print("\n" + "=" * 60)
    if passed_a == total:
        conclusion = f"支持 H1: Layer A {passed_a}/{total} 全部通过, 均值回归族在样本外复现"
    elif passed_a == 0:
        conclusion = f"否定 H1: Layer A 0/{total}, 放弃该族, 另立假设"
    else:
        conclusion = f"不确定: Layer A {passed_a}/{total}, 不宣称成立也不否定, 需更多数据"
    print(f"结论(按事前分级): {conclusion}")
    print(f"Layer B 通过数(仅供泛化性参考, 不作结论依据): {passed_b}/{total}")
    print("=" * 60)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "layer_a": lines_a,
                    "layer_b": lines_b,
                    "passed_a": passed_a,
                    "passed_b": passed_b,
                    "conclusion": conclusion,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
