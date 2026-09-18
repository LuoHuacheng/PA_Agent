# 正期望改造 v2 · 可完整执行计划

> **本文档已重构为「可完整执行」的形态。** 上半部分（第 0–3 节）是**唯一执行路径**，
> 每一步都带前提、文件、命令、期望输出和验收门槛；下半部分（附录 A–F）是**归档**：
> 原计划 v1 的任务书与诊断，仅供追溯，**不要照着附录执行**。
>
> 执行者请从 **第 3 节 T1** 顺序往下做；每做完一个 T 就在该节顶部勾选并写下提交号。

## 0. 执行协议（每个 Task 都适用，不要跳）

**环境**

- 一律 `.venv/bin/python`（系统 python3 缺 pydantic）；**所有命令的工作目录都是仓库根**。
- 服务当前**已停止**（无 `config/monitoring.pid`）。凡是要改 `config/settings.json` 的步骤，
  先 `cp config/settings.json config/settings.json.bak_<日期>_<标签>`。
- `config/settings.json` 被 gitignore（**不要提交**）；`config/settings.example.json` 被跟踪，
  **两个 env 块（`binance_usdm_live` + `binance_usdm_testnet`）必须同步改**。

**每个 Task 的固定流程**

1. 先写失败测试 → 2. 跑它确认失败（把报错原文记进提交信息）→ 3. 最小实现 → 4. 跑它确认通过
→ 5. 跑全量回归 + ruff → 6. 提交（**不 push**）。

**回归基线（2026-09-18，改动前必须记下来与它对比）**

| 检查 | 基线 |
|---|---|
| `.venv/bin/python -m pytest --ignore=tests/e2e -q` | **1306 passed, 1 skipped** |
| `ruff check <改动文件>` | 与该文件 HEAD 版本**持平**（不比 HEAD 多） |

**三条禁令（违反则这一轮的结论作废）**

1. **禁止用点估计下结论**。任何"某配方更好"的判断，必须带 90% 自助法区间；区间跨零 = 没有结论。
2. **禁止在同一批样本上既选配方又报告它的收益**。选配方用前 38 天，报告用最后 7 天（T6 的样本外）。
3. **禁止一次改多个闸门**。一次只动一个开关，记下前后对照，否则无法归因。

---

## 1. 目标与完成定义

**主目标（一句话）**：让 PA_Agent 在**忠实回放**与**实盘 testnet** 上的单笔净额均值**统计上显著为正**
（90% 自助法置信区间下界 > 0），同时把成交单量从 0.3 笔/天提到 **≥ 1 笔/天**。

**分档门槛**（全部满足才算通过该档）

| 档 | 样本 | 通过条件 | 不通过怎么办 |
|---|---|---|---|
| **R1a** 单闸门回放 | n ≥ 20 | 均U > 0 **且** 90%CI 下界 > 0 | 换闸门，不上线 |
| **R1b** 组合配方回放 | n ≥ 10 | 同上 | 放弃该组合（本样本量不支持叠条件） |
| **R2** 实盘小样 | n ≥ 30 | 均U > 0 且 最大回撤 < 25% | 立刻回滚配置 |
| **R3** 实盘确认 | n ≥ 100 | 均U > 0 且 90%CI 下界 > 0 且 最大回撤 < 30% | 回滚 |
| **R4** 加仓 | R3 通过 | 单笔风险 4U → 8U，再验 n ≥ 100 | 任一 30% 回撤即减半 |

> **为什么 R1 要拆两档**：实测 45 笔样本里，能到 **n≥20** 的只有 6 个单闸门变体；
> 而组合类配方只有个位数——「做多 + RR≥1.5」**n=7**、「做多 + RR≥1.5 + 仅 channel」**n=2**。
> 它们**结构上无法通过 n≥20**。不拆档就只能得出「什么都不够格」的假结论。
> （唯一通过 R1b 的是「做多 + 通道/区间」n=14，见 T2。）

**完成定义（DoD，每条都可机器验证）**

- [x] `--variants` 输出里，至少一个变体满足 R1a 或 R1b → **「做多 + 通道/区间」n=14 CI [+1.01,+4.90]**（见 `docs/闸门裁定表.md`）。
- [ ] `tools/expectancy_report.py` 在实盘数据上输出 `PASS`（R3）。 → **未达成**：当前 n=73 为上线前历史样本，需新配方实盘累积到 n≥30（R2）后才有 R3 判定。
- [x] `docs/正期望验证规程.md` 存在，且阶段门禁表与本文件第 1 节逐字一致。
- [x] `config/settings.example.json` 两个 env 块的**键集**一致（值可不同）。
- [x] `pytest` 与 `ruff` 不劣于第 0 节基线。
- [x] `records/orders/` 非空（`orders-20260918.jsonl`）。

---

## 2. 现状与基线（动手前先复现一遍，数字对不上就别往下做）

