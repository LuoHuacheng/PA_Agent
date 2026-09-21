# ruff: noqa: RUF001 - Chinese product copy

"""时序因子审计: 逐个特征算 rank IC / IR / 月度胜率, 判断它是否真的预测下一根 bar.

移植自 alphaevo(https://github.com/ZhuLinsen/alphaevo, Apache-2.0) 的
validator.py / AlphaFactory, 但口径改写为**单品种逐 bar 时序** —— 上游是日线
截面选股(同一天多标的排序), 本仓库是加密单标的逐 bar 决策, 没有截面可用。

纯函数 + 标准库; 只读, 不写 records/ 与 trade_records/.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pa_agent.ai.kline_features import compute_kline_geometry_features
from pa_agent.data.base import KlineFrame
from pa_agent.data.snapshot import compute_indicators

#: 秩相关系数退化判定阈值
RANK_EPSILON = 1e-12

#: 与仓库其它工具一致的本地时区(UTC+8), 用于按月分桶
TZ8 = timezone(timedelta(hours=8))

ExpectedDirection = Literal["positive", "negative"]


@dataclass(frozen=True)
class BarFact:
    """一根 bar 上的一次观测: 特征值 + 其后一根 bar 的收益."""

    symbol: str
    ts_ms: int
    factor: float
    forward_return: float
    history: int = 0  # 该观测可用到的历史 bar 数; 跨快照去重时用来挑最深的一条
    timeframe: str = ""  # 周期; 去重与分组必须带上它, 否则 5m/15m 会撞同一个 ts_open


@dataclass(frozen=True)
class AuditThresholds:
    """因子准入阈值(与 alphaevo ValidationThresholds 对齐, 数值待本仓库样本标定)."""

    min_ic_abs: float = 0.02
    min_ir: float = 0.30
    min_monthly_win_rate: float = 0.50
    min_obs: int = 30


@dataclass(frozen=True)
class FactorStats:
    """一个因子的审计结论."""

    name: str
    n_obs: int
    group_count: int
    ic_mean: float
    ic_std: float
    ir: float
    monthly_win_rate: float
    passed: bool
    reasons: tuple[str, ...]
    ir_groups: int = 0  # 参与 IR 计算的分品种组数; 0 表示 IR 未定义(与 ir=0.0 可区分)


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _month_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=TZ8).strftime("%Y-%m")


def _ranks(values: Sequence[float]) -> list[float]:
    """平均秩(处理并列)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = len(a)
    if n < 3 or n != len(b):
        return None
    mean_a = _mean(a)
    mean_b = _mean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= RANK_EPSILON or var_b <= RANK_EPSILON:
        return None
    return cov / math.sqrt(var_a * var_b)


def spearman_ic(factor: Sequence[float], forward: Sequence[float]) -> float | None:
    """Spearman 秩相关. 样本不足(<3)或任一序列恒定(无秩)时返回 None."""
    if len(factor) < 3 or len(factor) != len(forward):
        return None
    return _pearson(_ranks(factor), _ranks(forward))


