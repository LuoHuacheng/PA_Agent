"""Horizon 扫描: 因子对"下一根 bar"的 IC 是否只是被 1-bar 微观噪声盖住了?

动机: 此前全部审计都只用 **1 根 bar** 的前向收益。而产品的实际持仓周期是数小时到数天,
1 根 15m bar 的收益基本由微观噪声主导 —— 在 1-bar 尺度测不出信号, 不能说明在真实
持仓尺度上也没有信号。

设计: 特征值与 horizon 无关, 只有前向收益依赖它。所以只跑**一次**昂贵的特征收集,
再从连续收盘价序列按每个 horizon 重算前向收益。7 个 horizon 只付一次的代价。

每个 horizon 用**它自己**的族级(FWER)置换零分布当阈值 —— 样本量随 horizon 变化,
阈值必须跟着变。价格对照项 `control.close_price` 全程保留, 用来暴露价位伪信号。

诊断性分析, 纯只读。
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.factor_audit import (
    BarFact,
    build_extractors,
    collect_facts,
    dedupe_facts,
    ic_stats,
    load_frames,
    permutation_null,
)

# ── 事前冻结 ──
FROZEN_HORIZONS = (1, 2, 4, 8, 12, 24, 48)
FROZEN_ROUNDS = 300
FROZEN_SEED = 7
FROZEN_MIN_OBS = 30


def _price_control() -> dict:
    """对照项: 原始收盘价本身(绝对价位, 不该有预测力)."""

    def extractor(frame):
        return float(frame.bars[0].close) if frame.bars else None

    return {"control.close_price": extractor}


def build_close_series(frames) -> dict[tuple[str, str], list[tuple[int, float]]]:
    """每个 (symbol, timeframe) 的连续收盘价序列, 按 ts 升序去重."""
    series: dict[tuple[str, str], dict[int, float]] = {}
    for frame in frames:
        bucket = series.setdefault((frame.symbol, frame.timeframe), {})
        for bar in frame.bars:
            bucket[int(bar.ts_open * 1000)] = float(bar.close)
    return {key: sorted(bucket.items()) for key, bucket in series.items()}


def horizon_returns(
    series: dict[tuple[str, str], list[tuple[int, float]]],
    horizon: int,
) -> dict[tuple[str, str, int], float]:
    """按 horizon 计算前向收益, **只保留不重叠的观测**.

    关键: horizon > 1 时若逐根取值, 相邻观测的前向窗口互相重叠 —— 观测不再独立。
    而置换零分布是逐 bar 打乱的, 会**低估**重叠情形下的方差, 让阈值偏低、命中虚高。
    所以每个 (symbol, timeframe) 每 horizon 根只取一根, 使窗口互不重叠。
    代价是有效样本量按 1/horizon 缩小 —— 那是真实的、诚实的信息量代价。
    """
    out: dict[tuple[str, str, int], float] = {}
    for (symbol, timeframe), points in series.items():
        for i in range(0, len(points) - horizon, horizon):
            ts, close = points[i]
            future = points[i + horizon][1]
            if close > 0:
                out[(symbol, timeframe, ts)] = future / close - 1.0
    return out


def retarget(facts_by_name, returns) -> dict[str, list[BarFact]]:
    """把观测的前向收益换成指定 horizon 的; 缺该 horizon 收益的观测丢弃."""
    out: dict[str, list[BarFact]] = {}
    for name, rows in facts_by_name.items():
        kept: list[BarFact] = []
        for row in rows:
            value = returns.get((row.symbol, row.timeframe, row.ts_ms))
            if value is None:
                continue
            kept.append(replace(row, forward_return=value))
        out[name] = kept
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="horizon_scan",
        description="扫描多个前向收益 horizon 下的因子 IC 与匹配零分布(只读)",
    )
    parser.add_argument("--records", default="records/pending")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rounds", type=int, default=FROZEN_ROUNDS)
    parser.add_argument("--horizons", default=",".join(str(h) for h in FROZEN_HORIZONS))
    parser.add_argument("--cache", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    root = Path(args.records)
    if not root.is_dir():
        print(f"路径不存在: {root}", file=sys.stderr)
        return 2
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())

    paths = sorted(root.glob("*.json"))
    if args.limit:
        paths = paths[: args.limit]
    print(f"记录 {len(paths)} 份 | horizons={horizons} | rounds={args.rounds} seed={FROZEN_SEED}")

    cache = Path(args.cache) if args.cache else Path(f"/tmp/hscan_{args.limit or 'all'}.pkl")
    if cache.exists():
        with cache.open("rb") as fh:
            facts, series = pickle.load(fh)
        print(f"复用缓存: {cache}")
    else:
        frames = load_frames(paths)
        series = build_close_series(frames)
        extractors = {**build_extractors(), **_price_control()}
        facts = collect_facts(frames, extractors)
        with cache.open("wb") as fh:
            pickle.dump((facts, series), fh)
        print(f"已缓存: {cache}")

    names = sorted(facts)
    results: dict[str, dict[str, tuple[float, int]]] = {name: {} for name in names}
    thresholds: dict[int, float] = {}

    for h in horizons:
        retargeted = retarget(facts, horizon_returns(series, h))
        retargeted = {name: dedupe_facts(rows) for name, rows in retargeted.items()}
        null = permutation_null(retargeted, rounds=args.rounds, seed=FROZEN_SEED, family_wise=True)
        thresholds[h] = null.ic_p95
        for name, rows in retargeted.items():
            st = ic_stats(name, rows)
            results[name][h] = (st.ic_mean, st.n_obs)
        counts = {len(v) for v in retargeted.values()}
        print(f"  horizon={h:>2}: 观测 {min(counts)}~{max(counts)}  族级 p95={null.ic_p95:.5f}")

    print()
    width = 11
    header = f"{'因子':<30}" + "".join(f"{'h=' + str(h):>{width}}" for h in horizons)
    print(header)
    print("-" * len(header))
    for name in names:
        cells = ""
        for h in horizons:
            ic = results[name][h][0]
            mark = "*" if abs(ic) >= thresholds[h] else " "
            cells += f"{ic:>9.4f}{mark} "
        print(f"{name:<30}{cells}")
    print()
    print("阈值(族级 p95, 每个 horizon 自标定):")
    for h in horizons:
        print(f"  h={h:>2}: {thresholds[h]:.5f}")

    print("\n=== 逐因子: 超出自身 horizon 阈值的个数 ===")
    for name in sorted(
        names, key=lambda n: -sum(1 for h in horizons if abs(results[n][h][0]) >= thresholds[h])
    ):
        hits = [h for h in horizons if abs(results[name][h][0]) >= thresholds[h]]
        if hits:
            print(f"  {name:<30} {len(hits)}/{len(horizons)}  命中 horizons={hits}")
    print('("*" = |IC| 超出该 horizon 的族级 p95)')

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "thresholds": {str(h): v for h, v in thresholds.items()},
                    "results": {n: {str(h): v for h, v in d.items()} for n, d in results.items()},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