### 2.1 已完成的三个 Task（**不要重做**）

| 原 Task | 提交 | 结果 |
|---|---|---|
| Task 0 回放费用校准 | `db54db3` | 单边费率 `maker+taker == 往返`（1/3 与 2/3，实测四点定标）；兜底支路不再双计。手续费 37.04 → **18.52U** |
| Task 1 提示词补止损下限 | `8552cc5` | 实现为 `prompt_assembler._render_execution_constraints()`（按帧渲染真实数值），**不是**静态规则文本 |
| Task 2 周期白名单门控 | `1ba44bd` | `block_cycle_allowlist` 已就位，默认 `[]`（关）；**实盘也保持关**（理由见附录 D-Task 2） |

计划外补的两件基建（本 v2 会用到）：`2f73f58` `tools/exit_cause_report.py`（出场成因归因）、
`67af2aa` `pa_agent/trading/order_ledger.py`（本地订单账本）。

### 2.2 一条命令复现基线

```bash
cd /Users/hc/Documents/ai/PA_Agent
.venv/bin/python tools/shadow_replay.py --replay --days 45 \
  --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
  --trend-days 7 --trend-neutral 1.5 2>&1 | tail -14
```

**期望输出（2026-09-18 实测，对不上说明样本/环境变了）：**

```text
手续费率(往返, 名义占比): 0.00060
=== 结局分布 === {'rejected_stop_floor': 545, 'unfilled': 160, 'stop': 25, 'no_data': 13, 'time_stop': 10, 'tp2': 10}
成交 45 笔 / 计划 763 笔 = 5.9%
净 1.57U  均 0.03U  胜率 42.2%  手续费 18.52U
```

⚠️ `--trend-days 7 --trend-neutral 1.5` **不能省**：省掉后 CSV 的 `trend_pct` 全空，
逆势闸门判定会静默失效（2026-09-17 踩过）。

### 2.3 当前实盘闸门栈（来自 `config/settings.json`，T2 要逐个裁定）

| 开关 | 实盘值 | 回放里单独关掉它的效果（点估计，样本内 n=45） |
|---|---|---|
| `counter_trend_block` | true | 关掉（禁逆势 1.5% 口径）+38.21U / 33 笔 |
| `block_trending_tr_entry` | true | 关掉（禁 trending_tr）+31.15U / 25 笔 |
| `block_neutral_diag_entry` | true | 关掉 −9.24U / 37 笔（**点估计**；加区间后跨零 ⇒ 证据不足，见 T2） |
| `block_session_entry` | true | 回放未单列，需在 T2 补 |
| `block_short_entry` | false | 关掉做空 +30.58U / 23 笔 |
| `min_plan_reward_risk` | 1.5 | 调到 1.5 保留 36% 成交，净 +16.69U |
| `time_stop_minutes` | 720 | 见 T5 |
| `tp_partial_close_pct` | 50 | 见 T4 |
| `min_stop_mode` / `min_stop_distance_pct` / `min_stop_atr_multiple` | atr / 0.2 / 1.5 | 下限已生效（09-13 后无 <0.2% 止损） |
| `breakeven_stop_trigger` | off | 见 T4 |
| `risk_per_trade_usdt` | 4.0 | R4 才动 |

**`trade_conf` 门槛是反例**：`trade_conf>=55` 过滤后 39 笔 **−22.49U**。
**不要再往"提高置信度门槛"方向调参**，两轮独立数据都证伪了它。

---

## 3. 待执行任务（按 T1 → T6 顺序做，每个都带验收）

### T1 · 回放变体表加自助法置信区间  ✅ 已完成（`cff6542`）

**前提**：无，**可立即执行**。它是其余所有任务的前置——没有区间就不许下结论。
**为什么**：现在 15 行变体表全是点估计，`+38.21U` 与 `n=2` 的 `+12.26U` 并列展示，
而本计划自己定的规矩是「区间不含 0 才进下一步」——区间还不存在。

**Files**
- Modify: `tools/shadow_replay.py`（新增函数；`report_variants` 在 **:480**；`summarize` 在 **:639**）
- Create: `tests/unit/test_shadow_replay_stats.py`
- Create: `docs/正期望改造计划-基线.md`

**Step 1 · 写失败测试** `tests/unit/test_shadow_replay_stats.py`

```python
"""自助法置信区间: 没有区间就不许下结论."""
from __future__ import annotations

from tools.shadow_replay import bootstrap_mean_ci


def test_all_positive_sample_has_positive_interval():
    lo, hi = bootstrap_mean_ci([5.0] * 30)
    assert lo > 0 and hi > 0


def test_mixed_sample_straddles_zero():
    lo, hi = bootstrap_mean_ci([10.0, -9.0] * 15)
    assert lo < 0 < hi


def test_seed_makes_it_deterministic():
    assert bootstrap_mean_ci([1.0, -2.0, 3.0]) == bootstrap_mean_ci([1.0, -2.0, 3.0])


def test_empty_input_is_zero_interval():
    assert bootstrap_mean_ci([]) == (0.0, 0.0)
```

