# 正期望改造计划 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 PA_Agent 在忠实回放与实盘 testnet 上达到**统计上显著的正期望**（净额均值 > 0，且自助法 90% 置信区间不含 0），同时把单量从 0.3 笔/天提升到 1–3 笔/天。

**Architecture:** 先修**度量失真**（回放费用模型 2 倍于实盘、趋势标签缺失、无显著性输出），再**收敛入场配方**（方向 + 周期位置 + 盈亏比三层门控），再用**提示词修复**提高计划质量与单量，最后用**留出集 + 实盘小样**验证。任何一步不达标就回滚，不带病上线。

**Tech Stack:** Python 3.11 · pydantic v2 · pytest · ruff · 离线引擎 `tools/shadow_replay.py` · Binance USD$\cdot$M Futures Testnet

**Spec:** 本文件自带诊断证据（见下）。数据源：`records/pending/*.json`(5307 条) / `logs/shadow_rows.csv` / `trade_records/outcomes.csv`(73 笔真实成交)。

---

## 执行状态（2026-09-18 复核后更新）

| Task | 状态 | 提交 / 证据 |
|---|---|---|
| Task 0 回放费用模型校准 | ✅ 已完成 | `db54db3` · 手续费 37.04→**18.52U**，基线净 −21.15→**+1.57U** |
| Task 1 提示词补止损下限 | ✅ 已完成（实现位置与本文不同） | `8552cc5` · `prompt_assembler._render_execution_constraints` 动态渲染，非静态 §10.2 |
| Task 2 周期白名单门控 | ✅ 已完成 | `1ba44bd` · 配置默认关，实盘也保持关（理由见该 Task） |
| Task 3 回放自助法置信区间 | ⬜ 未做 | 本文 Step 1 的测试写法**跑不起来**，见「可执行性评估」 |
| Task 4 时间止损贡献量化 | ⬜ 未做，**前提已失效** | E2 已过期：实盘现在有时间止损 |
| Task 5 实盘验证协议 | ⬜ 未做 | `tools/expectancy_report.py` 未创建；本文的 loader 写法同样有 bug |

**本次新增的相关基建（不在原计划内）**

| 提交 | 内容 | 为什么和本计划相关 |
|---|---|---|
| `af74202` | 平仓腿记录平仓单 clientOrderId | Task 2/3 判定「谁平的仓」需要 |
| `2f73f58` | `tools/exit_cause_report.py` 出场成因归因 | 补上计划缺失的「出场侧」度量 |
| `67af2aa` | 本地订单账本 `pa_agent/trading/order_ledger.py` | 交易所只留 ~7 天订单，账本让归因不再过期 |

---

## Global Constraints

- Python 3.11，虚拟环境 `.venv`；所有命令用 `.venv/bin/python`（系统 python3 缺 pydantic）。
- 提交格式固定：`<emoji> <type>(scope): 中文描述`，例：`🐛 fix(tools): 修正回放手续费双重计入`。**不主动 push。**
- `config/settings.json` 是实盘配置且被 gitignore；`config/settings.example.json` 是模板且被跟踪，**两个 env 块（`binance_usdm_live` + `binance_usdm_testnet`）必须同步**。
- **所有回放命令必须带 `--trend-days 7 --trend-neutral 1.5`**，否则 CSV 的 `trend_pct` 全空、逆势闸门判定静默失效（2026-09-17 已踩过）。
- 每个新门控都要有 settings 开关，默认值与改动前一致（灰度开关，可回滚）。
- 合并前 `ruff check` 与 `pytest` 通过；不新增 lint 错误。

---

## 诊断：证据与结论

### E1 回放手续费约是实盘的 2 倍（最高优先级）

| 口径 | 平均名义 | 手续费 | 往返费率 |
|---|---|---|---|
| 回放 45 笔 | 684U | 37.04U | **12.03 bps** |
| 实盘 73 笔 | 1737U | 109.6U(合计) | **6.29 bps** |

`simulate_plan` 的费用分支（`tools/shadow_replay.py:393-397`）：

```python
if maker_fee is not None and taker_fee is not None:
    entry_fee_rate = taker_fee if entry_taker else maker_fee
    fees = entry_fee_rate * notional + taker_fee * sum(px * q for px, q in exits)
else:
    fees = fee_rate * (notional + sum(px * q for px, q in exits))
```

而 `fee_rate_from_outcomes`（`tools/shadow_replay.py:600-618`）返回的是**往返**费率
（`fees_usdt / notional` 的中位数）。兜底分支把它同时乘到「入场名义 + 出场名义」上
= **同一笔往返费收了两遍**。

**证据**：毛盈亏 **+15.90U**，净 **-21.15U**。费用 37.04U 全额吃掉毛利还倒亏 5U。

**状态：已修复（`db54db3`，2026-09-18）。** 修复时用实盘 73 笔重新定标，把三点都测了出来：

| 形态 | 实测往返费率 | 模型 |
|---|---|---|
| 两腿都 maker | **4.00 bps** | 2 × maker |
| 一腿 maker 一腿 taker（中位） | **6.01 bps** | maker + taker |
| 两腿都 taker | **8.04 bps** | 2 × taker |

`2/3 × 6.01 = 4.01 ≈ 4.00` ⇒ **maker = 1/3 往返、taker = 2/3 往返**，所以实现用
`MAKER_SHARE = 1.0 / 3.0`（本文原稿写的 0.33 是凑的，实测支持的是 2/3 这个比例）。
两处都修了：默认支路 `0.4 / 0.8`（**×1.20 超收**）→ `1/3 / 2/3`；兜底支路
`fee_rate × (入场名义 + 出场名义)`（**×2.00**）→ 每腿各取一半，合计恰好一次往返。
另修了同源派生 `tools/audit/baseline_lab.py:204`。

**修复后重跑 45 天（763 计划 / 45 成交）：手续费 37.04 → 18.52U，净额 −21.15 → +1.57U。**
即**现行闸门栈在正确费用下已不再是负期望**，本文后续所有配方的边际值都已被这次修正改写。

### E2 ~~时间止损只存在于回放，实盘没有~~ → **已失效（2026-09-18 复核）**

`DEFAULT_TIME_STOP_MIN = 720.0`（`tools/shadow_replay.py:64`），但
`grep -rn "time_stop" pa_agent/` **零命中** —— 实盘没有任何最长持有时间出场。

| 出场原因 | 笔数 | 毛U | 净U | 均U | 胜率 |
|---|---|---|---|---|---|
| stop | 25 | -81.39 | -103.68 | -4.15 | 4% |
| time_stop | 10 | +28.63 | **+22.33** | +2.23 | 70% |
| tp2 | 10 | +68.65 | **+60.21** | +6.02 | 100% |

回放靠 12 小时平仓「捞回」+22.33U，实盘里这些仓位会继续走到止损或止盈。
**回放的净额比实盘乐观，不能直接当收益预期。**

