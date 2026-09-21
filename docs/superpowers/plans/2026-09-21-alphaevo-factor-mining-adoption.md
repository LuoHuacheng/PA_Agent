# 借鉴 alphaevo 做因子挖掘 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 PA_Agent 补上"因子这一层" —— 把现有 K 线特征从"写死在 prompt 组装里"变成可量化打分、可存档、可淘汰的研究资产。

**Architecture:** 三段递进。① **审计**：在历史 K 线上逐 bar 计算特征值与其后一根 bar 的收益，算时序 rank IC / IR / 月度胜率，产出因子排行榜；② **存档**：SQLite 因子库，记录定义、指标、状态（active / retired / failed）；③ **准入**：成熟度检查单把"够不够格进下一步"变成结构化结论 + 一条可执行的下一步命令。机制移植自 alphaevo 的 `alpha_factory`（Apache-2.0，单向兼容本仓库 AGPL-3.0），但把它的**日线截面 IC** 改写成**单品种逐 bar 时序 IC** —— 本仓库是加密单标的逐 bar 决策，没有同日多标的截面。

**Tech Stack:** Python 3.11、pytest、ruff（line-length 100）、标准库 sqlite3 / dataclasses / statistics、复用既有 `pa_agent.data` 与 `pa_agent.ai`。

**Spec:** 本计划来源是 2026-09-21 对 https://github.com/ZhuLinsen/alphaevo 的对照分析。结论：**不整体 clone**（它跑美股/ A 股日线截面选股，标的与语义都不同），只拆它 `alpha_factory / validator / maturity` 三个模块的机制，套到本仓库的 K 线特征与 `records/` 上。

---

## 背景事实（均已在本仓库核实）

| 事实 | 值 |
| --- | --- |
| 落盘分析记录 | `records/pending/` 共 **5737** 份 JSON（2026-08-07 起） |
| 已平仓对账样本 | `trade_records/outcomes.csv`，含 `conf / cycle_position / win_r / net_usdt / era` 等 30 列 |
| bar 顺序 | `KlineFrame.bars[0]` 是**最新** bar，`bars[-1]` 最旧（`pa_agent/data/base.py:66`） |
| 指标计算 | `compute_indicators(bars: list[KlineBar]) -> IndicatorBundle`（`pa_agent/data/snapshot.py:104`） |
| 单 bar 几何特征 | `KlineGeometryFeature` 16 字段，数值字段 6 个（`pa_agent/ai/kline_features.py:18`） |
| 结构特征 | `SimpleMarketFeatures` 33 字段，数值字段 13 个（`pa_agent/ai/market_features.py`） |
| 记录元数据 | `RecordMeta.timestamp_local_ms / symbol / timeframe / bar_count`（`pa_agent/records/schema.py:12`） |
| 测试闸门 | 仓库根 `conftest.py` 对 `PROJECT_ROOT/trade_records` 与 `PROJECT_ROOT/records` 的写入直接抛 `RuntimeError` |

**关键洞察（决定架构）**：本仓库没有现成的 `forward_returns`。但 `bars` 是**新到旧**排序，所以 bar `idx` 的"下一根"就是 `idx - 1` —— 前向收益可以直接在单份记录的 `kline_data` 里就地取得，**不需要新建回测引擎**。这是本计划成本可控的根本原因。

---

## 范围与非目标

**做**

- 时序因子审计工具（CLI + 库函数），对既有 6 个几何特征 + 13 个结构特征逐个打分
- SQLite 因子库，落定义与指标，支持 active / retired / failed 状态流转
- 成熟度检查单 + 下一步动作推荐
- 与已落地的 `tools/feature_guard.py` 打通：审计的输入特征若来自 LLM 生成代码，先过静态体检

**不做（明确非目标）**

- 不建回测引擎、不做资金曲线与滑点模拟（那是 Phase 4/5 的独立课题）
- 不做横截面 IC / 换手率 —— 本仓库是单标的时序，截面口径无意义
- 不执行 LLM 生成的代码。本仓库当前架构不动态执行模型输出，引入它是一个新的攻击面，须单独立项评估
- 不改 `pa_agent/ai/kline_features.py` 与 `market_features.py` 的任何行为（只读取其输出）
- 不做因子组合优化 / 权重分配

---

## Global Constraints

以下为项目级硬约束，**每个任务的要求都隐含包含本节**：

- Python `>=3.11`；命令一律用 `.venv/bin/python`
- lint：每个 Task 的 lint 步骤**一律按三步走**：`ruff check --fix <files>`（自动修可修项，含 I003/I001 import 排序与 F401 未使用 import）→ `ruff format <files>` → `ruff check <files>`（必须 0 报错）。注意 `ruff format` **不会**排序 import，所以只跑 `format` + `check` 会在 import 排序上失败。`line-length = 100`，`extend-select = ["E","F","I","UP","B","SIM","RUF"]`，`E501` 忽略。
  - 实测坑：本仓库 ruff 把 `tools` 与 `pa_agent` 判为**不同** import 段（`tools` 视为 first-party），所以 `from pa_agent...` 与 `from tools...` 之间**必须有一个空行**；同段内则**不得**有空行。
- 测试放 `tests/unit/`，命名 `test_*.py`；工具模块以 `from tools.<module> import ...` 导入（`tools/` 是命名空间包，无 `__init__.py`）
- 提交信息格式固定：`<emoji> <type>(scope): 中文描述`，例：`✨ feat(tools): 加时序因子审计`
- **向任何已有 .py 文件追加代码块时**（源文件与测试文件同理）：代码块里的新增 `import` 必须**合并进该文件顶部已有的 import 区**，只把函数/常量追加到文件末尾。整块贴在文件中间会触发 `E402 Module level import not at top of file`，而 `ruff check --fix` **不会**自动修这一条。新建文件不受此限。
- **只读约束**：本计划新增的工具在测试与运行中都**不得写入** `records/` 与 `trade_records/`。因子库默认落在 `~/.pa_agent/factors.db`（用户目录），避开仓库根 conftest 的进程级 fs 闸门
- 术语：前视 = look-ahead；IC = Spearman rank IC；IR = IC 均值 / IC 标准差；方向性 IC = IC 均值 × 预期方向符号
- 所有新增数值函数必须是**纯函数**（输入 list/序列，输出 float），便于 TDD 与复现
- 随机性一律带 `seed`；不引入 `numpy` 之外的数值依赖（标准库可算的一律标准库）

---

## 文件结构

**新建**

| 文件 | 职责 |
| --- | --- |
| `tools/factor_audit.py` | 时序因子审计：统计核心 + 观测抽取 + CLI |
| `pa_agent/factors/__init__.py` | 包标记（导出 store / maturity 的公共名） |
| `pa_agent/factors/store.py` | SQLite 因子库（定义、指标、状态流转） |
| `pa_agent/factors/maturity.py` | 成熟度检查单 + 下一步动作 |
| `tests/unit/test_factor_audit.py` | Phase 1 回归 |
| `tests/unit/test_factor_store.py` | Phase 2 回归 |
| `tests/unit/test_factor_maturity.py` | Phase 3 回归 |

**修改**：无。Phase 1 是全新的评测工具；Phase 2/3 只新增包。既有模块一行不动。

**已完成基线（本计划不重复实施）**