> **不要用** `importlib.util.spec_from_file_location` 那套写法（原稿 Task 3 的写法）。
> 不注册 `sys.modules` 时模块内的 `@dataclass` 取不到命名空间，实测直接崩：
> `AttributeError: 'NoneType' object has no attribute '__dict__'`。
> 用仓库既有惯例 `from tools.shadow_replay import ...` 即可（`test_vergex_gens.py` 一直这么写）。

**Step 2 · 确认失败**：
`.venv/bin/python -m pytest tests/unit/test_shadow_replay_stats.py -q`
→ 期望 `ImportError: cannot import name 'bootstrap_mean_ci'`

**Step 3 · 加函数**（放在 `tools/shadow_replay.py` 的 `fee_rate_from_outcomes` 附近）

```python
def bootstrap_mean_ci(nets, *, iters=20000, alpha=0.10, seed=7):
    """净额均值的自助法置信区间(nets = 每笔净额 USDT); 空输入返回 (0.0, 0.0)."""
    values = [float(x) for x in nets]
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    return (means[int(iters * (alpha / 2))], means[int(iters * (1 - alpha / 2))])
```

> 实测 **0.13 秒/变体**，15 行变体约 2 秒，**不需要**调小 `iters`。

**Step 4 · 接进 `report_variants`（:480）** 表头与数据行各加两列：

```python
    print("%-26s %5s %6s %10s %8s %7s %9s %10s %9s %9s" %
          ("变体", "笔数", "保留", "净U", "均U", "胜率", "净/风险", "相比基线", "90%下", "90%上"))
```

```python
        lo, hi = bootstrap_mean_ci([r["net"] for r in sub])
        print("%-26s %5d %5.0f%% %10.2f %8.2f %6.0f%% %9.3f %+10.2f %9.2f %9.2f" % (
            name, len(sub), len(sub) / len(base) * 100, net, net / len(sub),
            sum(1 for r in sub if r["net"] > 0) / len(sub) * 100,
            net / risk if risk else 0, net - base_net, lo, hi))
```

**Step 5 · 接进 `summarize`（:639）**：同样加两列，区间用 `[r["net"] for r in filled]`。

**Step 6 · 验证**

```bash
.venv/bin/python -m pytest tests/unit/test_shadow_replay_stats.py -q
.venv/bin/python -m pytest --ignore=tests/e2e -q          # 期望 1310 passed, 1 skipped
.venv/bin/python -m ruff check tools/shadow_replay.py     # 期望 37 条 == HEAD, 不多
```

**Step 7 · 落基线**：把 `--variants` 全表（含区间）抄进 `docs/正期望改造计划-基线.md`，
文件头写明「生成命令 + 日期 + 样本量」。

**Step 8 · 提交**：`✨ feat(tools): 回放变体表加自助法置信区间，无区间不下结论`

**验收**
- [ ] 4 个测试通过；`--variants` 输出多出 `90%下` / `90%上` 两列
- [ ] `docs/正期望改造计划-基线.md` 存在且含区间列
- [ ] ruff == 37，pytest 不比基线差

---

### T2 · 用区间逐条裁定现有闸门  ✅ 已完成（`2c1019a`）

**前提**：T1 完成（有 `bootstrap_mean_ci`）。
**产出**：`docs/闸门裁定表.md`（新建）。**本任务只出结论，不动 `config/settings.json`**——
改实盘是 T6 的事。

**Step 1 · 一次算出所有候选闸门的区间**