def ic_stats(
    name: str,
    rows: Sequence[BarFact],
    *,
    expected_direction: ExpectedDirection = "positive",
    thresholds: AuditThresholds | None = None,
) -> FactorStats:
    """按品种分组建 IC, 由组间 IC 的均值/标准差得 IR; 再按自然月算胜率."""
    limits = thresholds or AuditThresholds()
    sign = 1.0 if expected_direction == "positive" else -1.0
    reasons: list[str] = []

    # 分组必须带周期: 同一 ts_open 上的 5m/15m bar 是两种不同 horizon 的观测
    groups: dict[tuple[str, str], list[BarFact]] = {}
    for row in rows:
        groups.setdefault((row.symbol, row.timeframe), []).append(row)

    per_group_ic: list[float] = []
    for group_rows in groups.values():
        ic = spearman_ic(
            [r.factor for r in group_rows],
            [r.forward_return for r in group_rows],
        )
        if ic is not None:
            per_group_ic.append(ic)

    months: dict[str, list[BarFact]] = {}
    for row in rows:
        months.setdefault(_month_key(row.ts_ms), []).append(row)

    monthly_ic: list[float] = []
    for month_rows in months.values():
        ic = spearman_ic(
            [r.factor for r in month_rows],
            [r.forward_return for r in month_rows],
        )
        if ic is not None:
            monthly_ic.append(ic)

    ic_mean = _mean(per_group_ic)
    ic_std = _stdev(per_group_ic)
    ir = ic_mean / ic_std if ic_std > RANK_EPSILON else 0.0
    ir_groups = len(per_group_ic)
    # std=0 时 ir 退化为 0.0: 组间 IC 完全一致是"IR 未定义", 不是"没有方向性"
    identical_group_ics = ic_std <= RANK_EPSILON and ic_mean != 0.0
    win_rate = sum(1 for ic in monthly_ic if ic * sign > 0) / len(monthly_ic) if monthly_ic else 0.0

    if len(rows) < limits.min_obs:
        reasons.append(f"观测数 {len(rows)} < {limits.min_obs}")
    if ir_groups < 2:
        reasons.append("有效分品种 IC 少于 2 组, 无法估计 IR")

    directed_ic = ic_mean * sign
    directed_ir = ir * sign
    if ic_mean != 0.0 and directed_ic < 0:
        reasons.append(f"IC 均值 {ic_mean:.4f} 与预期方向 {expected_direction} 相反")
    elif directed_ic < limits.min_ic_abs:
        reasons.append(f"方向性 IC {directed_ic:.4f} < {limits.min_ic_abs}")
    # IR 判定需要 >=3 组才有统计功效(组数 <5 时功效本就很低, 见报告口径说明);
    # 组间 IC 完全一致时 ir 退化为 0.0, 那是"未定义"而非"无方向性", 直接通过 IR 检查
    if ir_groups >= 3 and not identical_group_ics:
        if directed_ir < 0:
            reasons.append(f"IR {ir:.4f} 与预期方向 {expected_direction} 相反")
        elif directed_ir < limits.min_ir:
            reasons.append(f"方向性 IR {directed_ir:.4f} < {limits.min_ir}")
    if win_rate < limits.min_monthly_win_rate:
        reasons.append(f"月度胜率 {win_rate:.2%} < {limits.min_monthly_win_rate:.0%}")

    return FactorStats(
        name=name,
        n_obs=len(rows),
        group_count=len(groups),
        ic_mean=round(ic_mean, 6),
        ic_std=round(ic_std, 6),
        ir=round(ir, 6),
        monthly_win_rate=round(win_rate, 6),
        passed=not reasons,
        reasons=tuple(reasons),
        ir_groups=ir_groups,
    )


#: KlineGeometryFeature 里可作因子输入的数值字段
GEOMETRY_NUMERIC_FIELDS = (
    "body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "close_position",
    "range_atr_ratio",
    "overlap_prev_ratio",
)

Extractor = Callable[[KlineFrame], "float | None"]


def dedupe_facts(facts: Iterable[BarFact]) -> list[BarFact]:
    """同一根 bar 会被多份滚动快照反复覆盖, 按 (symbol, timeframe, ts_ms) 只保留历史最深的一条.

    键必须带周期: 实测(2026-09-21)300 份记录里 797 个 (symbol, ts_open) 有 141 个(17.7%)
    同时被 5m 与 15m 覆盖; 同一 ts_open 的两种周期是完全不同的观测(OHLC 与 horizon 都不同),
    不带周期会静默丢掉其中一个.

    实测(2026-09-21, BTCUSDT 15m): 616 份记录里只有 1417 根不同的 bar, 99.4% 的 bar
    被多份记录覆盖, 单根最多出现 90 次 —— 不去重会把样本量虚增约 43 倍, 让 min_obs
    门槛与月度胜率全部失真。同一根 bar 在不同快照里能看到的**历史长度**不同(旧快照更短),
    特征值因此可能不同; 取历史最深的一条, 与生产路径(历史充足)的语义一致。
    """
    best: dict[tuple[str, str, int], BarFact] = {}
    for fact in facts:
        key = (fact.symbol, fact.timeframe, fact.ts_ms)
        current = best.get(key)
        if current is None or fact.history > current.history:
            best[key] = fact
    return sorted(best.values(), key=lambda f: (f.symbol, f.timeframe, f.ts_ms))


