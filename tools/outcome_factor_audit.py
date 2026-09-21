"""用真实成交结局当被预测对象: 决策时的 K 线特征能否预测这笔交易的最终结果?

与之前所有审计的根本区别:
- **被预测对象**不再是"下一根 bar 的合成收益", 而是这笔交易真实的 risk-normalized 结局
  (\`win_r\`), 它内含真实持仓周期与止盈止损结构 —— 那才是决策真正关心的东西。
- 样本是**跨交易**的横截面(每笔交易一个观测), 不是单标的时间序列。
- 样本量极小(约 90 笔), 而特征有 23 个 —— **先算噪声门槛, 再看有没有因子过得去**。

纯只读: 读 trade_records/outcomes.csv 与 records/pending/*.json, 不写任何仓库文件。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.factor_audit import (
    RANK_EPSILON,
    _mean,
    _quantile,
    build_extractors,
    spearman_ic,
)

FROZEN_TARGET = "win_r"
FROZEN_ROUNDS = 500
FROZEN_SEED = 7


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_control() -> dict:
    """对照: 决策时的收盘价绝对水平. 绝对价位不是因子, 用它暴露量纲伪信号."""

    def extractor(frame):
        return float(frame.bars[0].close) if frame.bars else None

    return {"control.close_price": extractor}


def load_trade_frames(csv_path: Path, root: Path, *, era: str | None):
    """把每笔成交接回它自己的决策记录, 返回 [(row, frame, target), ...]."""
    from pa_agent.demo.record_loader import frame_from_record_klines, try_load_analysis_record

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    out = []
    skipped = {"era": 0, "target": 0, "record": 0}
    for row in rows:
        if era is not None and row.get("era", "") != era:
            skipped["era"] += 1
            continue
        target = _to_float(row.get(FROZEN_TARGET))
        if target is None:
            skipped["target"] += 1
            continue
        raw = (row.get("record_file") or "").strip()
        if not raw:
            skipped["record"] += 1
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = root / raw.lstrip("/")
        record = try_load_analysis_record(path)
        if record is None or not record.kline_data:
            skipped["record"] += 1
            continue
        frame = frame_from_record_klines(
            record.kline_data,
            symbol=record.meta.symbol,
            timeframe=record.meta.timeframe,
            snapshot_ts_local_ms=record.meta.timestamp_local_ms,
        )
        out.append((row, frame, target))
    return out, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="outcome_factor_audit",
        description="用真实成交结局(win_r)审因子(只读, 先算噪声门槛)",
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--csv", default="trade_records/outcomes.csv")
    parser.add_argument("--era", default=None, help="只取该 era; 省略=全部(混合纪元, 有偏)")
    parser.add_argument("--rounds", type=int, default=FROZEN_ROUNDS)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    csv_path = root / args.csv
    if not csv_path.is_file():
        print(f"找不到 {csv_path}", file=sys.stderr)
        return 2

    trades, skipped = load_trade_frames(csv_path, root, era=args.era)
    n = len(trades)
    print(
        f"成交样本 n={n} (跳过: era={skipped['era']} 无target={skipped['target']} 记录不可读={skipped['record']})"
    )
    if n < 20:
        print("样本太少, 不做判定", file=sys.stderr)
        return 2
    targets = [t for _, _, t in trades]
    print(
        f"target \`{FROZEN_TARGET}\`: 中位={sorted(targets)[n // 2]:.3f} 均值={_mean(targets):.3f}"
    )

    extractors = {**build_extractors(), **_price_control()}
    series: dict[str, list[tuple[float, float]]] = {}
    for _, frame, target in trades:
        for name, fn in extractors.items():
            value = fn(frame)
            if value is None:
                continue
            series.setdefault(name, []).append((float(value), target))

    # ── 先算噪声门槛: 同一份打乱同时评估全部特征, 取每轮 max|IC| ──
    import random

    rng = random.Random(FROZEN_SEED)
    usable = {k: v for k, v in series.items() if len(v) >= 20}
    max_samples: list[float] = []
    for _ in range(args.rounds):
        perm = list(range(len(trades)))
        rng.shuffle(perm)
        shuffled = [targets[i] for i in perm]
        # 每个特征用自己的 (value, trade_index) 映射回打乱后的 target
        per_feature: list[float] = []
        for name, pairs in usable.items():
            values = [v for v, _ in pairs]
            # pairs 是按 trades 顺序 append 的, 但可能跳过了 None —— 重新按 trade 索引取
            shuffled_targets = [shuffled[i] for i in range(len(trades))]
            # 与 values 对应的 target 顺序需要重建: 用原始 pairs 的 target 定位会失真,
            # 因此改为对每个特征重采样其自身观测
            idx_map = [
                i for i, (_, frame, _t) in enumerate(trades) if extractors[name](frame) is not None
            ]
            tvals = [shuffled_targets[i] for i in idx_map]
            ic = spearman_ic(values, tvals)
            if ic is not None:
                per_feature.append(abs(ic))
        if per_feature:
            max_samples.append(max(per_feature))

    p95 = _quantile(max_samples, 0.95)
    p99 = _quantile(max_samples, 0.99)
    print(f"\n族级置换零分布 (rounds={args.rounds}, 样本 {len(max_samples)}):")
    print(f"  |IC| p95 = {p95:.4f}   p99 = {p99:.4f}   <- 23+1 个特征里最高那个的噪声上限")

    print(f"\n{'因子':<32}{'n':>5}{'IC':>9}{'|IC|/p95':>10}  判定")
    print("-" * 68)
    hits: list[str] = []
    for name, pairs in sorted(
        usable.items(),
        key=lambda kv: -abs(spearman_ic([v for v, _ in kv[1]], [t for _, t in kv[1]]) or 0.0),
    ):
        values = [v for v, _ in pairs]
        tvals = [t for _, t in pairs]
        ic = spearman_ic(values, tvals)
        if ic is None:
            continue
        ratio = abs(ic) / p95 if p95 > RANK_EPSILON else 0.0
        mark = "超阈值" if abs(ic) >= p95 else ""
        if mark:
            hits.append(name)
        print(f"{name:<32}{len(pairs):>5}{ic:>9.4f}{ratio:>10.2f}  {mark}")

    print("\n" + "=" * 68)
    print(f"超出族级 p95 的特征: {len(hits)} / {len(usable)}")
    for name in hits:
        print(f"  {name}")
    print("=" * 68)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"n": n, "p95": p95, "p99": p99, "hits": hits}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