```bash
cd /Users/hc/Documents/ai/PA_Agent
.venv/bin/python - <<'PY'
import csv
from tools.shadow_replay import _is_counter_trend, bootstrap_mean_ci

def num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

rows = []
for r in csv.DictReader(open("logs/shadow_rows.csv", encoding="utf-8-sig")):
    if r["filled"] != "True": continue
    for k in ("net", "rr", "trade_conf", "trend_pct"):
        r[k] = num(r[k])          # CSV 全是字符串; _is_counter_trend 要 float, 不转就 TypeError
    rows.append(r)

def hour(r): return int(r["record_time"][11:13])
CH = ("broad_channel", "normal_channel", "trading_range")
cases = [
    ("基线 全部", rows),
    ("禁 session(04-12时)", [r for r in rows if not 4 <= hour(r) <= 11]),
    ("仅 session(04-12时)", [r for r in rows if 4 <= hour(r) <= 11]),
    ("禁 neutral 诊断", [r for r in rows if r["diag_direction"] != "neutral"]),
    ("仅 neutral 诊断", [r for r in rows if r["diag_direction"] == "neutral"]),
    ("禁 trending_tr (R1a)", [r for r in rows if r["cycle_position"] != "trending_tr"]),
    ("仅 trending_tr", [r for r in rows if r["cycle_position"] == "trending_tr"]),
    ("禁逆势 1.5% (R1a)", [r for r in rows if not _is_counter_trend(r, 1.5)]),
    ("trade_conf>=55", [r for r in rows if (r["trade_conf"] or 0) >= 55]),
    ("仅做多 (R1a)", [r for r in rows if r["direction"] == "做多"]),
    ("仅通道/区间 (R1b)", [r for r in rows if r["cycle_position"] in CH]),
    ("做多+通道/区间 (R1b)", [r for r in rows if r["direction"] == "做多" and r["cycle_position"] in CH]),
    ("做多+RR>=1.5 (R1b)", [r for r in rows if r["direction"] == "做多" and (r["rr"] or 0) >= 1.5]),
]
print("%-24s %4s %9s %7s %21s %s" % ("变体", "n", "净U", "均U", "90%CI", "R1"))
for name, sub in cases:
    nets = [r["net"] for r in sub]
    lo, hi = bootstrap_mean_ci(nets)
    need = 20 if "(R1a)" in name else (10 if "(R1b)" in name else None)
    v = "" if need is None else ("PASS" if (len(nets) >= need and lo > 0) else "fail")
    print("%-24s %4d %+9.2f %+7.2f [%+8.2f,%+8.2f] %s" % (
        name, len(nets), sum(nets), sum(nets)/len(nets), lo, hi, v))
PY
```

**Step 2 · 期望输出（2026-09-18 用同一算法先行算出，T1 完成后应能复现）**

```text
变体                          n        净U      均U                 90%CI R1
基线 全部                      45     +1.57   +0.03 [   -1.14,   +1.24]
禁 session(04-12时)          35    +30.55   +0.87 [   -0.50,   +2.30]
仅 session(04-12时)          10    -28.98   -2.90 [   -4.29,   -1.13]   <- 区间全负
禁 neutral 诊断               37     -9.24   -0.25 [   -1.53,   +1.09]
仅 neutral 诊断                8    +10.81   +1.35 [   -1.50,   +4.33]
禁 trending_tr (R1a)        25    +31.15   +1.25 [   -0.46,   +3.00] fail
仅 trending_tr              20    -29.58   -1.48 [   -2.85,   -0.01]   <- 区间全负
禁逆势 1.5% (R1a)             33    +38.21   +1.16 [   -0.25,   +2.59] fail
trade_conf>=55             39    -22.49   -0.58 [   -1.77,   +0.68]
仅做多 (R1a)                  23    +30.58   +1.33 [   -0.39,   +3.03] fail
仅通道/区间 (R1b)               22    +39.19   +1.78 [   -0.11,   +3.66] fail
做多+通道/区间 (R1b)             14    +41.60   +2.97 [   +1.01,   +4.90] PASS
做多+RR>=1.5 (R1b)            7    +24.03   +3.43 [   +0.20,   +6.47] fail
```

**Step 3 · 裁定规则（照抄执行，不要自己发挥）**

| 观察到 | 裁定 |
|---|---|
| 区间**上界 < 0**（全负）且实盘**已开**该闸门 | ✅ 保留（闸门方向正确，是"关掉它"变差） |
| 区间**上界 < 0** 且实盘**未开** | ⚠️ 可开，但需 T6 的样本外复核后才动 |
| 区间**下界 > 0** | ✅ 该配方有统计支持 → 进 T6 |
| 区间**跨 0** | ❌ 证据不足 → **不动**（哪怕点估计很漂亮） |

**Step 4 · 按上面规则得到的裁定（2026-09-18，样本内 n=45）**

| 闸门 | 实盘 | 裁定 | 依据 |
|---|---|---|---|
| `block_session_entry` | true | **保留** | 「仅 session 04-12时」n=10 CI **[−4.29,−1.13] 全负** ⇒ 该时段显著亏钱 |
| `block_trending_tr_entry` | true | **保留** | 「仅 trending_tr」n=20 CI [−2.85,−0.01] 全负 ⇒ 该周期显著亏钱 |
| `block_neutral_diag_entry` | true | **不动** | 「禁 neutral」CI [−1.53,+1.09] **跨零** ⇒ 点估计 −9.24U 不足以定案 |
| `counter_trend_block` | true | **暂时保留** | 「禁逆势」+38.21U 但 CI [−0.25,+2.59] 跨零（n=33 仍不够） |
| `block_short_entry` | false | **不动** | 「仅做多」+30.58U 但 CI [−0.39,+3.03] 跨零 |
| `min_plan_reward_risk`=1.5 | 1.5 | **不动** | RR≥1.0 与基线同值；RR≥1.5 的增益未单独做区间 |
| `block_cycle_allowlist` | `[]` | **不动** | 单独白名单 CI [−0.11,+3.66] 跨零 |
| **组合：仅做多 + 通道/区间** | — | ⭐ **唯一 PASS** | n=14 CI **[+1.01,+4.90]** 全正 ⇒ 进 T6 |
| `trade_conf` 门槛 | 无 | **不要加** | ≥55 的 CI 跨零且点估计为负 |