def forward_return_at(frame: KlineFrame, idx: int) -> float | None:
    """bar idx 之后一根 bar 的收盘收益.

    bars 是新到旧, 所以"下一根"是 idx - 1; 最新那根(idx=0)没有未来.
    """
    if idx <= 0 or idx >= len(frame.bars):
        return None
    current = float(frame.bars[idx].close)
    following = float(frame.bars[idx - 1].close)
    if current <= 0:
        return None
    return following / current - 1.0


def _window_frame(frame: KlineFrame, idx: int) -> KlineFrame:
    """只保留 bars[idx:] 的子 frame, 指标同步重算(按新到旧对齐)."""
    bars = list(frame.bars[idx:])
    return KlineFrame(
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        bars=tuple(bars),
        indicators=compute_indicators(bars),
        snapshot_ts_local_ms=frame.snapshot_ts_local_ms,
    )


def bar_facts_for(
    frame: KlineFrame,
    name: str,
    extractor: Extractor,
    *,
    min_history: int = 20,
    max_bars: int = 200,
) -> list[BarFact]:
    """在 frame 上逐 bar 取"当时可知的特征值"与其后一根 bar 的收益."""
    del name  # 仅用于调用方可读性; 观测本身不携带因子名
    facts: list[BarFact] = []
    bars = frame.bars
    for idx in range(min(len(bars) - 1, max_bars)):
        if len(bars) - idx < min_history:
            continue
        forward = forward_return_at(frame, idx)
        if forward is None:
            continue
        value = extractor(_window_frame(frame, idx))
        if value is None:
            continue
        facts.append(
            BarFact(
                symbol=frame.symbol,
                ts_ms=int(bars[idx].ts_open * 1000),
                factor=float(value),
                forward_return=forward,
                history=len(bars) - idx,
                timeframe=frame.timeframe,
            )
        )
    return facts


def geometry_extractors() -> dict[str, Extractor]:
    """把单 bar 几何特征的数值字段映射成"取最新 bar 该字段"的抽取函数."""

    def make(field: str) -> Extractor:
        def extractor(frame: KlineFrame) -> float | None:
            rows = compute_kline_geometry_features(frame, limit=1)
            if not rows:
                return None
            value = getattr(rows[0], field)
            return None if value is None else float(value)

        return extractor

    return {f"geometry.{field}": make(field) for field in GEOMETRY_NUMERIC_FIELDS}


def audit_frames(
    frames: Iterable[KlineFrame],
    extractors: dict[str, Extractor],
    *,
    expected_direction: ExpectedDirection = "positive",
    thresholds: AuditThresholds | None = None,
) -> list[FactorStats]:
    """对每个抽取函数跑一遍全样本, 返回按 IR 降序的审计结论."""
    frame_list = list(frames)
    results: list[FactorStats] = []
    for name, extractor in extractors.items():
        rows: list[BarFact] = []
        for frame in frame_list:
            rows.extend(bar_facts_for(frame, name, extractor))
        rows = dedupe_facts(rows)
        results.append(
            ic_stats(
                name,
                rows,
                expected_direction=expected_direction,
                thresholds=thresholds,
            )
        )
    results.sort(key=lambda s: (-abs(s.ir), s.name))
    return results