**状态：前提已失效（2026-09-18 复核）。** 实盘现在**有**最长持有时间出场：

- `pa_agent/config/settings.py` 定义 `time_stop_minutes`（上限 7 天），实盘
  `config/settings.json` 当前值 **720**（= 12 小时）；
- 实现是 `pa_agent/trading/position_manager.py` 的 Time-stop 轮询线程，到期市价清仓；
- 运行日志里 `时间止损` 9 次、`Time-stop` 31 次，说明它真的跑过。

所以 Task 4「决定是否移植」已经没有可移植的对象；它剩下的价值只有「12 小时这个值
是不是最优」——那是调参，不是移植。**Task 4 应重写为「时间止损参数寻优」，优先级降低。**

### E3 止损单高度集中，配方线索清晰

25 笔止损：`trending_tr` 12 笔、做空 15 笔、RR<1.5 16 笔、04–12 时段 9 笔。

按周期位置（45 笔全部成交）：

| 周期位置 | 笔数 | 毛U | 净U | 均U | 胜率 |
|---|---|---|---|---|---|
| broad_channel | 9 | +19.73 | **+15.16** | +1.68 | 67% |
| normal_channel | 7 | +13.06 | **+7.47** | +1.07 | 43% |
| trading_range | 6 | +11.28 | **+3.28** | +0.55 | 50% |
| trending_tr | 20 | -20.81 | **-38.35** | -1.92 | 30% |
| spike | 2 | -3.36 | -4.38 | -2.19 | 0% |
| tight_channel | 1 | -4.00 | -4.33 | -4.33 | 0% |

**费用修正后重跑（2026-09-18，`--variants` 同口径 45 天）：**

| 周期位置 | 笔数 | 净U | 均U | 胜率 |
|---|---|---|---|---|
| broad_channel | 9 | **+21.64** | +2.40 | 67% |
| normal_channel | 7 | **+10.26** | +1.47 | 43% |
| trading_range | 6 | **+7.28** | +1.21 | 50% |
| trending_tr | 20 | **−29.58** | −1.48 | 30% |
| spike | 2 | −3.87 | −1.94 | 50% |
| tight_channel | 1 | −4.16 | −4.16 | 0% |
| 合计 | 45 | +1.57 | +0.03 | 42% |

方向：做多 23 笔 **+30.58**；做空 22 笔 **−29.01**。
通道/区间三项合计 22 笔 **+39.18U**，其余 23 笔 **−37.61U**。

⚠️ 上表**全部是样本内**（同一 45 笔搜出来的），n 最小只有 1。它只支持
「trending_tr 是主要出血口」这一个结论——而实盘已经开着
`block_trending_tr_entry=True` 把这块吃掉了。

按方向 × 周期（最差与最好格子）：

| 分组 | 笔数 | 净U | 均U | 胜率 |
|---|---|---|---|---|
| 做空 + trending_tr | 13 | **-31.63** | -2.43 | 23% |
| 做多 + trading_range | 4 | **+15.41** | +3.85 | 75% |
| 做多 + normal_channel | 6 | +11.85 | +1.98 | 50% |
| 做空 + broad_channel | 5 | +10.02 | +2.00 | 60% |

候选配方实测（同样本，未做留出）：

| 配方 | 笔数 | 净U | 均U | 胜率 |
|---|---|---|---|---|
| 基线全部 | 45 | -21.15 | -0.47 | 40% |
| 禁 trending_tr | 25 | **+17.20** | +0.69 | 48% |
| 做多 | 23 | +17.08 | +0.74 | 52% |
| 做多 + 通道/区间 | 14 | **+32.40** | +2.31 | 64% |
| 做多 + 通道/区间 + RR≥1.5 | 5 | +18.53 | +3.71 | 80% |

### E4 提示词没告诉模型止损下限 → 71% 计划被判死

`_stop_distance_floor_pct`（`pa_agent/trading/binance_usdm_testnet.py:399-435`）
下限 = `max(min_stop_distance_pct, min_stop_atr_multiple × ATR%)`，实盘当前 =
`max(0.2%, 1.5 × ATR%)`。执行端在 :1231 与 :1296 两处硬拒「Stop loss too close」。

`pa_agent/ai/prompt_assembler.py` 附近 `:462-528` 有 §10.1/§10.2/§10.3 全套止损规则，
但**没有一处写出这个数值下限**。结果：

```
731 个计划
 ├─ 519 rejected_stop_floor   (71%)   ← 模型按结构定损, 达不到 1.5ATR
 ├─ 159 unfilled
 ├─ 8 no_data
 └─ 45 成交
```

**这 519 笔在 LLM 上真金白银付过推理费，产出的是执行层认不上的方案。**

**状态：已修复（`8552cc5`），但实现位置与本文不同，且本文的观测脚本是错的。**

- 实际实现：`pa_agent/ai/prompt_assembler.py` 的
  `_render_execution_constraints()`（约 :981），把**当前帧算出来的** ATR%、
  止损下限、TP1 下限、逆势规则渲染进阶段二的 user turn。
  **比本文方案更好**——本文要写静态常量，实际实现给的是每帧真实数值，所以模型看到
  的永远是当次的 `max(min_stop_distance_pct, min_stop_atr_multiple × ATR%)`。
- 本文 Step 3 要暴露的 `render_stage2_rules_for_test()` **不需要也不存在**，
  `tests/unit/test_prompt_assembler.py` 里已有对应断言（`_renders_execution_constraints` 等）。
- **Step 6 的观测脚本不可用**：它读 `records/pending/*.json` 的
  `exception.message` 找 "stop"，但 5307 条记录里 4264 条根本没有 `execution` 块，
  且真正的指标名是 `reason == "rejected_stop_floor"`，它只在
  **回放输出**（`logs/shadow_rows.csv`）里。正确写法见文末「可执行性评估」。
- 顺带一个必须说清的坑：**回放里的 71% 拒单率不会因为提示词修好而立刻下降**，
  因为回放吃的是**历史**计划单。当前实测 763 计划里 545 条
  `rejected_stop_floor`（71.4%），与本文原稿一致——但这反映的是修复前的模型产出。

### E5 现有结论的统计强度

| 口径 | 笔数 | 净U | 自助法 90% 区间 |
|---|---|---|---|
| 全不拦 | 45 | -21.15 | [-74.0, +33.6]（跨零） |
| 现行闸门栈 | 5 | +30.61 | [+17.5, +41.3] |

现行栈区间全正，但 **n=5**；当前样本只能提假设，不能下结论。
**本计划的门槛是：回放 n≥20 且区间不含 0；实盘 n≥100 且区间不含 0。**

**费用修正后的同一张表（2026-09-18，`--variants`，样本内）：**