> **⚠️ 这一节推翻了两轮点估计的结论，必须读进去**：
> 「禁逆势 +38.21U」「禁 trending_tr +31.15U」「仅做多 +30.58U」「仅通道/区间 +39.19U」
> ——**四个全部跨零**。它们不是"有效的闸门"，只是样本噪声。**唯一**站得住的是
> 「做多 + 通道/区间」这一个组合（n=14）。这也正是第 0 节禁令 1 存在的原因。
>
> 同理，上一轮文档里「禁 neutral 在伤害实盘」的说法**已被区间否掉**：那只是点估计。

**Step 5 · 写 `docs/闸门裁定表.md`**：把 Step 3 的规则、Step 4 的裁定表、
以及产生它的命令与日期抄进去。**每个裁定都必须带 n 和区间**，没有区间的写「证据不足，不动」。

**Step 6 · 提交**：`📝 docs(plans): 用置信区间裁定闸门，仅一个配方通过 R1`

**验收**
- [ ] Step 1 命令的输出与 Step 2 的期望表一致（不一致说明样本变了，停下重估）
- [ ] `docs/闸门裁定表.md` 存在，每行都带 n 与 90%CI
- [ ] `config/settings.json` **未被修改**（用 `git status` 无法验证，因为它是 gitignore；用文件 mtime 或 diff 备份确认）

---

### T3 · 实盘期望判定脚本 + 验证规程  ✅ 已完成（`d9ce5b9`）

**前提**：T1 完成（要复用 `bootstrap_mean_ci`）。

**Files**
- Create: `tools/expectancy_report.py`
- Create: `docs/正期望验证规程.md`

**Step 1 · 写脚本**（注意：用 `sys.path` + 正常 import，**不要**用原稿的
`importlib.util.spec_from_file_location`，那套会崩）

```python
"""按硬门槛判定实盘期望是否显著为正."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.shadow_replay import bootstrap_mean_ci  # noqa: E402 - path set above

MIN_N = {"R2": 30, "R3": 100}
MAX_DD = {"R2": 0.25, "R3": 0.30}


def max_drawdown(nets):
    """按时间顺序累计净额的峰值回撤(相对峰值)."""
    peak = equity = worst = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def main(path="trade_records/outcomes.csv"):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    pairs = sorted((str(r.get("ts_close") or ""), num(r.get("net_usdt"))) for r in rows)
    nets = [n for _, n in pairs if n is not None]
    if not nets:
        print("FAIL: 无成交记录")
        return 1
    mean = sum(nets) / len(nets)
    lo, hi = bootstrap_mean_ci(nets)
    wr = sum(1 for n in nets if n > 0) / len(nets)
    dd = max_drawdown(nets)
    stage = "R3" if len(nets) >= MIN_N["R3"] else ("R2" if len(nets) >= MIN_N["R2"] else "样本不足")
    if stage == "样本不足":
        print(f"n={len(nets)} 均{mean:+.2f}U 胜率{wr:.0%} 90%CI[{lo:+.2f}, {hi:+.2f}] "
              f"最大回撤{dd:.1%} -> FAIL(样本不足, 需 n>={MIN_N['R2']})")
        return 1
    ok = mean > 0 and lo > 0 and dd < MAX_DD[stage]
    print(f"n={len(nets)} 均{mean:+.2f}U 胜率{wr:.0%} 90%CI[{lo:+.2f}, {hi:+.2f}] "
          f"最大回撤{dd:.1%} 阶段={stage} -> {'PASS' if ok else 'FAIL'}")
    print(f"门槛({stage}): n>={MIN_N[stage]}, 均>0, CI下界>0, 回撤<{MAX_DD[stage]:.0%}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
```

**Step 2 · 跑它并确认 FAIL**

```bash
.venv/bin/python tools/expectancy_report.py
```

**期望（2026-09-18 实盘 73 笔，均 −2.19U、胜率 37%）**：输出以 `-> FAIL` 结尾。
**FAIL 是正确行为**——当前实盘就是负期望，脚本必须如实说。

**Step 3 · 写 `docs/正期望验证规程.md`**，至少包含：

- 第 1 节的 R1a/R1b/R2/R3/R4 门槛表（**与本文件逐字一致**）
- 每日一行检查：`.venv/bin/python tools/expectancy_report.py`
- 每日第二行检查（账本）：`ls records/orders/ | wc -l` 必须 `> 0`
- 回滚清单：恢复 `config/settings.json.bak_*` → 停服务 → 记原因
- 停服/起服命令（见 T6 Step 4 的两种写法）

**Step 4 · 提交**：`✨ feat(tools): 新增实盘期望判定脚本与阶段验证规程`