def load_frames(paths: Iterable[Path]) -> list[KlineFrame]:
    """把落盘的分析记录读成 KlineFrame; 坏文件跳过而不是中断."""
    from pa_agent.demo.record_loader import frame_from_record_klines, try_load_analysis_record

    frames: list[KlineFrame] = []
    for path in paths:
        record = try_load_analysis_record(Path(path))
        if record is None or not record.kline_data:
            continue
        try:
            frames.append(
                frame_from_record_klines(
                    record.kline_data,
                    symbol=record.meta.symbol,
                    timeframe=record.meta.timeframe,
                    snapshot_ts_local_ms=record.meta.timestamp_local_ms,
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return frames


def _fmt(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}"


def render_markdown(
    stats: Sequence[FactorStats],
    *,
    frames: int,
    observations: int,
    source: str,
) -> str:
    """把审计结论渲染成可归档的 Markdown 报告."""
    passed = [s for s in stats if s.passed]
    lines = [
        "# 因子审计报告",
        "",
        f"- 记录源：{source}",
        f"- 样本：{frames} 份记录 / 每因子约 {observations} 次观测",
        f"- 结论：{len(passed)} / {len(stats)} 个因子通过准入",
        "- 口径：分品种 IC 是**组内时序排序**，月度 IC 把当月所有品种混进同一秩池做**跨品种截面排序**，两者不是同一件事",
        "- 口径：IR 仅在分品种组数 >= 3 时才判定(组数越少统计功效越低)；IR 分组 列即参与 IR 计算的分品种(symbol×timeframe)组数，0 表示 IR 未定义而非 IR=0",
        "",
        "| 因子 | 观测 | 分组 | IR 分组 | IC 均值 | IR | 月度胜率 | 结论 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in stats:
        verdict = "通过" if item.passed else "未通过"
        lines.append(
            f"| {item.name} | {item.n_obs} | {item.group_count} | {item.ir_groups} | "
            f"{_fmt(item.ic_mean)} | {_fmt(item.ir)} | "
            f"{item.monthly_win_rate:.0%} | {verdict} |"
        )
    rejected = [s for s in stats if not s.passed]
    if rejected:
        lines.extend(["", "## 未通过原因", ""])
        for item in rejected:
            lines.append(f"### {item.name}")
            lines.append("")
            for reason in item.reasons:
                lines.append(f"- direction IC / IR 检查：{reason}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="factor_audit",
        description="时序因子审计: 算 rank IC / IR / 月度胜率(只读)",
    )
    parser.add_argument("--records", help="分析记录目录或单个 JSON 路径")
    parser.add_argument(
        "--limit",
        type=int,
        default=300,
        help="最多读入多少份记录; 按路径字典序截断, 取的是时间上最早的一段, 不含最新行情",
    )
    parser.add_argument("--direction", choices=("positive", "negative"), default="positive")
    parser.add_argument("--out", help="Markdown 报告输出路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结论")
    args = parser.parse_args(argv)

    if not args.records:
        parser.print_usage(sys.stderr)
        return 2

    root = Path(args.records)
    if not root.exists():
        print(f"路径不存在: {root}", file=sys.stderr)
        return 2
    paths = sorted(root.rglob("*.json"))[: args.limit] if root.is_dir() else [root]

    frames = load_frames(paths)
    if not frames:
        print("没有可用的分析记录", file=sys.stderr)
        return 2

    stats = audit_frames(frames, geometry_extractors(), expected_direction=args.direction)
    observations = sum(s.n_obs for s in stats) // max(len(stats), 1)

    if args.json:
        print(json.dumps([asdict(s) for s in stats], ensure_ascii=False, indent=2))
    else:
        print(
            render_markdown(stats, frames=len(frames), observations=observations, source=str(root))
        )

    if args.out:
        Path(args.out).write_text(
            render_markdown(stats, frames=len(frames), observations=observations, source=str(root)),
            encoding="utf-8",
        )
    return 0 if any(s.passed for s in stats) else 1


#: SimpleMarketFeatures 里可作因子输入的数值字段
MARKET_NUMERIC_FIELDS = (
    "range_high",
    "range_low",
    "range_width_atr",
    "price_position",
    "dist_to_high_atr",
    "dist_to_low_atr",
    "overlap_mean_10",
    "doji_inside_ratio_10",
    "barbwire_score",
    "pullback_depth_atr",
    "pullback_bars",
    "invalidation_long",
    "invalidation_short",
)


def market_extractors(*, lookback: int = 40) -> dict[str, Extractor]:
    """把结构特征的数值字段映射成抽取函数."""
    from pa_agent.ai.market_features import compute_simple_market_features

    def make(field: str) -> Extractor:
        def extractor(frame: KlineFrame) -> float | None:
            features = compute_simple_market_features(frame, lookback=lookback)
            value = getattr(features, field)
            return None if value is None else float(value)

        return extractor

    return {f"market.{field}": make(field) for field in MARKET_NUMERIC_FIELDS}


if __name__ == "__main__":
    raise SystemExit(main())