| 配方 | 笔数 | 净U | 均U | 胜率 |
|---|---|---|---|---|
| 基线全部 | 45 | +1.57 | +0.03 | 42% |
| **禁逆势 1.5%** | 33 | **+38.21** | +1.16 | 52% |
| **禁空 + 禁 trending_tr** | 16 | **+33.30** | +2.08 | 56% |
| 禁 trending_tr | 25 | +31.15 | +1.25 | 52% |
| 禁空 + 禁 trending_tr + 禁 neutral | 14 | +31.68 | +2.26 | 57% |
| 禁做空 | 23 | +30.58 | +1.33 | 52% |
| 做多 + RR≥1.5 | 7 | +24.03 | +3.43 | 71% |
| RR≥1.5 | 16 | +16.69 | +1.04 | 50% |
| **禁 neutral 诊断** | 37 | **−9.24** | −0.25 | 38% |
| **trade_conf≥55** | 39 | **−22.49** | −0.58 | 36% |

两个**反对现行实盘配置**的信号，值得单独记下来：

1. `block_neutral_diag_entry`（实盘 **true**）：去掉 neutral 诊断单后净额反而降了
   10.81U（+1.57 → −9.24）⇒ 这批单在这段样本里是**正贡献**，闸门在伤害它。
2. `trade_conf>=55`：净 −22.49U ⇒ **置信度门槛依旧是负贡献**，与出场归因那一轮的
   结论一致（92 笔里 conf 55-59 是最大亏损桶、60-64 均亏 −19.89）。

两条都是样本内小 n，**不足以直接改实盘**；但它们的方向是「关掉一个已开的闸门」，
比「再开一个新闸门」风险低得多，值得优先用 Task 3 的区间来判。

---

## 文件结构（本计划会动到的文件）

| 文件 | 职责 | 动作 |
|---|---|---|
| `tools/shadow_replay.py` | 离线回放与统计 | 改费用模型、加自助法区间、加配方变体 |
| `pa_agent/ai/prompt_assembler.py` | 提示词装配 | §10.2 补止损下限规则 |
| `pa_agent/config/settings.py` | 配置模型 | 新增 `block_cycle_allowlist` |
| `pa_agent/trading/signal_pipeline.py` | 入口门控 | `_entry_block_reasons` 加周期白名单判定 |
| `config/settings.example.json` | 配置模板（跟踪） | 同步新键（两个 env 块） |
| `config/settings.json` | 实盘配置（gitignore） | 灰度开闸 |
| `tests/unit/test_shadow_replay_fees.py` | 新增 | 费用模型回归测试 |
| `tests/unit/test_signal_pipeline.py` | 已有 | 周期白名单门控测试 |
| `tools/expectancy_report.py` | 新增 | 统一出「配方 → 笔数/净/均/胜率/自助区间」报告 |

---

### Task 0: 回放费用模型校准 ✅ 已完成（`db54db3`）

> **实际落地与本文的差异**：① 本文写的 `MAKER_SHARE = 0.33`，实测支持的是
> `1.0/3.0`（见 E1 的三点定标）；② 测试的 loader 写法本身跑不起来（本文件已修正）；
> ③ Step 6 要求的 `docs/正期望改造计划-基线.md` **没有创建**——基线数字记录在本文件
> 的 E1/执行状态小节里，没另开文档。

**为什么先做：** 费用是净亏的第一大来源（E1）。费用模型错 2 倍时，后面所有配方的
边际判断都可能是假的。

**Files:**
- Modify: `tools/shadow_replay.py:393-397`（`simulate_plan` 费用分支）
- Modify: `tools/shadow_replay.py:725-727, 775-785`（费率派生）
- Test: `tests/unit/test_shadow_replay_fees.py`（新建）