**验收**
- [ ] `tools/expectancy_report.py` 在 73 笔上输出 FAIL 且带 CI 与回撤
- [ ] `docs/正期望验证规程.md` 的门槛表与第 1 节一致
- [ ] `pytest` 与 `ruff` 不劣于基线（该脚本不新增 lint 项）

---

### T4 · 出场侧实验（本计划原先完全没有的方向）  ✅ 已完成（`569018b`）

**前提**：`tools/exit_cause_report.py` 已存在（`2f73f58`）；`records/orders/` 有数据更好，没有也能跑。
**为什么**：五个原 Task 全在入场，但 2026-09-18 的出场归因显示亏损主要在出场侧：
`<30m` 桶 36 笔 **−89.87U**（占窗口亏损 54%），且 09-13 之后该桶 10 笔**全部**由交易所侧
`pa-sl` 平掉。**结论：能改的不是"晚点平仓"，而是止损位与获利结构。**

**Step 1 · 复现出场基线**

```bash
.venv/bin/python tools/exit_cause_report.py --days 14 --out logs/exit_cause.json 2>&1 | head -30
```

**期望（2026-09-18）**：`<30m n=36 净 −89.87`、`3-6h n=9 净 −65.50`（其中 ZEC 单笔 −61.60）、
`均MAE −0.797 / 均MFE +0.855`（几乎对称 ⇒ 入场无方向优势）。

**Step 2 · 三次单参数实验（一次只动一个，禁令 3）**

```bash
# a) 止损位: 1.5 → 2.5 ATR
.venv/bin/python tools/shadow_replay.py --replay --days 45 --min-stop-atr 2.5 --min-stop-pct 0.2 \
  --timeout-min 30 --trend-days 7 --trend-neutral 1.5 2>&1 | grep -E "净 |结局分布"

# b) 部分止盈: 50% → 0(全平) / 70%
.venv/bin/python tools/shadow_replay.py --replay --days 45 --min-stop-atr 1.5 --min-stop-pct 0.2 \
  --timeout-min 30 --partial-pct 0 --trend-days 7 --trend-neutral 1.5 2>&1 | grep -E "净 "

# c) 保本移损: off → 0.5r（走对照组入口 --baseline）
.venv/bin/python tools/shadow_replay.py --baseline --days 45 --baseline-be 0.5r \
  --trend-days 7 --trend-neutral 1.5 2>&1 | tail -5
```

**Step 3 · 判定**：每次跑完把 `logs/shadow_rows.csv` 存档（`cp logs/shadow_rows.csv logs/shadow_rows_<标签>.csv`），
再用 T2 Step 1 的脚本算区间。**只有区间下界 > 0 的参数才进 T6。**

**Step 4 · 写 `docs/出场侧实验.md`**：三个参数各自的前后 n/净/区间 + 结论。

**Step 5 · 提交**：`📝 docs(plans): 出场侧单参数实验(止损位/部分止盈/保本)`

**验收**
- [ ] 三份 `logs/shadow_rows_*.csv` 存档存在
- [ ] `docs/出场侧实验.md` 每个参数都带 n/净/区间
- [ ] 没有修改任何 `pa_agent/` 代码（本任务只做实验）

---

### T5 · 时间止损参数寻优（原 Task 4 的重写）  ✅ 已完成（`50464cd`）

**前提**：无。
**为什么重写**：原 Task 4 的前提（"实盘没有时间止损，要决定是否移植"）**已失效**——
实盘 `time_stop_minutes=720`，由 `pa_agent/trading/position_manager.py` 的 Time-stop 轮询执行，
运行日志里 `时间止损` 出现 9 次、`Time-stop` 31 次。所以现在要问的是**720 是不是最优**。

**Step 1 · 四个口径对比**

```bash
cd /Users/hc/Documents/ai/PA_Agent
for TS in 720 1440 4320 100000; do
  echo "=== time-stop-min=$TS ==="
  .venv/bin/python tools/shadow_replay.py --replay --days 45 --time-stop-min $TS \
    --min-stop-atr 1.5 --min-stop-pct 0.2 --timeout-min 30 \
    --trend-days 7 --trend-neutral 1.5 2>&1 | grep -E "净 |结局分布"
done
```

**Step 2 · 判定**：把每个口径的 `net` 列喂给 T1 的 `bootstrap_mean_ci`，
**只有区间下界 > 0 且优于 720 的口径才进 T6**。

**Step 3 · 交叉验证（重要）**：时间止损只影响"慢慢磨"的仓位——`exit_cause_report` 已证明
`<30m` 桶 09-13 后 10 笔**全是交易所侧 `pa-sl` 平掉的**，时间止损对它们**一笔都改不了**。
所以若某个 `TS` 值让净额大幅变化，必须解释是哪一档时长被改变；
时长桶基线（14 天，92 笔）：`<30m −89.87` / `1-3h +8.54` / `3-6h −65.50`（含 ZEC −61.60）/
`12-24h −30.25` / `>24h +3.89`。

