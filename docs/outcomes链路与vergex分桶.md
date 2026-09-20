# outcomes 链路修复与 VergeX 注入分桶（2026-09-20）

## 1. 发现：`trade_records/outcomes.csv` 停更 10 天

排查「VergeX 29/30 注入的 48h shadow 验证为什么没有样本」时发现：

| 事实 | 值 |
|---|---|
| `outcomes.csv` 覆盖区间 | 2026-09-04 12:55 ~ **2026-09-10 00:00** |
| 实弹期（09-18 20:40 起）样本 | **0 条** |
| 分桶结果（修复前） | 在场桶 **n=0** / 不在场桶 n=73 |

**根因不是 bug，而是没人跑**：`outcomes.csv` 的唯一写入者是
`tools/outcome_merge.py`，一个**手工 CLI**，没有挂任何定时任务。
09-13 已经把 `strategy_files_used` 的口径改成含 BASE 常驻文件
（`pa_agent/orchestrator/two_stage.py:620`，正是为了这条链），但链子本身一直没人执行。

## 2. 修复：手工补跑

```bash
cd /Users/hc/Documents/ai/PA_Agent
.venv/bin/python tools/outcome_merge.py --days 30
```

```text
币种: BTCUSDT, ETHUSDT, SOLUSDT, LINKUSDT, ZECUSDT, ADAUSDT, BNBUSDT, DOGEUSDT, TRXUSDT, XRPUSDT
决策记录: pending=756 csv=696
归属: matched=23 manual=1255 missing=1 ambiguous=0
新 OutcomeRows: 23 (uid 幂等去重落盘)
audit: logs/outcome_audit_20260920.json
```

`outcomes.csv` 73 → **96 行**，最新样本 **2026-09-20 01:01**。

## 3. VergeX 29/30 注入分桶（首次拿到数）

按 `strategy_files` 分桶（在场 = 同时含 `文件29-利弗莫尔趋势跟随移植.txt` 与 `文件30-三层评分趋势移植.txt`）：

| 桶 | n | 胜 | 均 R |
|---|---|---|---|
| **在场**（注入生效后） | **18** | 5 | **+0.223** |
| 不在场（注入前老样本） | 78 | 27 | **−0.310** |

接线仍在：`pa_agent/ai/prompt_assembler.py:799-806` 的 `STAGE2_BASE_PROMPT_TXT_FILES`
仍含两份移植文件。

## 4. ⚠️ 这个差异**不能归因给 29/30**

- 「不在场桶」是 09-10 之前的样本，「在场桶」是 09-13 之后 —— **时间上不重叠**；
- 同一窗口内还改过：闸门栈（09-13 引入 / 09-18 上线）、止损下限 1.5→1.2（09-20）、
  趋势窗口一致性（09-20）、`trending_tr` 放行与回滚（09-20）——**任何一项都能造成同样的差异**；
- 这是**前后对比**，不是 A/B。按 `docs/vergex移植策略shadow验证清单.md` 的判读纪律，
  样本量门槛（`feedback.min_samples`=10）虽然满足了（in=18），但满足的是「样本量」，
  不是「因果」。

**要判 29/30 的净贡献，得用同期对照** —— 仓库里已有工具
`tools/audit/injection_ab_export.py`（提交 `31c43fd`，含 outcomes 回填），
它才是为这个目的建的。

## 5. 遗留

1. **`outcome_merge` 已接进日检（2026-09-20 15:59 修复）** ——
   `scripts/daily_check.py` 与 `scripts/daily_check.sh` 都在**判定之前**跑
   `.venv/bin/python tools/outcome_merge.py --days 7`，输出记为「样本回填」一行。

   **顺序是硬要求**：第 1~4 行的 `expectancy_report.py` 默认入参就是
   `trade_records/outcomes.csv` —— 先判定后回填等于永远看滞后值。
   `--days 7` 配合 6 小时一次的节拍，uid 幂等，重复跑只补新增；失败不阻塞日检。
2. `outcomes.csv` 同时是 `tools/shadow_replay.py` 的**费率校准源**
   （`fee_rate_from_outcomes`）—— 它停更期间，回放用的是旧费率。

## 6. 相关

- launchd 日检任务本次一并恢复，且修掉了两个此前没暴露的问题
  （TCC 目录不能做 `Standard*Path`、解释器不能 realpath）——
  见 `docs/闸门裁定表.md` §7.6。