**Interfaces:**
- Consumes: `fee_rate_from_outcomes(path) -> float`（往返费率，已存在）
- Produces: `maker_fee_rate(fee_rate) -> float` / `taker_fee_rate(fee_rate) -> float`
  —— 保证 `maker + taker == fee_rate`（单边费率之和 = 往返费率）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_shadow_replay_fees.py
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "shadow_replay", ROOT / "tools" / "shadow_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # 缺这行会炸(实测 AttributeError: 'NoneType' object has no attribute '__dict__'):
    # 不注册 sys.modules 时, 模块里的 @dataclass 取不到自己的命名空间。
    sys.modules["shadow_replay"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_side_rates_sum_to_round_trip_rate():
    mod = _load()
    maker = mod.maker_fee_rate(0.0006)
    taker = mod.taker_fee_rate(0.0006)
    assert abs(maker + taker - 0.0006) < 1e-12
    assert maker < taker


def test_total_fees_never_exceed_round_trip_rate():
    """一笔 GTC 入场 + 单次市价出场的总费用必须 ≈ fee_rate × 名义, 不得翻倍。"""
    mod = _load()
    fee_rate = 0.0006
    maker, taker = mod.maker_fee_rate(fee_rate), mod.taker_fee_rate(fee_rate)
    notional = 1000.0
    exit_notional = 1010.0  # 止盈出场, 名义略大
    fees = maker * notional + taker * exit_notional
    total_notional = notional + exit_notional
    assert fees / total_notional <= fee_rate * 1.05
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_shadow_replay_fees.py -v`
Expected: FAIL —— `module 'shadow_replay' has no attribute 'maker_fee_rate'`

- [ ] **Step 3: 实现单边费率函数并替换派生逻辑**

```python
# tools/shadow_replay.py —— 放在 fee_rate_from_outcomes 之后
MAKER_SHARE = 0.33  # maker 单边费率占往返费率的比例(实测 maker 约为 taker 的一半)


def maker_fee_rate(fee_rate):
    """单边 maker 费率。与 taker_fee_rate 之和等于往返费率。"""
    return float(fee_rate) * MAKER_SHARE


def taker_fee_rate(fee_rate):
    """单边 taker 费率。"""
    return float(fee_rate) * (1.0 - MAKER_SHARE)
```

把 `725-727` 与 `784-785` 两处的
`maker_fee = ... fee_rate * 0.4` / `taker_fee = ... fee_rate * 0.8`
替换为：

```python
maker_fee = args.maker_fee if args.maker_fee is not None else maker_fee_rate(fee_rate)
taker_fee = args.taker_fee if args.taker_fee is not None else taker_fee_rate(fee_rate)
```

并修正 `393-397` 的兜底分支，让它也按单边费率计：

```python
if maker_fee is not None and taker_fee is not None:
    entry_fee_rate = taker_fee if entry_taker else maker_fee
    fees = entry_fee_rate * notional + taker_fee * sum(px * q for px, q in exits)
else:
    # fee_rate 是往返费率: 单边取一半, 分别计入场与出场
    half = fee_rate / 2.0
    fees = half * notional + half * sum(px * q for px, q in exits)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_shadow_replay_fees.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 重新校准并与实盘对齐**

```bash
cd /Users/hc/Documents/ai/PA_Agent
.venv/bin/python tools/shadow_replay.py --replay --days 45 \
  --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
  --trend-days 7 --trend-neutral 1.5 2>&1 | head -6
```

Expected: 输出里「手续费」合计从 37.04U 降到 **18–22U**，「往返费率」显示 ≈
`6.3 bps`（与 `trade_records/outcomes.csv` 实测一致）。

- [ ] **Step 6: 记录基线数字**

把修正后的基线（笔数 / 毛 / 费 / 净 / 均 / 胜率）写进
`docs/正期望改造计划-基线.md`（新建），并在计划本文件「诊断」段落追加一行
「费用修正后基线：净 X.XXU」。

- [ ] **Step 7: 提交**

```bash
git add tools/shadow_replay.py tests/unit/test_shadow_replay_fees.py docs/正期望改造计划-基线.md
git commit -m "🐛 fix(tools): 修正回放手续费双重计入, 单边费率之和等于往返费率"
```

---

### Task 1: 提示词补上止损下限规则 ✅ 已完成（`8552cc5`，实现方式不同）

> 见 E4 末尾的状态说明：实际走的是「按帧动态渲染执行约束」，不是「静态规则文本」，
> 观测脚本也需要重写。

**为什么：** E4 —— 71% 的计划死在生成阶段。这一项**不改变策略**，只把已经付过钱的
推理拿回来，是唯一同时提升单量与期望的方向。

**Files:**
- Modify: `pa_agent/ai/prompt_assembler.py:462-528`（§10 风险收益段落）
- Test: `tests/unit/test_prompt_assembler.py`（若不存在则新建）

**Interfaces:**
- Consumes: `Settings.binance_usdm_testnet.min_stop_distance_pct`、
  `Settings.binance_usdm_testnet.min_stop_atr_multiple`（已存在，当前 0.2 / 1.5）
- Produces: 提示词中出现常量串 `止损距离下限`，供测试断言

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_prompt_assembler.py(追加)
def test_prompt_states_stop_floor(monkeypatch):
    from pa_agent.ai import prompt_assembler as pa

    text = pa.render_stage2_rules_for_test()  # 见 Step 3 暴露的测试钩子
    assert "止损距离下限" in text
    assert "min_stop_atr_multiple" in text or "ATR" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_prompt_assembler.py -k stop_floor -v`
Expected: FAIL —— `AttributeError: module ... has no attribute 'render_stage2_rules_for_test'`

- [ ] **Step 3: 在 §10.2 末尾插入规则，并暴露测试钩子**

在 `prompt_assembler.py` 的 §10.2「止损不过大」条目之后新增一条（保持与相邻条目
同样的缩进与 `- ` 列表风格）：

```text
- **止损距离下限（程序硬约束，必须先满足）**：入场价到止损价的百分比距离
  必须 ≥ max(min_stop_distance_pct, min_stop_atr_multiple × 决策 ATR%)。
  实盘参数：min_stop_distance_pct = 0.2%、min_stop_atr_multiple = 1.5。
  若按结构锚点只能给出更近的止损，则 §10.2 判「否」，并**优先放宽 entry 与
  TP1/TP2 的距离以保住盈亏比**；禁止为凑下限而缩小目标价。
```

同时新增模块级函数（便于测试，不改变原有渲染调用链）：

```python
def render_stage2_rules_for_test() -> str:
    """供测试断言阶段二规则文本，不影响生产渲染路径。"""
    return _STAGE2_RULES_TEXT
```

其中 `_STAGE2_RULES_TEXT` 就是把原有 §10 规则块提为模块级常量的结果
（纯重构，文本内容逐字不变）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_prompt_assembler.py -k stop_floor -v`
Expected: PASS

- [ ] **Step 5: 全量回归**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check pa_agent/ai/prompt_assembler.py
```

Expected: 无新增失败；ruff 错误数与 `git stash` 后基线一致。

- [ ] **Step 6: 线上观察指标（部署后 7 天）**

```bash
# 新增记录的止损下限拒单比例, 目标从 71% 降到 < 30%
.venv/bin/python - <<'PY'
import glob, json, os
from datetime import datetime, timedelta
cut = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
tot = rej = 0
for p in glob.glob("records/pending/*.json"):
    if os.path.basename(p)[:10] < cut:
        continue
    doc = json.load(open(p, encoding="utf-8"))
    if doc.get("stage2_decision") is None:
        continue
    tot += 1
    exc = doc.get("exception") or {}
    if "stop" in str(exc.get("message", "")).lower():
        rej += 1
print(f"新增 {tot} 条, 止损下限拒单 {rej} ({rej/tot*100 if tot else 0:.0f}%)")
PY
```

Expected: 拒单率 < 30%。（若 7 天后仍 > 50%，回到本任务重写规则措辞。）

- [ ] **Step 7: 提交**

```bash
git add pa_agent/ai/prompt_assembler.py tests/unit/test_prompt_assembler.py
git commit -m "✨ feat(ai): 提示词写明止损距离下限, 降低计划被执行层拒单的比例"
```

---

### Task 2: 周期白名单门控（配方收敛） ✅ 已完成（`1ba44bd`）

> **实际落地与本文的差异**：
> ① 本文 Step 1 用的 `make_pipeline` / `make_signal` fixture **不存在**；实际测试沿用
> `tests/unit/test_signal_pipeline.py` 已有的 `_env_settings` / `_env_signal` / `_patch_io`
> 三个 helper，并补了第三条「缺 cycle 字段不拦」的用例。
> ② 本文描述串写 `周期白名单 22 笔 +25.91U`，费用修正后是 **+39.18U**。
> ③ **实盘配置刻意保持 `[]`（不启用）**：实盘已开 `block_trending_tr_entry=True`，
> 白名单的增量只剩 tight_channel(1 笔) + spike(2 笔) = n=3、−8.03U，样本太薄。
> 它是一个给下一轮回放调参用的旋钮，不是该立刻开的东西。
> ④ 本文 Step 6 的期望表（禁 trending_tr ≥20 笔 / 做多+通道 ≥12 笔）**已达成但没有区间**：
> 实际 25 笔 +31.15U、做多+通道 6 笔 +14.23U——**后者 n 只有 6，不满足 n≥12 的期望**，
> 因此按本计划自己的门槛，它还不该进下一步。

**为什么：** E3 —— `trending_tr` 单格 20 笔 -38.35U；`做空+trending_tr` 13 笔
-31.63U。`block_trending_tr_entry` 已存在，但缺一个**可灰度、可回放调参的白名单**，
以便验证「只做通道/区间」配方而不改代码。

**Files:**
- Modify: `pa_agent/config/settings.py:261-267`（`block_*` 字段附近）
- Modify: `pa_agent/trading/signal_pipeline.py:208-245`（`_entry_block_reasons`）
- Modify: `config/settings.example.json`（两个 env 块）
- Test: `tests/unit/test_signal_pipeline.py`（追加）

**Interfaces:**
- Consumes: `decision["diagnosis_summary"]["cycle_position"]`（已在 :224 解析为 `cycle`）
- Produces: `Settings.<env>.block_cycle_allowlist: list[str]`；门控原因串
  `"cycle_block: ..."`（前缀用于 `gates_key` 拆分）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_signal_pipeline.py(追加)
def test_cycle_allowlist_blocks_unlisted_cycle(make_pipeline, make_signal):
    pipeline = make_pipeline(block_cycle_allowlist=["broad_channel", "normal_channel"])
    decision = {"diagnosis_summary": {"cycle_position": "trending_tr", "direction": "bullish"}}
    reasons = pipeline._entry_block_reasons(make_signal(), decision=decision)
    assert any(r.startswith("cycle_block") for r in reasons)


def test_cycle_allowlist_empty_is_noop(make_pipeline, make_signal):
    pipeline = make_pipeline(block_cycle_allowlist=[])
    decision = {"diagnosis_summary": {"cycle_position": "trending_tr", "direction": "bullish"}}
    reasons = pipeline._entry_block_reasons(make_signal(), decision=decision)
    assert not any(r.startswith("cycle_block") for r in reasons)
```

（`make_pipeline` / `make_signal` 用本文件已有的 fixture 风格补齐：`make_pipeline`
接受 `**overrides` 覆盖 `binance_usdm_testnet` 配置字段。）

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_signal_pipeline.py -k cycle_allowlist -v`
Expected: FAIL —— `TypeError: ... unexpected keyword argument 'block_cycle_allowlist'`

- [ ] **Step 3: 加配置字段**

```python
# pa_agent/config/settings.py —— 紧跟 block_neutral_diag_entry 之后
block_cycle_allowlist: list[str] = Field(
    default_factory=list,
    description="非空时只允许这些 cycle_position 入场(空=不启用)。"
                "2026-09-17 回放: trending_tr 20 笔 -38.35U, 通道/区间 22 笔 +25.91U.",
)
```

- [ ] **Step 4: 加门控判定**

```python
# pa_agent/trading/signal_pipeline.py —— _entry_block_reasons 内, cycle 解析之后
allow = list(getattr(cfg, "block_cycle_allowlist", ()) or ())
if allow and cycle and cycle not in allow:
    reasons.append(f"cycle_block: 周期 {cycle} 不在白名单 {','.join(allow)}")
```

放在 `trending_tr_block` 判定**之后**，保证两个闸门同时命中时原因串稳定。

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_signal_pipeline.py -k cycle_allowlist -v`
Expected: PASS（2 passed）

- [ ] **Step 6: 回放验证配方（不改实盘）**

```bash
.venv/bin/python tools/shadow_replay.py --replay --days 45 \
  --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
  --trend-days 7 --trend-neutral 1.5 --variants
```

对照要求（见 Task 3 加上的自助区间）：

| 配方 | 期望笔数 | 期望净U | 门槛 |
|---|---|---|---|
| 禁 trending_tr | ≥20 | > 0 | 区间不含 0 |
| 做多 + 通道/区间 | ≥12 | > 10 | 区间不含 0 |

- [ ] **Step 7: 同步示例配置**

```bash
# 两个 env 块都加, 值先给空数组(不启用)
.venv/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("config/settings.example.json")
doc = json.loads(p.read_text(encoding="utf-8"))
for env in ("binance_usdm_live", "binance_usdm_testnet"):
    doc[env].setdefault("block_cycle_allowlist", [])
p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
git diff --stat config/settings.example.json
```

- [ ] **Step 8: 提交**

```bash
git add pa_agent/config/settings.py pa_agent/trading/signal_pipeline.py \
        tests/unit/test_signal_pipeline.py config/settings.example.json
git commit -m "✨ feat(trading): 新增周期白名单入口门控, 默认关闭"
```

---

### Task 3: 回放输出自助法置信区间 ⬜ 未做（**建议优先做**，因为它是其余判断的前提）

> **本文需要修正的地方**：
> ① Step 1/5 的 loader 已在本文件修正（缺 `sys.modules` 注册）；
> ② 行号已漂移：`report_variants` 现在在 **:480**（本文写 477-521，接近），
> `summarize` 在 **:639**（本文写 679-700，**已过期 ~40 行**）；
> ③ `bootstrap_mean_ci` 的测试预期成立（`[5.0]*30` → 区间全正；`[10,-9]*15` → 跨零），
> 但 `iters=20000` × 纯 Python `rng.choice` 在 n=45 时约 90 万次抽样，单变体约 1 秒、
> 变体表 15 行约 15 秒，可接受；`--matrix` 组合更多时建议把 iters 降到 5000。
> ④ **做完它才能判 E5 那张表**：现在 +38.21 / −9.24 / −22.49 都是样本内点估计，
> 计划自己定的门槛是「区间不含 0」，所以 Task 3 必须排在改实盘配置之前。

**为什么：** E5 —— 现在所有结论都缺区间，n=5 和 n=45 被同等对待，导致反复得出
互相矛盾的结论（同一闸门的边际在两次运行间从 -11.04 跳到 0.00）。

**Files:**
- Modify: `tools/shadow_replay.py:477-521`（`report_variants`）
- Modify: `tools/shadow_replay.py:679-700`（`summarize`）
- Test: `tests/unit/test_shadow_replay_stats.py`（新建）

**Interfaces:**
- Produces: `bootstrap_mean_ci(nets: list[float], *, iters: int = 20000, alpha: float = 0.10, seed: int = 7) -> tuple[float, float]`
- Produces: 变体表新增两列 `90%下` / `90%上`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_shadow_replay_stats.py
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "shadow_replay", ROOT / "tools" / "shadow_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # 缺这行会炸(实测 AttributeError: 'NoneType' object has no attribute '__dict__'):
    # 不注册 sys.modules 时, 模块里的 @dataclass 取不到自己的命名空间。
    sys.modules["shadow_replay"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_bootstrap_ci_all_positive():
    mod = _load()
    lo, hi = mod.bootstrap_mean_ci([5.0] * 30)
    assert lo > 0 and hi > 0


def test_bootstrap_ci_straddles_zero():
    mod = _load()
    lo, hi = mod.bootstrap_mean_ci([10.0, -9.0] * 15)
    assert lo < 0 < hi


def test_bootstrap_is_deterministic_with_seed():
    mod = _load()
    assert mod.bootstrap_mean_ci([1.0, -2.0, 3.0]) == mod.bootstrap_mean_ci([1.0, -2.0, 3.0])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_shadow_replay_stats.py -v`
Expected: FAIL —— `no attribute 'bootstrap_mean_ci'`

- [ ] **Step 3: 实现并接进变体表**

```python
# tools/shadow_replay.py —— 顶部已 import random, 补上
def bootstrap_mean_ci(nets, *, iters=20000, alpha=0.10, seed=7):
    """净额均值的自助法置信区间。nets 为每笔净额(USDT)。"""
    values = [float(x) for x in nets]
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    lo = means[int(iters * (alpha / 2))]
    hi = means[int(iters * (1 - alpha / 2))]
    return (lo, hi)
```

在 `report_variants` 的表头后追加两列，并对每个变体打印
`bootstrap_mean_ci([r["net"] for r in sub])`。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_shadow_replay_stats.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 用新表复核已下过的结论**

```bash
.venv/bin/python tools/shadow_replay.py --replay --days 45 \
  --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
  --trend-days 7 --trend-neutral 1.5 --variants 2>&1 | tail -30
```

**记录到 `docs/正期望改造计划-基线.md`：** 每个变体的 n、净U、均U、90% 区间。
**只有区间不含 0 的变体才进入下一步。**

- [ ] **Step 6: 提交**

```bash
git add tools/shadow_replay.py tests/unit/test_shadow_replay_stats.py docs/正期望改造计划-基线.md
git commit -m "✨ feat(tools): 回放变体表加自助法置信区间, 无区间不下结论"
```

---

### Task 4: 量化时间止损的贡献，决定是否移植 ⬜ 未做（**前提已失效，建议重写为参数寻优**）

> E2 已过期：实盘**有**时间止损（`time_stop_minutes=720`）。「是否移植」不再有意义，
> 剩下的问题是「12 小时是不是最优值」。
> 另外本文 Step 2 的脚本有解析风险：`re.search(r"净 (-?[\d.]+)U", out)` 能匹配
> `净 1.57U`（当前输出格式），但它依赖输出措辞；更稳的是读
> `logs/shadow_rows.csv` 的 `net` 列直接聚合。

**为什么：** E2 —— 回放有 12 小时强制平仓，实盘没有。在移植前必须先知道它到底值多少，
以及那些仓位如果继续持有会怎样。

**Files:**
- Modify: `tools/shadow_replay.py`（仅加对比脚本入口，不改引擎）
- Create: `tools/time_stop_impact.py`
- 决策文档: `docs/时间止损移植评估.md`（新建）

**Interfaces:**
- Consumes: `--time-stop-min`（已存在于 `tools/shadow_replay.py:925`）
- Produces: 一份对照报告 + 明确结论（移植 / 不移植）

- [ ] **Step 1: 跑两个极端口径**

```bash
cd /Users/hc/Documents/ai/PA_Agent
for TS in 720 4320 100000; do
  echo "=== time-stop-min=$TS ==="
  .venv/bin/python tools/shadow_replay.py --replay --days 45 \
    --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
    --time-stop-min $TS --trend-days 7 --trend-neutral 1.5 2>&1 | \
    grep -E "成交 |净 |结局分布"
done
```

- [ ] **Step 2: 写对比脚本**

```python
# tools/time_stop_impact.py
"""对比不同最长持有时间下的回放净额, 量化时间止损的贡献。"""
import subprocess, sys, re

CMD = [
    sys.executable, "tools/shadow_replay.py", "--replay", "--days", "45",
    "--min-stop-atr", "1.5", "--min-stop-pct", "0.2", "--timeout-min", "30",
    "--trend-days", "7", "--trend-neutral", "1.5",
]

for ts in (720, 1440, 4320, 100000):
    out = subprocess.run(CMD + ["--time-stop-min", str(ts)],
                         capture_output=True, text=True).stdout
    net = re.search(r"净 (-?[\d.]+)U", out)
    reason = re.search(r"结局分布 (\{.*?\})", out)
    print(f"ts={ts:>6}  净={net.group(1) if net else '?':>9}U  {reason.group(1) if reason else ''}")
```

- [ ] **Step 3: 跑脚本并写评估文档**

Run: `.venv/bin/python tools/time_stop_impact.py`
Expected: 输出 4 行；把结果与「实盘无时间止损」的事实一起写进
`docs/时间止损移植评估.md`，给出明确结论。

**判据**：若「不限时」的净额显著低于 720 分钟（差距 > 10U），则时间止损**值得移植**，
另开一份子计划给 `pa_agent/monitoring/service.py` 加持仓计时器；否则**不移植**，
并在 Task 5 的门槛里把回放的 time_stop 收益剔除。

- [ ] **Step 4: 提交**

```bash
git add tools/time_stop_impact.py docs/时间止损移植评估.md
git commit -m "📝 docs(tools): 量化时间止损贡献并给出移植结论"
```

---

### Task 5: 实盘验证协议与阶段门禁 ⬜ 未做（脚本的 loader 已修正，其余可用）

> **已核实成立的部分**：`tools/expectancy_report.py` 的预期输出是对的——
> 当前 `trade_records/outcomes.csv` 73 笔，均值 **−2.19U**、胜率 **37%**，
> 判定确实应为 FAIL。本文写 `n=73 均-2.19U` 完全命中。
> **需要修正的地方**：
> ① loader（已在本文件修正）；
> ② `pa-monitor stop && pa-monitor start` 在 `pyproject.toml:47` 确实注册了，
> 但在**受沙箱限制的自动化环境里 `pa-monitor stop` 会失败**（内部用 `ps`，
> 沙箱下 `ps: Operation not permitted`）；此时改用
> `kill -TERM $(cat config/monitoring.pid)`。
> ③ Step 4 只关掉 `block_neutral_diag_entry` 是**有方向依据的**
> （E5 的「禁 neutral 诊断 = −9.24U」），但它仍然只有点估计——建议等 Task 3 出区间。
> ④ 计划发布后新增的 `67af2aa`（本地订单账本）应该写进规程的「每日检查」：
> 账本一旦为空，出场归因会在 7 天后失效。

**为什么：** E5 的 n=5 问题只能靠实盘样本解决。这一步把「什么算成功」写成硬门槛，
避免又一次用 5 笔样本改配置。

**Files:**
- Create: `tools/expectancy_report.py`
- Create: `docs/正期望验证规程.md`
- Modify: `config/settings.json`（灰度开闸，**非模板，勿提交**）

**Interfaces:**
- Consumes: `trade_records/outcomes.csv`（列：`net_usdt, risk_usdt, direction, cycle_position, diag_direction, ts_record`）
- Produces: 一行式判定输出 `PASS/FAIL`

- [ ] **Step 1: 写实盘判定脚本**

```python
# tools/expectancy_report.py
"""按硬门槛判定实盘期望是否显著为正。"""
import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sr", ROOT / "tools/shadow_replay.py")
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

MIN_N = 100
MIN_MEAN = 0.0


def main(path="trade_records/outcomes.csv"):
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8-sig"))]

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    nets = [n for n in (num(r.get("net_usdt")) for r in rows) if n is not None]
    if not nets:
        print("FAIL: 无成交记录")
        return 1
    mean = sum(nets) / len(nets)
    lo, hi = sr.bootstrap_mean_ci(nets)
    wr = sum(1 for n in nets if n > 0) / len(nets)
    ok = len(nets) >= MIN_N and mean > MIN_MEAN and lo > 0
    print(f"n={len(nets)}  均{mean:+.2f}U  胜率{wr:.0%}  90%CI[{lo:+.2f}, {hi:+.2f}]  "
          f"-> {'PASS' if ok else 'FAIL'}")
    print(f"门槛: n>={MIN_N} 且 均>0 且 CI下界>0")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
```

- [ ] **Step 2: 在当前 73 笔上跑，确认判定为 FAIL**

Run: `.venv/bin/python tools/expectancy_report.py`
Expected: `n=73 均-2.19U ... -> FAIL`（当前实盘就是负期望，这是正确行为）

- [ ] **Step 3: 写验证规程文档**

`docs/正期望验证规程.md` 必须包含：

```markdown
# 正期望验证规程

## 阶段门禁

| 阶段 | 样本 | 通过条件(全部满足) | 不通过怎么办 |
|---|---|---|---|
| R1 回放 | n>=20 | 均U>0 且 90%CI 下界>0 | 回 Task 2 换配方, 不上线 |
| R2 实盘小样 | n>=30 | 均U>0 且 最大回撤<25% | 立即回滚配置, 回 Task 2 |
| R3 实盘确认 | n>=100 | 均U>0 且 90%CI 下界>0 且 最大回撤<30% | 回滚并停止加仓 |
| R4 加仓 | R3 通过 | 单笔风险 4U -> 8U, 再验 n>=100 | 任一回撤>30% 减半 |

## 每日检查（一行）

    .venv/bin/python tools/expectancy_report.py

## 回滚清单

- config/settings.json 恢复到 config/settings.json.bak_<tag>
- 重启监控: pa-monitor stop && pa-monitor start
- 记录回滚原因到 docs/正期望验证规程.md
```

- [ ] **Step 4: 灰度开闸并重启监控**

```bash
cd /Users/hc/Documents/ai/PA_Agent
cp config/settings.json config/settings.json.bak_20260917_pre_expectancy
# 只开 R1 回放已通过的那一项门控, 一次一个; 先备份再改
.venv/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("config/settings.json")
doc = json.loads(p.read_text(encoding="utf-8"))
cfg = doc["binance_usdm_testnet"]
cfg["block_neutral_diag_entry"] = False     # Task 2 前唯一已验证的改动
p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
pa-monitor stop && pa-monitor start
```

- [ ] **Step 5: 提交（不含实盘配置）**

```bash
git status --short          # 确认 config/settings.json 未被 git add
git add tools/expectancy_report.py docs/正期望验证规程.md
git commit -m "✨ feat(tools): 新增实盘期望判定脚本与阶段验证规程"
```

---

## 可执行性评估（2026-09-18 逐条实测）

**判定口径**：一个 agent worker 拿着这条 Step，不追问、不猜，能不能跑完并拿到可验证的结果。
「能」= 可执行；「要改一处才能跑」= 有缺陷；「照抄必崩」= 不可执行。

| Task | 可执行性 | 结论 |
|---|---|---|
| Task 0 费用校准 | **有缺陷 → 已由 `db54db3` 完成** | 目标、判据、期望值都对；测试 loader 写法必崩 |
| Task 1 提示词 | **有缺陷 → 已由 `8552cc5` 完成** | 修复方向对；实现方案不如实际做法；观测脚本读错字段 |
| Task 2 周期白名单 | **有缺陷 → 已由 `1ba44bd` 完成** | 设计对；fixture 名不存在；配方的期望笔数达不到 |
| Task 3 自助法区间 | **可执行**（修正 loader + 行号后） | 算法与测试预期都成立，是全计划里最该先做的一项 |
| Task 4 时间止损 | **不可执行（前提失效）** | 要对比的对象在实盘已经存在 |
| Task 5 实盘规程 | **可执行**（修正 loader 后） | 期望输出精确命中；沙箱下停止服务的命令要换 |

### 已实测的缺陷（都有复现证据）

**D1（阻塞级）三处测试 loader 照抄必崩。**
Task 0 / Task 3 / Task 5 共用同一段：

```python
spec = importlib.util.spec_from_file_location("shadow_replay", ...)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)   # ← 崩在这
```

实测报 `AttributeError: 'NoneType' object has no attribute '__dict__'`：没注册
`sys.modules` 时，模块里的 `@dataclass` 取不到自己的命名空间。
**本文件已就地修正**（补 `import sys` + `sys.modules["shadow_replay"] = mod`）。
更省的写法是仓库既有惯例：`from tools.shadow_replay import ...`，见
`tests/unit/test_vergex_gens.py` 与本次新增的 `test_shadow_replay_fees.py`，实测可用。

**D2（阻塞级）Task 2 的 fixture 不存在。**
`make_pipeline` / `make_signal` 在 `tests/unit/test_signal_pipeline.py` 里**没有**，
照抄会得到 fixture not found。实际可用的是 `_env_settings(**flags)` /
`_env_signal(direction, cycle, diag)` / `_patch_io(monkeypatch)`。

**D3（阻塞级）Task 1 Step 6 的观测脚本读错字段。**
它从 `records/pending/*.json` 的 `exception.message` 里找 "stop"，但实测 5307 条记录里
**4264 条根本没有 `execution` 块**；真正的指标名是 `reason == "rejected_stop_floor"`，
且只出现在**回放产物**里。当前实测：763 计划 / 545 拒单 = **71.4%**，
与 E4 原稿的 519/731 一致（即提示词修好也**不会让这个数字立刻下降**，因为回放吃的是历史计划单）。

---

**D4（错误）Task 1 的测试断言对象不存在。**
`render_stage2_rules_for_test()` 与模块级 `_STAGE2_RULES_TEXT` 在仓库里都没有，
而且**不需要**——实际实现是 `_render_execution_constraints()` 按帧渲染。

**D5（过期）行号与常量漂移。**
`report_variants` 现在在 **:480**（本文写 477-521，接近），`summarize` 在 **:639**
（本文写 679-700，过期约 40 行）。Task 4 的脚本用正则抓输出措辞，更稳的做法是直接
聚合 `logs/shadow_rows.csv` 的 `net` 列。

**D6（环境）Task 5 的停服命令在受沙箱限制的环境下必失败。**
`pa-monitor stop` 内部用 `ps`，沙箱下报 ps: Operation not permitted。
改用：读 `config/monitoring.pid` 后直接 `kill -TERM` 该 pid。

### 结构性问题（不是 bug，是计划的设计）

**S1「先修度量失真」这个顺序判断是全计划最大的价值。**
费用模型错 2 倍时，所有配方的边际值都是假的。实际修完后基线从 **−21.15U 变成 +1.57U**，
「现行闸门栈是负期望」这个前提**直接消失**。把它放 Task 0 完全正确。

**S2 但 Task 3（区间）应该排在 Task 2 之前。**
计划自己定的门槛是「区间不含 0 才进下一步」，可配方筛选发生在补区间之前，
等于**先用点估计选配方、再补区间**。实测变体表有 15 行、笔数 2–45 不等，
+38.21U 与 n=2 的 +12.26U 被并列展示——这正是本计划「风险」段自己警告的数据窥探。
**建议顺序：Task 0 → Task 3 → Task 2 → Task 5。**

**S3 诊断有保鲜期，E2/E4 已经腐坏。**
E2（实盘无时间止损）已失效；E4 的 71% 是**修复前**的历史计划单，修完提示词后
回放口径不会自动改善。**「现状断言」型证据写进计划时必须带核对日期 + 一条自动复核命令**，
否则下一个 worker 会拿着过期前提干活。本文件现已加「执行状态」表来缓解。

**S4 完成定义里 R1 的门槛对现有数据不友好。**
R1 要求「n≥20 且区间不含 0」。实测能达到 n≥20 的只有：基线 45、禁空 23、
禁 trending_tr 25、禁逆势 33、RR≥1.0 45、trade_conf≥55 39。而 **做多+通道/区间只有 6 笔**、
做多+RR≥1.5 只有 7 笔——E3 里最诱人的那几个配方**结构上无法通过 R1**。
这不是计划的错，但它意味着在本样本量下「配方收敛」只能做**减法**（关掉某个闸门），
不能做**加法**（叠加条件）。建议把 R1 拆两档：单闸门 n≥20、组合闸门 n≥10。

**S5 计划缺「出场侧」的度量。**
五个 Task 全部围绕入场（方向 / 周期 / 盈亏比 / 提示词），而 2026-09-18 的出场归因显示：
92 笔里 **36% 从未走到止损距离的 1/4**，`<30m` 桶贡献 −89.87U（占窗口亏损 54%），
且 09-13 之后 <30m 的 10 笔**全部**由交易所侧 `pa-sl` 平掉——即「最小持有时间」这类
出场侧改动对它们**一笔都改不了**（这个反证已写进 Task 2 的状态说明）。
**入场侧减法做完后，下一个增量一定在出场侧。** 本次已补两件基建：
`tools/exit_cause_report.py`（`2f73f58`）与 `pa_agent/trading/order_ledger.py`（`67af2aa`）。

### 建议的执行顺序（修正版）

1. **Task 3（自助法区间）**——先有判据，再谈配方。
2. 用区间复核 E5 的两个**负贡献**项：`block_neutral_diag_entry`（去 neutral 后 −9.24U）、
   `trade_conf>=55`（−22.49U）。**关掉一个已开的闸门比新开一个闸门风险低，优先做。**
3. **Task 2 的实际动作**不再是开白名单，而是等「关掉某闸门」的对照样本攒出来。
4. **Task 5**：写 `expectancy_report.py`，但日检要加一条 `records/orders/` 账本非空检查
   ——账本一旦空掉，出场归因会在 7 天后失效（这是 `67af2aa` 引入的新依赖）。
5. **新 Task 6（计划外，建议采纳）**：出场侧归因 + 止损位/持有时间实验。
6. ~~Task 4~~ 降级为「时间止损参数寻优」，优先级最低。

---

## 完成定义（Definition of Done）

1. **R1**：`--days 45` 忠实回放，目标配方 n≥20、均U>0、90% CI 下界>0。
2. **R3**：实盘 testnet n≥100、均U>0、90% CI 下界>0、最大回撤<30%。
3. `docs/正期望验证规程.md` 与 `docs/正期望改造计划-基线.md` 数字与最新一次运行一致。
   （**2026-09-18 实况**：这两份文档都还不存在；Task 0 完成后基线数字记在本文件的
   E1 与「执行状态」小节里，没有另开文件。要么补文件、要么把这条 DoD 改成「本文件内的
   基线小节与最新一次运行一致」。）
4. `config/settings.example.json` 两个 env 块与实盘键集一致（值可不同）。
5. `pytest` 与 `ruff` 不劣于改动前基线。

## 风险与已知陷阱

- **数据窥探**：Task 2 的配方是在同一 45 笔样本上搜出来的（32 组合搜索的 argmax 曾给出
  +50.38U）。**必须**留出最后 7 天做样本外复核；回放通过不等于实盘通过。
- **市况依赖**：样本期 10 个标的里 9 个处于日线空头，做多配方在牛市可能反向失效。
- **网络错误**：2026-09-18 复核为 **19%**（`records/pending` 5307 条里 1012 条命中
  `network_error`），不是 2026-09-17 记录的 35%——原始口径把「消息里含该串」与
  「周期失败」混在一起了。它会稀释样本，但**不影响期望本身**；换供应商前所有实盘统计
  都要标注此偏差。复核命令：
  `python -c "import glob,json;fs=glob.glob('records/pending/*.json');print(sum('network_error' in json.dumps(json.load(open(p,encoding='utf-8')),ensure_ascii=False) for p in fs), '/', len(fs))"`
- **订单归因过期（2026-09-18 新增）**：交易所 `allOrders` 只回约 7 天，且被消费掉的
  algo 条件单**没有任何历史端点**，algo 下单响应也不返回 algoId。93 笔已平仓里只有 16 笔
  能解析出平仓单。修复是本地账本 `pa_agent/trading/order_ledger.py`（`67af2aa`）——
  **但它只记录上线之后的单**，老样本永远补不回来。任何 ≥7 天的出场归因结论都必须标注
  「可归属 n=?」，并检查 `records/orders/` 是否非空。
- **testnet 与实盘差异**：testnet 撮合宽松（限价单成交率偏高）。R3 的均U 需按 0.7 折扣估算主网。
- **不要为了追收益加杠杆**：单笔风险 26.7% 下，30 天回撤 ≥50% 的概率是 52–72%（见历史分析）。

## Self-Review 记录（原稿）

> 下面是原计划的自我复核，**保留原样**。2026-09-18 的复核结论见本文件顶部的
> 「执行状态」与文末的「可执行性评估」：原稿的「占位符扫描：每个代码步骤都给了可运行
> 代码」这一条**不成立**——三处测试 loader 与 Task 2 的 fixture 照抄都会崩。



- **Spec 覆盖**：E1→Task 0；E4→Task 1；E3→Task 2；E5→Task 3/5；E2→Task 4。无遗漏。
- **占位符扫描**：无 TBD/TODO；每个代码步骤都给了可运行代码或精确命令。
- **类型一致性**：`maker_fee_rate`/`taker_fee_rate`/`bootstrap_mean_ci`/`block_cycle_allowlist`
  在所有引用处命名一致；`cycle_block` 前缀与 `signal_pipeline.py:171,189` 的
  `split(":", 1)[0]` 拆分逻辑兼容。