**Step 4 · 提交**：`📝 docs(plans): 时间止损参数寻优结果`

**验收**
- [ ] 四个口径的结果与区间都记进文档
- [ ] 给出"720 保留 / 改 X"的明确结论，或"证据不足，保留 720"

---

### T6 · 灰度上线 + 实盘小样验证（**唯一**允许改 settings.json 的任务）  🟡 已灰度上线（`c00789d`），待 R2 实盘样本

**前提**：T1、T2、T3 完成。

> ### ⚠️ 先说清一个硬约束：回放样本外**做不了**
>
> 实测把 45 笔按时间切：
>
> | 留出 | 样本内 | 样本外 | 唯一 PASS 配方在样本外的 n |
> |---|---|---|---|
> | 最后 7 天（切点 09-11） | 39 笔 | 6 笔 | **2 笔** |
> | 最后 10 天（切点 09-08） | 33 笔 | 12 笔 | 3 笔 |
> | 最后 14 天（切点 09-04） | 10 笔 | 35 笔 | 11 笔，但**样本内只剩 3 笔**（无法选配方） |
>
> 配方（做多+通道/区间）的 14 笔里 11 笔集中在最近 14 天，所以任何"留出"都会把
> 选择集或验证集压到 2–3 笔。**结论：不能靠回放留出验证，只能靠实盘小样（R2）。**
> 这也意味着 T6 是本计划唯一有真实风险的一步——**必须按下面的回滚清单执行**。

**Step 1 · 备份**

```bash
cp config/settings.json config/settings.json.bak_$(date +%Y%m%d)_pre_gate
```

**Step 2 · 只改一个配方**（T2 裁定的唯一 PASS 项；两个键同属一个配方，属于一次决策）

```bash
.venv/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("config/settings.json")
doc = json.loads(p.read_text(encoding="utf-8"))
cfg = doc["binance_usdm_testnet"]
cfg["block_short_entry"] = True
cfg["block_cycle_allowlist"] = ["broad_channel", "normal_channel", "trading_range"]
p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("ok:", {k: cfg[k] for k in ("block_short_entry", "block_cycle_allowlist")})
PY
```

同时把这两个键同步到 `config/settings.example.json` 的**两个 env 块**（值给默认：
`block_short_entry: false`、`block_cycle_allowlist: []`）。

> 若 T4/T5 也有通过项，**在 R2 通过之后**再叠加，不要和本配方同时上（禁令 3）。

**Step 3 · 记录本次改动**（写进 `docs/闸门裁定表.md`）
：改了哪两个键、依据是哪一行区间、预期什么、失败判据是什么。

**Step 4 · 起服务**（两种写法，按环境选）

```bash
# 常规环境
pa-monitor start

# 沙箱/受限环境(pa-monitor stop 内部用 ps, 会被拒绝)——直接管 pid 文件
nohup .venv/bin/python -m pa_agent.monitoring.cli start > logs/monitor.out 2>&1 &
# 停: 不读 pid 文件也能停, 用 python 发信号
.venv/bin/python -c "import pathlib, os, signal; os.kill(int(pathlib.Path('config/monitoring.pid').read_text().strip()), signal.SIGTERM)"
```

**Step 5 · 日检**（每天一次，两条都要看）

```bash
.venv/bin/python tools/expectancy_report.py     # 期望先 FAIL(样本不足), n 攒到 30 后再判定
ls records/orders/ | wc -l                      # 必须 > 0, 否则出场归因 7 天后失效
```

**Step 6 · 判定与回滚**

| 观察 | 动作 |
|---|---|
| n≥30 且 `expectancy_report` PASS | 进 R3（继续攒到 n≥100） |
| n≥30 但 CI 下界 ≤ 0 | **回滚**：恢复备份 → 停服务 → 记原因 |
| 最大回撤 ≥ 25% | **立即回滚**，不等 n |
| `records/orders/` 为空 | 停下查账本（`order_ledger` 写失败会只告警不报错） |

回滚命令：

```bash
cp config/settings.json.bak_<日期>_pre_gate config/settings.json
.venv/bin/python -c "import pathlib, os, signal; os.kill(int(pathlib.Path('config/monitoring.pid').read_text().strip()), signal.SIGTERM)"
```

**Step 7 · 提交**（**不含** settings.json，它被 gitignore）

```bash
git add docs/闸门裁定表.md docs/正期望验证规程.md config/settings.example.json
git status --short      # 确认 settings.json 不在列表里
git commit -m "✨ feat(config): 灰度启用做多+通道/区间配方，附回滚清单"
```

**验收**
- [ ] 备份文件存在，且回滚命令实测可执行（在备份上演练一次）
- [ ] `config/settings.example.json` 两个 env 块键集一致
- [ ] 裁定表里写明了本次改动的依据行（n 与区间）
- [ ] 日检两条命令都有输出且被记录