- `tools/feature_guard.py`（755 行）+ `tests/unit/test_feature_guard.py`（43 用例）—— 因子代码静态体检，15 条规则，纯 AST 不执行代码。
- 验证命令：`.venv/bin/python -m pytest tests/unit/test_feature_guard.py -q` → `43 passed`；`.venv/bin/python tools/feature_guard.py --demo` → `13/13 个样例符合期望`。
- 本计划的 Task 1.2 会在抽取"LLM 生成因子"的观测时调用它做前置体检（见 Task 1.4）。

---

## Phase 1：时序因子审计（P0，先做）

产出：一条命令能回答"我现在喂给模型的这 19 个特征，哪些其实预测不了下一根 bar"。

### Task 1.1：时序 IC 与因子统计核心

**Files:**
- Create: `tools/factor_audit.py`
- Test: `tests/unit/test_factor_audit.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces:
  - `BarFact(symbol: str, ts_ms: int, factor: float, forward_return: float)` —— frozen dataclass
  - `AuditThresholds(min_ic_abs=0.02, min_ir=0.30, min_monthly_win_rate=0.50, min_obs=30)` —— frozen dataclass
  - `FactorStats(name, n_obs, group_count, ic_mean, ic_std, ir, monthly_win_rate, passed, reasons)` —— frozen dataclass，`reasons: tuple[str, ...]`
  - `spearman_ic(factor: Sequence[float], forward: Sequence[float]) -> float | None`
  - `ic_stats(name, rows: Sequence[BarFact], *, expected_direction: Literal["positive","negative"] = "positive", thresholds: AuditThresholds | None = None) -> FactorStats`

- [ ] **Step 1: 写失败测试**

写入 `tests/unit/test_factor_audit.py`：

```python
"""时序因子审计的统计核心."""
from __future__ import annotations

from tools.factor_audit import AuditThresholds, BarFact, ic_stats, spearman_ic

BASE_TS = 1_700_000_000_000
DAY_MS = 86_400_000


def _fact(symbol: str, ts_ms: int, factor: float, forward: float) -> BarFact:
    return BarFact(symbol=symbol, ts_ms=ts_ms, factor=factor, forward_return=forward)


def _increasing_rows(symbol: str, *, swaps: int) -> list[BarFact]:
    """factor 单调递增; forward 在 swaps 处做相邻逆序对, 用来压低 IC。"""
    factors = [float(i) for i in range(40)]
    forwards = [float(i) for i in range(40)]
    for k in range(swaps):
        forwards[2 * k], forwards[2 * k + 1] = forwards[2 * k + 1], forwards[2 * k]
    return [
        _fact(symbol, BASE_TS + i * DAY_MS, factors[i], forwards[i]) for i in range(40)
    ]


