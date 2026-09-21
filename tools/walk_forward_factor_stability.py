"""Walk-forward 因子稳定性诊断: 把全部记录切成 K 个连续时间折, 每折用**该折自身**
的族级置换零分布当阈值, 看同一因子的 IC 是否跨折同号且跨折显著。

**这是诊断, 不是证实。** 它使用的数据在此之前已经被看过(探索集与验证集),
所以不能作为样本外证据。它回答的是"信号是否随时间稳定", 而不是"假设是否成立"。

判定规则在运行前冻结(下方常量)。纯只读。
"""

from __future__ import annotations

import argparse
import json
import pickle
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


def _price_control() -> dict:
    """对照项: 原始收盘价本身.

    绝对价位不该当因子, 但放进同一张表当对照可以暴露"价位类伪信号":
    若它表现出与价位类特征同样的跨折稳定性, 说明那类稳定性来自价格水平漂移,
    而不是对下一根收益的预测力。
    """

    def extractor(frame):
        return float(frame.bars[0].close) if frame.bars else None

    return {"control.close_price": extractor}


# ── 事前冻结的规则 ──
FROZEN_FOLDS = 6  # 连续时间折数
FROZEN_ROUNDS = 200  # 每折族级零分布的置换轮数
FROZEN_SEED = 7
FROZEN_MIN_OBS = 30  # 单折内因子观测数下限
FROZEN_SIGN_MIN_FOLDS = 5  # "同号"要求: >= 5/6 折与多数符号一致
FROZEN_SIG_MIN_FOLDS = 4  # "显著"要求: >= 4/6 折的 |IC| >= 该折自身族级 p95
FROZEN_LEVEL = "p95"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="walk_forward_factor_stability",
        description="跨时间折的因子稳定性诊断(规则已冻结, 只读)",
    )
    parser.add_argument("--records", default="records/pending")
    parser.add_argument("--folds", type=int, default=FROZEN_FOLDS)
    parser.add_argument("--rounds", type=int, default=FROZEN_ROUNDS)
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 份(冒烟); 0=全量")
    parser.add_argument("--cache", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    root = Path(args.records)
    if not root.is_dir():
        print(f"路径不存在: {root}", file=sys.stderr)
        return 2

    paths = sorted(root.glob("*.json"))
    if args.limit:
        paths = paths[: args.limit]
    if len(paths) < args.folds * 50:
        print(f"记录太少({len(paths)}), 无法切 {args.folds} 折", file=sys.stderr)
        return 2

    size = len(paths) // args.folds
    folds = [paths[i * size : (i + 1) * size] for i in range(args.folds)]
    print(f"记录 {len(paths)} 份 -> {args.folds} 折, 每折 {size} 份")
    print(f"折区间: {folds[0][0].name[:10]} .. {folds[-1][-1].name[:10]}")
    print(
        f"冻结规则: 同号 >= {FROZEN_SIGN_MIN_FOLDS}/{args.folds} 且 显著 >= {FROZEN_SIG_MIN_FOLDS}/{args.folds}"
    )
    print("(诊断性分析: 用的是已看过的数据, 不构成样本外证据)")

    cache = Path(args.cache) if args.cache else Path(f"/tmp/facts_wf_{args.limit or 'all'}.pkl")
    per_fold: list[dict[str, tuple[float, float, int]]] = []
    thresholds: list[float] = []
    if cache.exists():
        with cache.open("rb") as fh:
            per_fold, thresholds = pickle.load(fh)
        print(f"复用缓存: {cache}")
    else:
        for i, fold_paths in enumerate(folds):
            extractors = {**build_extractors(), **_price_control()}
            facts = collect_facts(load_frames(fold_paths), extractors)
            null = permutation_null(facts, rounds=args.rounds, seed=FROZEN_SEED, family_wise=True)
            row: dict[str, tuple[float, float, int]] = {}
            for name, rows in facts.items():
                st = ic_stats(name, rows)
                row[name] = (st.ic_mean, st.ir, st.n_obs)
            per_fold.append(row)
            thresholds.append(null.ic_p95)
            print(
                f"  折 {i + 1}: 组合 {len({(r.symbol, r.timeframe) for rows in facts.values() for r in rows})}"
                f"  族级 p95={null.ic_p95:.5f}"
            )
        with cache.open("wb") as fh:
            pickle.dump((per_fold, thresholds), fh)
        print(f"已缓存: {cache}")

    names = sorted(per_fold[0])
    print()
    header = (
        f"{'因子':<32}"
        + "".join(f"{'折' + str(i + 1):>9}" for i in range(args.folds))
        + f"{'同号':>6}{'显著':>6}  判定"
    )
    print(header)
    print("-" * len(header))
    summary: dict[str, dict] = {}
    for name in names:
        ics = [per_fold[i][name][0] for i in range(args.folds)]
        obs = [per_fold[i][name][2] for i in range(args.folds)]
        sig = sum(
            1 for i, ic in enumerate(ics) if obs[i] >= FROZEN_MIN_OBS and abs(ic) >= thresholds[i]
        )
        majority = (
            1.0
            if sum(1 for ic in ics if ic > 0) >= args.folds - sum(1 for ic in ics if ic > 0)
            else -1.0
        )
        same = sum(1 for ic in ics if ic * majority > 0)
        stable = same >= FROZEN_SIGN_MIN_FOLDS and sig >= FROZEN_SIG_MIN_FOLDS
        cells = "".join(f"{ic:>9.4f}" for ic in ics)
        print(f"{name:<32}{cells}{same:>6}{sig:>6}  {'稳定' if stable else '不稳'}")
        summary[name] = {
            "ics": ics,
            "same_sign_folds": same,
            "significant_folds": sig,
            "stable": stable,
        }

    stable_names = [n for n, s in summary.items() if s["stable"]]
    print("\n" + "=" * 70)
    print(f"跨 {args.folds} 折同时满足同号与显著的因子: {len(stable_names)} / {len(names)}")
    for name in stable_names:
        print(
            f"  {name}  (同号 {summary[name]['same_sign_folds']}/{args.folds}, "
            f"显著 {summary[name]['significant_folds']}/{args.folds})"
        )
    print("=" * 70)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"thresholds": thresholds, "summary": summary, "stable": stable_names},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