---

## 4. 附录（归档，勿按此执行）

以下为**原计划 v1 的内容与 2026-09-18 的复核批注**，仅用于追溯证据来源。
执行路径只有上面第 0–3 节。



> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 PA_Agent 在忠实回放与实盘 testnet 上达到**统计上显著的正期望**（净额均值 > 0，且自助法 90% 置信区间不含 0），同时把单量从 0.3 笔/天提升到 1–3 笔/天。

**Architecture:** 先修**度量失真**（回放费用模型 2 倍于实盘、趋势标签缺失、无显著性输出），再**收敛入场配方**（方向 + 周期位置 + 盈亏比三层门控），再用**提示词修复**提高计划质量与单量，最后用**留出集 + 实盘小样**验证。任何一步不达标就回滚，不带病上线。

**Tech Stack:** Python 3.11 · pydantic v2 · pytest · ruff · 离线引擎 `tools/shadow_replay.py` · Binance USD$\cdot$M Futures Testnet

**Spec:** 本文件自带诊断证据（见下）。数据源：`records/pending/*.json`(5307 条) / `logs/shadow_rows.csv` / `trade_records/outcomes.csv`(73 笔真实成交)。

---

### A. 执行状态（2026-09-18 复核）

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

### B. Global Constraints（原稿）

- Python 3.11，虚拟环境 `.venv`；所有命令用 `.venv/bin/python`（系统 python3 缺 pydantic）。
- 提交格式固定：`<emoji> <type>(scope): 中文描述`，例：`🐛 fix(tools): 修正回放手续费双重计入`。**不主动 push。**
- `config/settings.json` 是实盘配置且被 gitignore；`config/settings.example.json` 是模板且被跟踪，**两个 env 块（`binance_usdm_live` + `binance_usdm_testnet`）必须同步**。
- **所有回放命令必须带 `--trend-days 7 --trend-neutral 1.5`**，否则 CSV 的 `trend_pct` 全空、逆势闸门判定静默失效（2026-09-17 已踩过）。
- 每个新门控都要有 settings 开关，默认值与改动前一致（灰度开关，可回滚）。
- 合并前 `ruff check` 与 `pytest` 通过；不新增 lint 错误。

---

### C. 原诊断 E1–E5（已按 2026-09-18 复核批注修正）

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

### D. 原计划的文件清单

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

#### 【归档】Task 0: 回放费用模型校准 ✅ 已完成（`db54db3`）

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

#### 【归档】Task 1: 提示词补上止损下限规则 ✅ 已完成（`8552cc5`）

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

#### 【归档】Task 2: 周期白名单门控 ✅ 已完成（`1ba44bd`）

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

#### 【归档】Task 3: 自助法置信区间 → 已重写为正文 T1

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

#### 【归档】Task 4: 时间止损移植 → 已重写为正文 T5

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

#### 【归档】Task 5: 实盘验证协议 → 已重写为正文 T3/T6

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

### E. 可执行性评估（2026-09-18 逐条实测）

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

### F. 原完成定义（已被第 1 节取代）

1. **R1**：`--days 45` 忠实回放，目标配方 n≥20、均U>0、90% CI 下界>0。
2. **R3**：实盘 testnet n≥100、均U>0、90% CI 下界>0、最大回撤<30%。
3. `docs/正期望验证规程.md` 与 `docs/正期望改造计划-基线.md` 数字与最新一次运行一致。
   （**2026-09-18 实况**：这两份文档都还不存在；Task 0 完成后基线数字记在本文件的
   E1 与「执行状态」小节里，没有另开文件。要么补文件、要么把这条 DoD 改成「本文件内的
   基线小节与最新一次运行一致」。）
4. `config/settings.example.json` 两个 env 块与实盘键集一致（值可不同）。
5. `pytest` 与 `ruff` 不劣于改动前基线。

### G. 风险与已知陷阱（仍然有效）

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

### H. Self-Review 记录（原稿）

> 下面是原计划的自我复核，**保留原样**。2026-09-18 的复核结论见本文件顶部的
> 「执行状态」与文末的「可执行性评估」：原稿的「占位符扫描：每个代码步骤都给了可运行
> 代码」这一条**不成立**——三处测试 loader 与 Task 2 的 fixture 照抄都会崩。



- **Spec 覆盖**：E1→Task 0；E4→Task 1；E3→Task 2；E5→Task 3/5；E2→Task 4。无遗漏。
- **占位符扫描**：无 TBD/TODO；每个代码步骤都给了可运行代码或精确命令。
- **类型一致性**：`maker_fee_rate`/`taker_fee_rate`/`bootstrap_mean_ci`/`block_cycle_allowlist`
  在所有引用处命名一致；`cycle_block` 前缀与 `signal_pipeline.py:171,189` 的
  `split(":", 1)[0]` 拆分逻辑兼容。