def test_spearman_perfect_positive():
    assert spearman_ic([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == 1.0


def test_spearman_perfect_negative():
    assert spearman_ic([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) == -1.0


def test_spearman_is_rank_based_not_linear():
    # 单调但非线性, Spearman 仍为 1
    assert spearman_ic([1, 2, 3, 4, 5], [1, 4, 9, 16, 25]) == 1.0


def test_spearman_returns_none_for_constant_series():
    assert spearman_ic([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None


def test_spearman_returns_none_for_too_few_or_mismatched():
    assert spearman_ic([1, 2], [1, 2]) is None
    assert spearman_ic([1, 2, 3], [1, 2]) is None


def test_ic_stats_passes_a_predictive_factor():
    rows = _increasing_rows("BTCUSDT", swaps=0) + _increasing_rows("ETHUSDT", swaps=3)
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.n_obs == 80
    assert stats.group_count == 2
    assert stats.ic_mean > 0.9
    assert stats.ir > 0.3
    assert stats.monthly_win_rate > 0.5
    assert stats.passed is True
    assert stats.reasons == ()


def test_ic_stats_rejects_a_direction_flipped_factor():
    rows = [
        _fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(-i)) for i in range(40)
    ]
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.ic_mean < -0.9
    assert stats.passed is False
    assert any("方向" in reason for reason in stats.reasons)


def test_ic_stats_rejects_a_too_small_sample():
    rows = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(5)]
    stats = ic_stats("geometry.body_ratio", rows)
    assert stats.passed is False
    assert any("观测数" in reason for reason in stats.reasons)


def test_ic_stats_respects_negative_expected_direction():
    rows = [
        _fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(-i)) for i in range(40)
    ]
    stats = ic_stats("geometry.body_ratio", rows, expected_direction="negative")
    assert stats.ic_mean < -0.9
    assert not any("方向" in reason for reason in stats.reasons)


def test_thresholds_are_overridable():
    rows = [_fact("BTCUSDT", BASE_TS + i * DAY_MS, float(i), float(i)) for i in range(40)]
    strict = AuditThresholds(min_obs=1000)
    assert ic_stats("x", rows, thresholds=strict).passed is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'tools.factor_audit'`

- [ ] **Step 3: 写最小实现**

写入 `tools/factor_audit.py`（本步只放常量、数据模型、统计核心；模块 docstring 需写明"移植自 alphaevo，IC 口径由截面改时序"）：

```python
"""时序因子审计: 逐个特征算 rank IC / IR / 月度胜率, 判断它是否真的预测下一根 bar.

移植自 alphaevo(https://github.com/ZhuLinsen/alphaevo, Apache-2.0) 的
validator.py / AlphaFactory, 但口径改写为**单品种逐 bar 时序** —— 上游是日线
截面选股(同一天多标的排序), 本仓库是加密单标的逐 bar 决策, 没有截面可用。

纯函数 + 标准库; 只读, 不写 records/ 与 trade_records/.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

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

    groups: dict[str, list[BarFact]] = {}
    for row in rows:
        groups.setdefault(row.symbol, []).append(row)

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
    win_rate = (
        sum(1 for ic in monthly_ic if ic * sign > 0) / len(monthly_ic) if monthly_ic else 0.0
    )

    if len(rows) < limits.min_obs:
        reasons.append(f"观测数 {len(rows)} < {limits.min_obs}")
    if len(per_group_ic) < 2:
        reasons.append("有效分品种 IC 少于 2 组, 无法估计 IR")

    directed_ic = ic_mean * sign
    directed_ir = ir * sign
    if ic_mean != 0.0 and directed_ic < 0:
        reasons.append(f"IC 均值 {ic_mean:.4f} 与预期方向 {expected_direction} 相反")
    elif directed_ic < limits.min_ic_abs:
        reasons.append(f"方向性 IC {directed_ic:.4f} < {limits.min_ic_abs}")
    # 只有 >=2 个分品种组时 IR 才有定义: 单组样本下 std 恒为 0, 判定 IR 会误报
    if len(per_group_ic) >= 2:
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
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: PASS（10 passed）

- [ ] **Step 5: lint + 提交**

```bash
.venv/bin/python -m ruff format tools/factor_audit.py tests/unit/test_factor_audit.py
.venv/bin/python -m ruff check tools/factor_audit.py tests/unit/test_factor_audit.py
git add tools/factor_audit.py tests/unit/test_factor_audit.py
git commit -m "✨ feat(tools): 加时序因子审计统计核心(rank IC/IR/月度胜率)"
```

---

### Task 1.2：从落盘记录抽取 (特征值, 前向收益) 观测

**Files:**
- Modify: `tools/factor_audit.py`（追加，不改 Task 1.1 已有函数）
- Test: `tests/unit/test_factor_audit.py`（追加）

**Interfaces:**
- Consumes: `BarFact`（Task 1.1）；`pa_agent.demo.record_loader.frame_from_record_klines`、`try_load_analysis_record`；`pa_agent.data.snapshot.compute_indicators`；`pa_agent.ai.kline_features.compute_kline_geometry_features`
- Produces:
  - `BarFact` **接口扩展**：新增带默认值的字段 `history: int = 0`（默认值保证 Task 1.1 已提交的 10 个用例不受影响）
  - `dedupe_facts(facts: Iterable[BarFact]) -> list[BarFact]`
  - `forward_return_at(frame: KlineFrame, idx: int) -> float | None`
  - `bar_facts_for(frame, name, extractor: Callable[[KlineFrame], float | None], *, min_history: int = 20, max_bars: int = 200) -> list[BarFact]`
  - `geometry_extractors() -> dict[str, Callable[[KlineFrame], float | None]]`
  - `audit_frames(frames, extractors, *, expected_direction="positive", thresholds=None) -> list[FactorStats]`

**为什么必须去重（真数据实测的结论，不是猜测）**：`records/pending` 是**逐根 bar 的滚动快照**，同一根 bar 会被大量记录重复覆盖。2026-09-21 对 BTCUSDT 15m 的探测：**616 份记录、每份中位 100 根 bar，但只有 1417 根不同的 bar**；99.4% 的 bar 被多份记录覆盖，单根最多被 **90 份**覆盖。朴素池化会把样本量虚增约 **43 倍**，让 `min_obs` 门槛与月度胜率全部失真。所以 `audit_frames` 必须按 `(symbol, ts_ms)` 去重。

同一根 bar 在不同快照里**可见的历史长度不同**（旧快照更短），特征值因此可能不同；去重时保留**历史最深**的那条，与生产路径（历史充足）语义一致。

**核心语义（别搞反）**：`frame.bars[0]` 是**最新** bar、`bars[-1]` 最旧。所以 bar `idx` 的"下一根"是 `idx - 1`；`idx == 0` 没有未来，必须剔除。评估 bar `idx` 的特征值时，要用 `bars[idx:]` 重建一个子 frame（`compute_indicators` 必须重算，因为它按"新到旧"对齐），再取该子 frame 的`最新`一行 —— 这样特征函数内部只会看到 idx 及其之前的数据，与生产路径的语义完全一致。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_factor_audit.py` 中做两件事（**顺序很关键**）：

1. 把下面代码块里的 4 条 import（`replace` / `pytest` / `pa_agent...` / `tools.factor_audit`）**合并进文件顶部已有的 import 区**，不要贴在文件中间 —— 否则 ruff 会报 `E402 Module level import not at top of file`。
2. 把 `def _frame(...)` 及其后的所有测试函数**追加到文件末尾**。

```python
from dataclasses import replace

import pytest

from pa_agent.data.base import KlineBar, KlineFrame
from pa_agent.data.snapshot import compute_indicators
from tools.factor_audit import (
    BarFact,
    audit_frames,
    bar_facts_for,
    dedupe_facts,
    forward_return_at,
    geometry_extractors,
)


def _frame(closes: list[float], *, symbol: str = "BTCUSDT") -> KlineFrame:
    """closes 按"旧到新"给出; 返回 newest-first 的 KlineFrame."""
    built = [
        KlineBar(
            seq=len(closes) - i,
            ts_open=1_700_000_000.0 + i * 900.0,
            open=float(c),
            high=float(c) + 1.0,
            low=float(c) - 1.0,
            close=float(c),
            volume=10.0,
        )
        for i, c in enumerate(closes)
    ]
    built.reverse()
    bars = [replace(bar, seq=i + 1) for i, bar in enumerate(built)]
    return KlineFrame(
        symbol=symbol,
        timeframe="15m",
        bars=tuple(bars),
        indicators=compute_indicators(bars),
        snapshot_ts_local_ms=1_700_000_000_000,
    )


def test_forward_return_at_uses_the_newer_neighbour_bar():
    frame = _frame([100.0, 101.0, 102.0])
    assert forward_return_at(frame, 2) == pytest.approx(101.0 / 100.0 - 1.0)
    assert forward_return_at(frame, 1) == pytest.approx(102.0 / 101.0 - 1.0)


def test_forward_return_at_is_none_for_the_newest_bar():
    frame = _frame([100.0, 101.0, 102.0])
    assert forward_return_at(frame, 0) is None


def test_bar_facts_exclude_the_newest_bar_and_short_history():
    frame = _frame([100.0 + i for i in range(60)])
    extractor = geometry_extractors()["geometry.body_ratio"]
    facts = bar_facts_for(frame, "geometry.body_ratio", extractor, min_history=20)
    assert facts
    newest_ts = int(frame.bars[0].ts_open * 1000)
    assert all(fact.ts_ms != newest_ts for fact in facts)
    # 历史不足 min_history 的尾部 bar 也不该产出观测
    oldest_used_ts = int(frame.bars[-1].ts_open * 1000)
    assert all(fact.ts_ms >= oldest_used_ts for fact in facts)


def test_geometry_extractors_expose_all_numeric_fields():
    names = set(geometry_extractors())
    assert names == {
        "geometry.body_ratio",
        "geometry.upper_wick_ratio",
        "geometry.lower_wick_ratio",
        "geometry.close_position",
        "geometry.range_atr_ratio",
        "geometry.overlap_prev_ratio",
    }


def test_audit_frames_returns_one_stats_row_per_extractor():
    frames = [
        _frame([100.0 + i for i in range(60)], symbol="BTCUSDT"),
        _frame([200.0 - i for i in range(60)], symbol="ETHUSDT"),
    ]
    extractors = {
        "geometry.body_ratio": geometry_extractors()["geometry.body_ratio"],
        "constant_zero": lambda frame: 0.0,
    }
    stats = {s.name: s for s in audit_frames(frames, extractors)}
    assert set(stats) == {"geometry.body_ratio", "constant_zero"}
    # 恒定特征无秩 => 无有效 IC
    assert stats["constant_zero"].passed is False
    assert stats["constant_zero"].ic_mean == 0.0


def test_dedupe_keeps_the_deepest_history_for_a_repeated_bar():
    shallow = BarFact("BTCUSDT", 1000, 0.5, 0.01, history=5)
    deep = BarFact("BTCUSDT", 1000, 0.9, 0.01, history=60)
    other = BarFact("BTCUSDT", 2000, 0.1, 0.02, history=30)
    got = dedupe_facts([shallow, other, deep])
    assert [f.ts_ms for f in got] == [1000, 2000]
    assert got[0].factor == 0.9


def test_audit_frames_dedupes_bars_shared_by_two_snapshots():
    # 两份快照覆盖同一批 bar: 去重后观测数不应翻倍
    first = _frame([100.0 + i for i in range(60)])
    second = _frame([100.0 + i for i in range(60)])
    extractors = {"geometry.body_ratio": geometry_extractors()["geometry.body_ratio"]}
    single = audit_frames([first], extractors)[0].n_obs
    doubled = audit_frames([first, second], extractors)[0].n_obs
    assert single > 0
    assert doubled == single
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: FAIL —— `ImportError: cannot import name 'forward_return_at'`

- [ ] **Step 3: 写最小实现**

追加到 `tools/factor_audit.py`：

**先做一处接口扩展**：用 `edit` 工具把 Task 1.1 已提交的 `BarFact` 定义改成下面这样（只加一个带默认值的字段，其余不动；不要重写整个文件）：

```python
@dataclass(frozen=True)
class BarFact:
    """一根 bar 上的一次观测: 特征值 + 其后一根 bar 的收益."""

    symbol: str
    ts_ms: int
    factor: float
    forward_return: float
    history: int = 0   # 该观测可用到的历史 bar 数; 跨快照去重时用来挑最深的一条
```

然后追加以下内容：

```python
from collections.abc import Callable, Iterable

from pa_agent.ai.kline_features import compute_kline_geometry_features
from pa_agent.data.base import KlineFrame
from pa_agent.data.snapshot import compute_indicators

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
    """同一根 bar 会被多份滚动快照反复覆盖, 按 (symbol, ts_ms) 只保留历史最深的一条.

    实测(2026-09-21, BTCUSDT 15m): 616 份记录里只有 1417 根不同的 bar, 99.4% 的 bar
    被多份记录覆盖, 单根最多出现 90 次 —— 不去重会把样本量虚增约 43 倍, 让 min_obs
    门槛与月度胜率全部失真。同一根 bar 在不同快照里能看到的**历史长度**不同(旧快照更短),
    特征值因此可能不同; 取历史最深的一条, 与生产路径(历史充足)的语义一致。
    """
    best: dict[tuple[str, int], BarFact] = {}
    for fact in facts:
        key = (fact.symbol, fact.ts_ms)
        current = best.get(key)
        if current is None or fact.history > current.history:
            best[key] = fact
    return sorted(best.values(), key=lambda f: (f.symbol, f.ts_ms))


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
```

注意：`frame_from_record_klines` 在 Task 1.3 才用，本步不要提前引入。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: PASS（17 passed）

- [ ] **Step 5: lint + 提交**

```bash
.venv/bin/python -m ruff check --fix tools/factor_audit.py tests/unit/test_factor_audit.py
.venv/bin/python -m ruff format tools/factor_audit.py tests/unit/test_factor_audit.py
.venv/bin/python -m ruff check tools/factor_audit.py tests/unit/test_factor_audit.py
git add tools/factor_audit.py tests/unit/test_factor_audit.py
git commit -m "✨ feat(tools): 因子审计支持从落盘记录逐 bar 抽观测"
```

---

### Task 1.3：CLI 与 Markdown 报告

**Files:**
- Modify: `tools/factor_audit.py`（追加渲染与 `main`）
- Test: `tests/unit/test_factor_audit.py`（追加）

**Interfaces:**
- Consumes: `audit_frames`、`geometry_extractors`（Task 1.2）；`pa_agent.demo.record_loader.try_load_analysis_record`、`frame_from_record_klines`
- Produces:
  - `load_frames(paths: Iterable[Path]) -> list[KlineFrame]`
  - `render_markdown(stats: Sequence[FactorStats], *, frames: int, observations: int, source: str) -> str`
  - `main(argv: list[str] | None = None) -> int`（退出码 0 = 有因子通过，1 = 无因子通过，2 = 用法/路径错误）

- [ ] **Step 1: 写失败测试**

```python
from tools.factor_audit import FactorStats, main, render_markdown


def test_render_markdown_lists_factors_with_verdict():
    stats = [
        FactorStats(
            name="geometry.body_ratio",
            n_obs=120,
            group_count=3,
            ic_mean=0.05,
            ic_std=0.04,
            ir=1.25,
            monthly_win_rate=0.75,
            passed=True,
            reasons=(),
        ),
        FactorStats(
            name="geometry.range_atr_ratio",
            n_obs=120,
            group_count=3,
            ic_mean=-0.01,
            ic_std=0.05,
            ir=-0.2,
            monthly_win_rate=0.4,
            passed=False,
            reasons=("方向性 IC -0.0100 < 0.02",),
        ),
    ]
    text = render_markdown(stats, frames=2, observations=240, source="records/pending")
    assert "# 因子审计报告" in text
    assert "geometry.body_ratio" in text
    assert "direction IC" in text
    assert "通过" in text and "未通过" in text


def test_cli_returns_two_for_missing_directory(tmp_path):
    assert main(["--records", str(tmp_path / "nope")]) == 2


def test_cli_returns_two_without_records_argument():
    assert main([]) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: FAIL —— `ImportError: cannot import name 'render_markdown'`

- [ ] **Step 3: 写最小实现**

追加到 `tools/factor_audit.py`：

```python
from pathlib import Path


def load_frames(paths: Iterable[Path]) -> list[KlineFrame]:
    """把落盘的分析记录读成 KlineFrame; 坏文件跳过而不是中断."""
    from pa_agent.demo.record_loader import frame_from_record_klines, try_load_analysis_record

    frames: list[KlineFrame] = []
    for path in paths:
        record = try_load_analysis_record(Path(path))
        if record is None or not record.kline_data:
            continue
        frames.append(
            frame_from_record_klines(
                record.kline_data,
                symbol=record.meta.symbol,
                timeframe=record.meta.timeframe,
                snapshot_ts_local_ms=record.meta.timestamp_local_ms,
            )
        )
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
        f"- 样本：{frames} 份记录 / {observations} 次观测",
        f"- 结论：{len(passed)} / {len(stats)} 个因子通过准入",
        "",
        "| 因子 | 观测 | 分组 | IC 均值 | IR | 月度胜率 | 结论 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in stats:
        verdict = "通过" if item.passed else "未通过"
        lines.append(
            f"| {item.name} | {item.n_obs} | {item.group_count} | "
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
    parser.add_argument("--limit", type=int, default=300, help="最多读入多少份记录")
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
        print(render_markdown(stats, frames=len(frames), observations=observations, source=str(root)))

    if args.out:
        Path(args.out).write_text(
            render_markdown(stats, frames=len(frames), observations=observations, source=str(root)),
            encoding="utf-8",
        )
    return 0 if any(s.passed for s in stats) else 1
```

同时在文件顶部补齐本步新用到的 import：`argparse`、`json`、`sys`、`dataclasses.asdict`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: PASS（20 passed）

- [ ] **Step 5: 真机冒烟 + 提交**

Run: `.venv/bin/python tools/factor_audit.py --records records/pending --limit 200 --out /tmp/factor_audit.md`
Expected: 打印报告；退出码 0 或 1 均可（0 = 有因子通过，1 = 全部未通过，两者都是有效结论）

```bash
.venv/bin/python -m ruff format tools/factor_audit.py
.venv/bin/python -m ruff check tools/factor_audit.py tests/unit/test_factor_audit.py
git add tools/factor_audit.py tests/unit/test_factor_audit.py
git commit -m "✨ feat(tools): 因子审计加 CLI 与 Markdown 报告"
```

---

### Task 1.4：接入结构特征（SimpleMarketFeatures）

**Files:**
- Modify: `tools/factor_audit.py`（追加一个抽取函数工厂）
- Test: `tests/unit/test_factor_audit.py`（追加）

**Interfaces:**
- Consumes: `compute_simple_market_features(frame, *, lookback=40) -> SimpleMarketFeatures`
- Produces: `market_extractors() -> dict[str, Extractor]`，键前缀 `market.`

**字段清单（已核实，直接用，勿猜测）**：可从 `SimpleMarketFeatures` 取的数值字段共 13 个 ——
`range_high`、`range_low`、`range_width_atr`、`price_position`、`dist_to_high_atr`、`dist_to_low_atr`、`overlap_mean_10`、`doji_inside_ratio_10`、`barbwire_score`、`pullback_depth_atr`、`pullback_bars`、`invalidation_long`、`invalidation_short`。

- [ ] **Step 1: 写失败测试**

```python
from tools.factor_audit import market_extractors


def test_market_extractors_cover_the_numeric_fields():
    names = set(market_extractors())
    assert "market.range_width_atr" in names
    assert "market.price_position" in names
    assert "market.barbwire_score" in names
    assert "market.zone" not in names  # 分类字段不是因子
    assert "market.swings" not in names


def test_market_extractor_reads_the_newest_bar():
    frame = _frame([100.0 + i for i in range(60)])
    value = market_extractors()["market.price_position"](frame)
    assert value is None or 0.0 <= value <= 1.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: FAIL —— `ImportError: cannot import name 'market_extractors'`

- [ ] **Step 3: 写最小实现**

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: PASS（22 passed）

- [ ] **Step 5: 提交**

```bash
.venv/bin/python -m ruff check --fix tools/factor_audit.py tests/unit/test_factor_audit.py
.venv/bin/python -m ruff format tools/factor_audit.py tests/unit/test_factor_audit.py
.venv/bin/python -m ruff check tools/factor_audit.py tests/unit/test_factor_audit.py
git add tools/factor_audit.py tests/unit/test_factor_audit.py
git commit -m "✨ feat(tools): 因子审计接入结构特征(13 个数值字段)"
```

---

## Phase 2：SQLite 因子库（P1）

产出：审计通过的因子成为可检索资产，失败的留档但不参与后续。

### Task 2.1：因子库存储层

**Files:**
- Create: `pa_agent/factors/__init__.py`、`pa_agent/factors/store.py`
- Test: `tests/unit/test_factor_store.py`

**Interfaces:**
- Produces:
  - `FactorRecord(name, definition, source, ic_mean, ic_std, ir, monthly_win_rate, n_obs, status, created_at, updated_at)`
  - `FactorStore(db_path: str | Path = ":memory:")`，支持 `with` 上下文
  - `.upsert(record) -> None` / `.get(name) -> FactorRecord | None` / `.list_all(*, status="active") -> list[FactorRecord]` / `.set_status(name, status) -> None` / `.close() -> None`
- 默认库路径：`Path.home() / ".pa_agent" / "factors.db"`（**不进仓库**，规避根 conftest 的 fs 闸门）

- [ ] **Step 1: 写失败测试**

```python
"""因子库的读写与状态流转."""
from __future__ import annotations

import pytest

from pa_agent.factors.store import FactorRecord, FactorStore


def _record(name: str = "geometry.body_ratio", **overrides) -> FactorRecord:
    data = {
        "name": name,
        "definition": "geometry.body_ratio",
        "source": "kline_geometry",
        "ic_mean": 0.05,
        "ic_std": 0.04,
        "ir": 1.25,
        "monthly_win_rate": 0.75,
        "n_obs": 120,
        "status": "active",
        "created_at": "2026-09-21T00:00:00+08:00",
        "updated_at": "2026-09-21T00:00:00+08:00",
    }
    data.update(overrides)
    return FactorRecord(**data)


def test_upsert_then_get_round_trip(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        store.upsert(_record())
        loaded = store.get("geometry.body_ratio")
    assert loaded is not None
    assert loaded.ir == 1.25
    assert loaded.status == "active"


def test_upsert_is_idempotent_and_updates_metrics(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        store.upsert(_record())
        store.upsert(_record(ir=2.0, n_obs=200))
        loaded = store.get("geometry.body_ratio")
    assert loaded is not None
    assert loaded.ir == 2.0
    assert loaded.n_obs == 200


def test_list_all_filters_by_status(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        store.upsert(_record("good"))
        store.upsert(_record("bad", status="failed"))
        assert [r.name for r in store.list_all()] == ["good"]
        assert {r.name for r in store.list_all(status="failed")} == {"bad"}
        assert {r.name for r in store.list_all(status=None)} == {"good", "bad"}


def test_set_status_persists(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        store.upsert(_record())
        store.set_status("geometry.body_ratio", "retired")
        loaded = store.get("geometry.body_ratio")
    assert loaded is not None
    assert loaded.status == "retired"


def test_set_status_rejects_unknown_status(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        store.upsert(_record())
        with pytest.raises(ValueError):
            store.set_status("geometry.body_ratio", "whatever")


def test_get_returns_none_for_unknown_factor(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        assert store.get("nope") is None


def test_default_db_path_lives_outside_the_repo():
    from pathlib import Path

    from pa_agent.factors.store import default_db_path

    resolved = Path(default_db_path())
    assert resolved.name == "factors.db"
    assert ".pa_agent" in resolved.parts
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_store.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'pa_agent.factors'`

- [ ] **Step 3: 写最小实现**

```python
# pa_agent/factors/store.py
"""SQLite 因子库: 落因子定义、审计指标与状态.

移植自 alphaevo 的 alpha_factory/factor_store.py (Apache-2.0), 精简为
本仓库需要的字段 —— 去掉 usage_count 与 code 执行, 保留"定义字符串"以
保持只读与可审计(本仓库不动态执行模型生成的代码).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pa_agent.util.timefmt import now_local_ms

FactorStatus = Literal["active", "retired", "failed"]

VALID_STATUSES: frozenset[str] = frozenset({"active", "retired", "failed"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
    name TEXT PRIMARY KEY,
    definition TEXT NOT NULL,
    source TEXT NOT NULL,
    ic_mean REAL NOT NULL DEFAULT 0.0,
    ic_std REAL NOT NULL DEFAULT 0.0,
    ir REAL NOT NULL DEFAULT 0.0,
    monthly_win_rate REAL NOT NULL DEFAULT 0.0,
    n_obs INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_factors_status ON factors(status);
CREATE INDEX IF NOT EXISTS idx_factors_ir ON factors(ir);
"""


def default_db_path() -> Path:
    """因子库默认位置: 用户目录, 不进仓库(仓库根 conftest 会拦 records/ 写入)."""
    return Path.home() / ".pa_agent" / "factors.db"


@dataclass(frozen=True)
class FactorRecord:
    name: str
    definition: str
    source: str
    ic_mean: float
    ic_std: float
    ir: float
    monthly_win_rate: float
    n_obs: int
    status: FactorStatus = "active"
    created_at: str = ""
    updated_at: str = ""


class FactorStore:
    """因子库句柄; 支持 with 语句."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self) -> FactorStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def upsert(self, record: FactorRecord) -> None:
        now = record.updated_at or _now_iso()
        created = record.created_at or now
        self._conn.execute(
            """
            INSERT INTO factors (name, definition, source, ic_mean, ic_std, ir,
                                 monthly_win_rate, n_obs, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                definition = excluded.definition,
                source = excluded.source,
                ic_mean = excluded.ic_mean,
                ic_std = excluded.ic_std,
                ir = excluded.ir,
                monthly_win_rate = excluded.monthly_win_rate,
                n_obs = excluded.n_obs,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                record.name,
                record.definition,
                record.source,
                record.ic_mean,
                record.ic_std,
                record.ir,
                record.monthly_win_rate,
                record.n_obs,
                record.status,
                created,
                now,
            ),
        )
        self._conn.commit()

    def get(self, name: str) -> FactorRecord | None:
        row = self._conn.execute("SELECT * FROM factors WHERE name = ?", (name,)).fetchone()
        return None if row is None else _to_record(row)

    def list_all(self, *, status: str | None = "active") -> list[FactorRecord]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM factors ORDER BY ir DESC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM factors WHERE status = ? ORDER BY ir DESC", (status,)
            ).fetchall()
        return [_to_record(row) for row in rows]

    def set_status(self, name: str, status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态 {status!r}; 允许: {sorted(VALID_STATUSES)}")
        self._conn.execute(
            "UPDATE factors SET status = ?, updated_at = ? WHERE name = ?",
            (status, _now_iso(), name),
        )
        self._conn.commit()


def _to_record(row: sqlite3.Row) -> FactorRecord:
    return FactorRecord(
        name=row["name"],
        definition=row["definition"],
        source=row["source"],
        ic_mean=row["ic_mean"],
        ic_std=row["ic_std"],
        ir=row["ir"],
        monthly_win_rate=row["monthly_win_rate"],
        n_obs=row["n_obs"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now_iso() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.fromtimestamp(
        now_local_ms() / 1000.0, tz=timezone(timedelta(hours=8))
    ).isoformat()
```

`pa_agent/factors/__init__.py` 导出公共名：

```python
"""因子研究资产: 审计指标入库、成熟度准入."""
from pa_agent.factors.store import FactorRecord, FactorStore, default_db_path

__all__ = ["FactorRecord", "FactorStore", "default_db_path"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_store.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
.venv/bin/python -m ruff format pa_agent/factors tests/unit/test_factor_store.py
.venv/bin/python -m ruff check pa_agent/factors tests/unit/test_factor_store.py
git add pa_agent/factors tests/unit/test_factor_store.py
git commit -m "✨ feat(factors): 加 SQLite 因子库(定义/指标/状态流转)"
```

---

### Task 2.2：审计结果入库

**Files:**
- Modify: `tools/factor_audit.py`（追加 `persist_stats`）
- Test: `tests/unit/test_factor_audit.py`（追加）

**Interfaces:**
- Consumes: `FactorStats`（Task 1.1）、`FactorStore` / `FactorRecord`（Task 2.1）
- Produces: `persist_stats(stats: Sequence[FactorStats], store: FactorStore, *, source: str) -> tuple[int, int]` —— 返回（通过数，落库数）

- [ ] **Step 1: 写失败测试**

```python
from pa_agent.factors.store import FactorStore
from tools.factor_audit import FactorStats, persist_stats


def _stats(name: str, *, passed: bool) -> FactorStats:
    return FactorStats(
        name=name,
        n_obs=100,
        group_count=3,
        ic_mean=0.05 if passed else -0.01,
        ic_std=0.04,
        ir=1.2 if passed else -0.1,
        monthly_win_rate=0.7 if passed else 0.3,
        passed=passed,
        reasons=() if passed else ("方向性 IR -0.1000 < 0.3",),
    )


def test_persist_stats_marks_passers_active_and_failers_failed(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        passed_count, stored = persist_stats(
            [_stats("good", passed=True), _stats("bad", passed=False)],
            store,
            source="kline_geometry",
        )
    assert passed_count == 1
    assert stored == 2
    with FactorStore(tmp_path / "factors.db") as store:
        assert [r.name for r in store.list_all()] == ["good"]
        assert {r.name for r in store.list_all(status="failed")} == {"bad"}


def test_persist_stats_keeps_definition_and_source(tmp_path):
    with FactorStore(tmp_path / "factors.db") as store:
        persist_stats([_stats("geometry.body_ratio", passed=True)], store, source="kline_geometry")
        record = store.get("geometry.body_ratio")
    assert record is not None
    assert record.definition == "geometry.body_ratio"
    assert record.source == "kline_geometry"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: FAIL —— `ImportError: cannot import name 'persist_stats'`

- [ ] **Step 3: 写最小实现**

```python
def persist_stats(
    stats: Sequence[FactorStats],
    store: FactorStore,
    *,
    source: str,
) -> tuple[int, int]:
    """把审计结论落库: 通过的标 active, 未通过的标 failed(留档但不参与后续)."""
    for item in stats:
        store.upsert(
            FactorRecord(
                name=item.name,
                definition=item.name,
                source=source,
                ic_mean=item.ic_mean,
                ic_std=item.ic_std,
                ir=item.ir,
                monthly_win_rate=item.monthly_win_rate,
                n_obs=item.n_obs,
                status="active" if item.passed else "failed",
            )
        )
    return sum(1 for item in stats if item.passed), len(stats)
```

顶部补 import：`from pa_agent.factors.store import FactorRecord, FactorStore`。
CLI 加 `--db` 参数（默认 `default_db_path()`）与 `--persist` 开关；不带 `--persist` 时行为完全不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: PASS（22 passed）

- [ ] **Step 5: 提交**

```bash
.venv/bin/python -m ruff format tools/factor_audit.py
.venv/bin/python -m ruff check tools/factor_audit.py tests/unit/test_factor_audit.py
git add tools/factor_audit.py tests/unit/test_factor_audit.py
git commit -m "✨ feat(factors): 审计通过的因子自动入库, 未通过留档为 failed"
```

---

## Phase 3：成熟度检查单（P1）

### Task 3.1：检查单核心

**Files:**
- Create: `pa_agent/factors/maturity.py`
- Test: `tests/unit/test_factor_maturity.py`

**Interfaces:**
- Consumes: `FactorStats`（`tools.factor_audit`）
- Produces:
  - `MaturityCheck(check_id, title, status, evidence, recommendation)`
  - `MaturityReport(subject, overall, checks, next_action)`
  - `assess_factor(name: str, stats: FactorStats, *, thresholds: AuditThresholds | None = None) -> MaturityReport`

**四项检查与判级**（`overall` 取最差一级：任一 `fail` 则 fail，否则任一 `watch` 则 watch，否则 pass）：

| check_id | pass | watch | fail |
| --- | --- | --- | --- |
| `sample` | `n_obs >= 200` | `n_obs >= min_obs` | 其余 |
| `direction` | IC 与预期同号且 `|IC| >= min_ic_abs` | IC 同号但偏弱 | 反号 |
| `stability` | `|IR| >= 0.50` | `|IR| >= min_ir` | 其余 |
| `monthly` | 月度胜率 `>= 0.60` | `>= min_monthly_win_rate` | 其余 |

- [ ] **Step 1: 写失败测试**

```python
"""因子成熟度检查单."""
from __future__ import annotations

from pa_agent.factors.maturity import assess_factor
from tools.factor_audit import FactorStats


def _stats(**overrides) -> FactorStats:
    data = {
        "name": "geometry.body_ratio",
        "n_obs": 500,
        "group_count": 4,
        "ic_mean": 0.06,
        "ic_std": 0.04,
        "ir": 1.5,
        "monthly_win_rate": 0.75,
        "passed": True,
        "reasons": (),
    }
    data.update(overrides)
    return FactorStats(**data)


def test_strong_factor_passes_every_check():
    report = assess_factor("geometry.body_ratio", _stats())
    assert report.overall == "pass"
    assert {c.check_id for c in report.checks} == {"sample", "direction", "stability", "monthly"}
    assert all(c.status == "pass" for c in report.checks)
    assert "无需" in report.next_action


def test_thin_sample_is_a_watch_and_recommends_expanding():
    report = assess_factor("geometry.body_ratio", _stats(n_obs=61))
    sample = next(c for c in report.checks if c.check_id == "sample")
    assert sample.status == "watch"
    assert report.overall == "watch"
    assert "records" in report.next_action


def test_direction_flip_is_a_fail():
    report = assess_factor("geometry.body_ratio", _stats(ic_mean=-0.06, ir=-1.5, passed=False))
    assert report.overall == "fail"
    assert next(c for c in report.checks if c.check_id == "direction").status == "fail"


def test_low_stability_is_watch_or_fail_by_threshold():
    watch = assess_factor("x", _stats(ir=0.4, passed=False))
    assert next(c for c in watch.checks if c.check_id == "stability").status == "watch"
    fail = assess_factor("x", _stats(ir=0.1, passed=False))
    assert next(c for c in fail.checks if c.check_id == "stability").status == "fail"


def test_overall_takes_the_worst_level():
    report = assess_factor("x", _stats(n_obs=60, ir=0.1, passed=False))
    assert report.overall == "fail"


def test_every_check_carries_evidence_and_recommendation():
    report = assess_factor("x", _stats(n_obs=60, passed=False))
    for check in report.checks:
        assert check.evidence
        assert check.recommendation
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_maturity.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'pa_agent.factors.maturity'`

- [ ] **Step 3: 写最小实现**

```python
"""因子成熟度检查单: 把"够不够格进下一步"变成结构化结论 + 一条可执行建议.

机制借鉴 alphaevo 的 evaluator/maturity.py (Apache-2.0): 不做新的统计, 只把
已有指标翻译成 pass/watch/fail 三档, 并把不过的闸门映射成具体的下一步动作.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tools.factor_audit import AuditThresholds, FactorStats

MaturityStatus = Literal["pass", "watch", "fail"]

_ORDER: dict[str, int] = {"pass": 0, "watch": 1, "fail": 2}

#: 比准入阈值更严的"完全成熟"门槛
STRONG_OBS = 200
STRONG_IR = 0.50
STRONG_MONTHLY_WIN_RATE = 0.60


@dataclass(frozen=True)
class MaturityCheck:
    check_id: str
    title: str
    status: MaturityStatus
    evidence: tuple[str, ...]
    recommendation: str


@dataclass(frozen=True)
class MaturityReport:
    subject: str
    overall: MaturityStatus
    checks: tuple[MaturityCheck, ...]
    next_action: str


def _sample_check(stats: FactorStats, limits: AuditThresholds) -> MaturityCheck:
    if stats.n_obs >= STRONG_OBS:
        status: MaturityStatus = "pass"
    elif stats.n_obs >= limits.min_obs:
        status = "watch"
    else:
        status = "fail"
    return MaturityCheck(
        check_id="sample",
        title="样本量",
        status=status,
        evidence=(f"观测 {stats.n_obs} / 分品种组 {stats.group_count}",),
        recommendation="补样本: python tools/factor_audit.py --records records/pending --limit 800",
    )


def _direction_check(stats: FactorStats, limits: AuditThresholds) -> MaturityCheck:
    if stats.ic_mean > 0 and abs(stats.ic_mean) >= limits.min_ic_abs:
        status: MaturityStatus = "pass"
    elif stats.ic_mean > 0:
        status = "watch"
    else:
        status = "fail"
    return MaturityCheck(
        check_id="direction",
        title="方向一致性",
        status=status,
        evidence=(f"IC 均值 {stats.ic_mean:.4f} (要求正号且 |IC| >= {limits.min_ic_abs})",),
        recommendation="方向反了: 要么取反号重测, 要么直接淘汰该特征",
    )


def _stability_check(stats: FactorStats, limits: AuditThresholds) -> MaturityCheck:
    if abs(stats.ir) >= STRONG_IR:
        status: MaturityStatus = "pass"
    elif abs(stats.ir) >= limits.min_ir:
        status = "watch"
    else:
        status = "fail"
    return MaturityCheck(
        check_id="stability",
        title="跨品种稳定性(IR)",
        status=status,
        evidence=(f"IR {stats.ir:.4f} (强门槛 {STRONG_IR}, 准入 {limits.min_ir})",),
        recommendation="不稳: 按品种分别看 IC, 找出拖后腿的品种后限缩适用范围",
    )


def _monthly_check(stats: FactorStats, limits: AuditThresholds) -> MaturityCheck:
    if stats.monthly_win_rate >= STRONG_MONTHLY_WIN_RATE:
        status: MaturityStatus = "pass"
    elif stats.monthly_win_rate >= limits.min_monthly_win_rate:
        status = "watch"
    else:
        status = "fail"
    return MaturityCheck(
        check_id="monthly",
        title="月度胜率",
        status=status,
        evidence=(f"月度胜率 {stats.monthly_win_rate:.0%} (强门槛 {STRONG_MONTHLY_WIN_RATE:.0%})",),
        recommendation="按月回看: 检查是否只在某一种周期位置有效, 若是则只在该位置启用",
    )


def assess_factor(
    name: str,
    stats: FactorStats,
    *,
    thresholds: AuditThresholds | None = None,
) -> MaturityReport:
    """给一个因子出成熟度检查单."""
    limits = thresholds or AuditThresholds()
    checks = (
        _sample_check(stats, limits),
        _direction_check(stats, limits),
        _stability_check(stats, limits),
        _monthly_check(stats, limits),
    )
    overall: MaturityStatus = max(
        (check.status for check in checks), key=lambda level: _ORDER[level]
    )
    if overall == "pass":
        next_action = "无需额外动作: 该因子可直接进入策略层试配"
    else:
        blocking = next(
            check for check in checks if check.status == overall
        )
        next_action = blocking.recommendation
    return MaturityReport(
        subject=name,
        overall=overall,
        checks=checks,
        next_action=next_action,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_maturity.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
.venv/bin/python -m ruff format pa_agent/factors tests/unit/test_factor_maturity.py
.venv/bin/python -m ruff check pa_agent/factors tests/unit/test_factor_maturity.py
git add pa_agent/factors tests/unit/test_factor_maturity.py
git commit -m "✨ feat(factors): 加因子成熟度检查单与下一步动作推荐"
```

---

### Task 3.2：把检查单接进审计报告

**Files:**
- Modify: `pa_agent/factors/__init__.py`、`tools/factor_audit.py`
- Test: `tests/unit/test_factor_audit.py`（追加）

**Interfaces:**
- Consumes: `assess_factor`（Task 3.1）、`render_markdown`（Task 1.3）
- Produces: `render_markdown(..., maturity: Mapping[str, MaturityReport] | None = None)`（新增可选参数；省略时输出与 Task 1.3 完全一致）

- [ ] **Step 1: 写失败测试**

```python
from pa_agent.factors.maturity import assess_factor
from tools.factor_audit import FactorStats, render_markdown


def test_render_markdown_appends_maturity_section():
    stats = FactorStats(
        name="geometry.body_ratio",
        n_obs=500,
        group_count=4,
        ic_mean=0.06,
        ic_std=0.04,
        ir=1.5,
        monthly_win_rate=0.75,
        passed=True,
        reasons=(),
    )
    maturity = {stats.name: assess_factor(stats.name, stats)}
    text = render_markdown(
        [stats], frames=2, observations=500, source="records/pending", maturity=maturity
    )
    assert "## 成熟度检查单" in text
    assert "跨品种稳定性(IR)" in text
    assert "无需额外动作" in text


def test_render_markdown_without_maturity_is_unchanged():
    stats = FactorStats(
        name="x",
        n_obs=10,
        group_count=1,
        ic_mean=0.0,
        ic_std=0.0,
        ir=0.0,
        monthly_win_rate=0.0,
        passed=False,
        reasons=("观测数 10 < 30",),
    )
    text = render_markdown([stats], frames=1, observations=10, source="s")
    assert "## 成熟度检查单" not in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py -q`
Expected: FAIL —— `TypeError: render_markdown() got an unexpected keyword argument 'maturity'`

- [ ] **Step 3: 写最小实现**

在 `render_markdown` 签名加 `maturity: Mapping[str, MaturityReport] | None = None`，并在返回前追加：

```python
    if maturity:
        lines.extend(["", "## 成熟度检查单", ""])
        for name, report in sorted(maturity.items()):
            lines.append(f"### {name} — {report.overall}")
            lines.append("")
            for check in report.checks:
                lines.append(f"- [{check.status}] {check.title}: {'; '.join(check.evidence)}")
            lines.append("")
            lines.append(f"下一步：{report.next_action}")
            lines.append("")
```

CLI 增加 `--maturity` 开关：打开时对每个因子调用 `assess_factor` 并传入渲染。
`pa_agent/factors/__init__.py` 补导出 `MaturityCheck / MaturityReport / assess_factor`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_factor_audit.py tests/unit/test_factor_maturity.py -q`
Expected: PASS（30 passed）

- [ ] **Step 5: 全量回归 + 提交**

```bash
.venv/bin/python -m pytest tests/unit/test_factor_audit.py tests/unit/test_factor_store.py tests/unit/test_factor_maturity.py tests/unit/test_feature_guard.py -q
.venv/bin/python -m ruff check pa_agent/factors tools/factor_audit.py
.venv/bin/python -m ruff format --check pa_agent/factors tools/factor_audit.py
git add pa_agent/factors tools/factor_audit.py tests/unit/test_factor_audit.py
git commit -m "✨ feat(factors): 审计报告接入成熟度检查单"
```

---

## Phase 4 / 5：后续独立计划（建议拆分，本计划不展开）

按 writing-plans 的 scope check：下面两块各自是独立子系统，**应当各写一份计划**，不要塞进本文件。

**Phase 4：反过拟合三闸**
alphaevo 用 `train_val_gap / val_test_gap / walk_forward_pass_rate` 三个门拒掉"样本内变好看、样本外没有"的候选。本仓库要落地它，前提是先把样本按时间切成三段并做滚动窗口验证 —— 这需要先确定"策略版本纪元"的口径（`outcomes.csv` 已有 `era` 列可复用）。**前置依赖：Phase 1 的观测抽取。**

**Phase 5：自动进化环**
把已有的 `tools/ab_intent.py` / `ab_loop.py` / `variance_batch.py` 从"人工跑对照"升级成"诊断失败 → 提议变异 → 重测 → 过了才留档"的闭环，对应 alphaevo 的 `reflection/` + `orchestrator/evolution.py`。**前置依赖：Phase 3 的检查单（闭环的准入判据）。**

---

## Self-Review

按 writing-plans 要求对上面的计划做了一遍自查：

**1. Spec 覆盖**
- 审计既有 K 线特征 → Task 1.1 / 1.2 / 1.3 / 1.4 ✅
- 因子存档 → Task 2.1 / 2.2 ✅
- 准入检查单 → Task 3.1 / 3.2 ✅
- 反过拟合 / 进化环 → 明确划为 Phase 4/5 独立计划 ✅
- 与 feature_guard 打通 → 已列为基线并说明关系；**未**在本计划强制调用（当前审计输入是仓库内置特征，不是 LLM 生成代码）。若将来审计 LLM 生成的因子，Task 1.4 的抽取函数工厂就是插入点。

**2. 占位符扫描**
无 `TBD` / `TODO` / "适当处理错误" / "类似 Task N"。所有代码块可直接粘贴运行。所有字段名（`bars[0]` 最新、`RecordMeta.timestamp_local_ms`、13 个 `market.*` 字段、6 个 `geometry.*` 字段）均在本仓库实际读取后写入，未凭记忆填写。

**3. 类型一致性**
- `BarFact` / `FactorStats` / `AuditThresholds` 在 Task 1.1 定义，Task 1.2 / 1.3 / 2.2 / 3.1 引用自同一处 ✅
- `FactorStore` / `FactorRecord` 在 Task 2.1 定义，Task 2.2 引用 ✅
- `render_markdown` 在 Task 1.3 定义签名，Task 3.2 只**追加可选参数**，向后兼容 ✅
- 测试计数：1.1 = 10、1.2 = +7（17）、1.3 = +3（20）、1.4 = +2（22）、2.2 = +2（24）、3.1 = 6（独立文件 test_factor_maturity.py）、3.2 = +2（26）；总计 26 + 6 = 32 ✅

**4. 已实跑验证（不是纸上推演）**
本计划的代码在落稿前后各跑过一次，验证方式是把计划里的代码块原样抄到 `/tmp/fa/` 下，用仓库同一个 `.venv` 执行：

| 验证对象 | 命令 | 结果 |
| --- | --- | --- |
| Task 1.1 统计核心（10 用例） | `PYTHONPATH=/tmp/fa .venv/bin/python -m pytest tests/test_factor_audit.py -q` | ✅ 10 passed |
| Task 1.2 观测抽取（5 用例） | `PYTHONPATH=/tmp/fa .venv/bin/python -m pytest tests/test_task12.py -q` | ✅ 5 passed |
| Task 3.1 成熟度检查单（6 断言） | 按文件路径加载 maturity.py 后逐条断言 | ✅ 6/6 通过 |
| Task 1.2 去重必要性（真数据） | `python /tmp/dedupe_probe.py` | ✅ 616 份记录仅 1417 根不同 bar，99.4% 重复，最多 90 次 |

**这次实跑改掉了计划里的一个真 bug**：`ic_stats` 原实现无条件判定 IR，当只有 1 个分品种组时 `ic_std == 0` 导致 `ir == 0.0`，于是任何单品种因子都会被误报"方向性 IR 0.0000 < 0.3"。正确语义是 **IR 在组数 < 2 时无定义**，已加 `len(per_group_ic) >= 2` 守卫，并已把修正写回上面的 Step 3 代码块。

**已知风险（不回避）**

1. ~~滚动快照重复覆盖可能高估有效样本量。~~ **已用真数据证实并已在本计划内修掉。** 2026-09-21 探测 BTCUSDT 15m：616 份记录、每份中位 100 根 bar，但只有 **1417 根不同 bar**；**99.4%** 的 bar 被多份记录覆盖，单根最多被 **90 份**覆盖，朴素池化虚增约 **43 倍**。Task 1.2 现在含 `dedupe_facts`：按 `(symbol, ts_ms)` 只保留**历史最深**的一条（同一根 bar 在不同快照里可见历史不同，取最深的与生产路径语义一致）。
2. 阈值（`min_ic_abs=0.02 / min_ir=0.30`）直接抄自 alphaevo 的日线截面场景，**在本仓库 15m bar 上未必合适**。首次跑完后应按实际分布标定，不要当成定论。
3. `audit_frames` 对每根 bar 重建子 frame 并重算指标，复杂度 O(n²)。`max_bars=200` 已封顶；若真机太慢，先降 `--limit`，不要急着优化。

---

## 执行方式

计划完成。两种执行选择：

1. **Subagent-Driven（推荐）** —— 每个 Task 派一个全新 subagent，Task 之间做两阶段评审，迭代快、上下文干净
2. **Inline Execution** —— 在本会话里用 superpowers:executing-plans 批量执行，带检查点

**建议先只做 Phase 1（Task 1.1 → 1.4）**：它自成闭环，跑完就能回答"现有 19 个特征里哪些在打空枪"。Phase 2/3 有无价值，取决于 Phase 1 的真实输出 —— 如果连一个因子都没通过，先别建库。
