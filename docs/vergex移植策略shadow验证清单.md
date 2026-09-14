# VergeX 移植策略 shadow 验证清单

> 依据: `docs/superpowers/plans/2026-09-14-vergex-prompt-files-wiring.md`(Task 3 Step 3)
> 上游: `docs/superpowers/plans/2026-09-14-vergex-strategy-integration.md` Task 7 Step 3(48h shadow 口径)
> 对象: `文件29-利弗莫尔趋势跟随移植.txt`、`文件30-三层评分趋势移植.txt`

## 0. 接线事实(先看这个)

- 两个移植文件走 **常驻基础注入**:`pa_agent/ai/prompt_assembler.py` 的
  `STAGE2_BASE_PROMPT_TXT_FILES`(共 6 项:逐棒分析检查单 / 文件16 / 文件17 / 文件23 /
  文件29 / 文件30),每次 stage2 必定注入,与 pattern 路由结果无关。
- `pa_agent/ai/router.py` 的 `_ALL_VALID_FILES` 已补录两个文件名,stage1 模型点名它们不会被静默丢弃。

- **落盘口径修正**(本次一并完成):`pa_agent/orchestrator/two_stage.py` 原先把
  `strategy_files_used` 记成"仅路由结果",常驻 BASE 文件(含 29/30)不进记录,
  导致 `outcomes.csv` 的 `strategy_files` 分桶根本看不到它们 —— 48h shadow 会得出
  "零样本"的假结论。现改为 "路由结果 + BASE 常驻文件(去重)",与真正注入 prompt 的
  文件集合一致;prompt 内容不变(内部 `dict.fromkeys` 去重、BASE 仍排末尾)。
- 执行侧零改动:`binance_usdm_*` 开关、`monitoring`、日亏熔断 30U、`block_trending_tr`、
  `block_session` 全部原样。

## 1. 重启指引(操作者执行)

改代码后必须重启进程才会生效:

- **GUI**:关闭 `pa-agent` 再重新启动(monitor 随 GUI 自启)。
- **无头**:`pa-monitor stop` → 确认旧进程已停(比对 `config/monitoring.pid` 里的 PID 是否已不存在,
  必要时 `kill <pid>`)→ `pa-monitor start`;用 `pa-monitor status` 复核运行中。

启动后 30 分钟内应能观察到新生效记录(`records/pending/*.json`)。

## 2. 观察口径(48h)

**取样**:48h 后(或至少积累 `feedback.min_samples` = 10 条已平仓样本后)读
`trade_records/outcomes.csv`,按 `strategy_files` 列(JSON 数组字符串)分桶:

- **在场桶**:该行 `strategy_files` 同时含 `文件29-利弗莫尔趋势跟随移植.txt` 与
  `文件30-三层评分趋势移植.txt`。
- **不在场桶**:该行缺任一文件(应只剩本次改动重启**之前**落盘的老样本)。
- 对比两桶的胜负计数、胜率、平均 `win_r`。

**快速取样命令**(仓库根):

```bash
python - <<'PY'
import csv, json, statistics as st
A = {"文件29-利弗莫尔趋势跟随移植.txt", "文件30-三层评分趋势移植.txt"}
with open("trade_records/outcomes.csv", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))
def bucket(rows):
    b = {"in": [], "out": []}
    for r in rows:
        files = set(json.loads(r.get("strategy_files") or "[]"))
        b["in" if A <= files else "out"].append(r)
    return b
for k, rs in bucket(rows).items():
    rs = [r for r in rs if (r.get("win_r") or "").strip()]
    vals = [float(r["win_r"]) for r in rs]
    wins = sum(1 for r in rs if r.get("outcome") == "win")
    print(f"桶={k} n={len(rs)} 胜={wins} 均R={st.mean(vals) if vals else float('nan'):.3f}")
PY
```

**判读纪律**:

- `rule_stats`(`pa_agent/feedback/rule_stats.py`,`build_group_stats`)的 `min_samples`
  未满**不下结论**;**预期两文件常驻后每笔新决策都在场**,"不在场桶"仅剩改动前老样本,
  因此新样本等价于全样本。
- 若"在场桶"里 29/30 缺失 —— 说明重启未生效或写盘口径又回退了,先查 Section 4 的排查项,
  不要直接解读为"策略无增益"。

## 3. 回滚路径

- **代码回滚(最小)**:从 `STAGE2_BASE_PROMPT_TXT_FILES` 删掉
  `文件29-利弗莫尔趋势跟随移植.txt`、`文件30-三层评分趋势移植.txt` 两行 → 重启 → 完全退回到改动前。
  执行侧、settings.json、路由器均无需改动。
- **整包回滚**:3 个提交可整段 revert(见 Section 5 的 hash);
  文件重命名可 `git mv` 回 `文件24/25`(历史保留)。
- 回滚后 `strategy_files` 里会残留历史样本的 29/30 记录,按 `ts_record` 截断窗口再统计即可。

## 4. 决策行为红旗清单(出现即止损排查)


| 红旗 | 含义 | 先查什么 |
| --- | --- | --- |
| 趋势市(`cycle_position` 为 spike/通道类)仍开 barbwire / 铁丝网逆势单 | 文件30 的环境层过滤未生效,或与 `文件21-铁丝网与无交易环境.txt` 打架 | 该笔 `records/pending/*.json` 的 `strategy_files_used` 是否含 29/30;stage2 的 `decision_trace` 引用哪条规则 |
| 三价(entry/stop/target)与 EMA20 关系反向 | 移植文件的"顺 EMA 方向"约束被 Routing/既有 playbook 覆盖 | 该笔同时在场了哪些文件(共 6 个 base + N 个路由文件) |
| 同一根收盘的输出与移植文件明文规则冲突(如文件29 的"跌破结构位立刻认错"被忽略) | 两文件与既有 22 个 playbook 提示冲突,或被长 prompt 稀释 | 对比注入前后的 token 数与 stage2 是否截断 |
| stage2 决策置信度整体异常抬升/下降(±10pt 量级) | 6 个 base 文件改变了模型基线 | 拉 48h 前后的 `trade_confidence` 分布对比 |
| stage2 报错率、超时率上升 | prompt 变长(≈ +124 行 ≈ 3.5k token)导致网关超时 | `logs/pa_agent.log` 的 stage2 耗时与重试计数 |


冲突排查手段:必要时**退回 pattern 路由模式**(备选接线点:router.py 加 pattern 分支 +
`_ALL_VALID_FILES`(已补录)+ `pattern_routing.py` 判定表加行),把两文件从"全程在场"
降级为"命中形态才在场"。

## 5. 本次改动清单与验收证据

| 项 | 内容 |
| --- | --- |
| 提交 1 | `🔧 chore(prompt): 移植策略文件重编号 24/25 为 29/30 消除撞号` |
| 提交 2 | `✨ feat(prompt): Livermore/三层评分移植文件常驻 stage2 基础注入` |
| 提交 3 | `📝 docs(prompt): 路由白名单补录移植文件 + 48h shadow 验证清单` |
| 落盘口径修正 | `pa_agent/orchestrator/two_stage.py` Step 10:BASE 常驻文件并入 `strategy_files_used` |
| 测试 | `tests/unit/test_prompt_txt_files.py`(2 新用例 + 顺序断言改用 `len(...)`)、`tests/integration/test_two_stage_strategy_files_base.py`(1 新用例) |

验收判据对照(计划"验收判据"1–4):

1. Task 2 两个新测试 PASS;全量单测无回归 —— 见提交后 `uv run pytest tests/unit` 结果。
2. 重启后任取 `records/pending/*.json`,其 stage2 调试信息(或 `_prompt_files_panel` 调试 tab)
   的文件列表含 29/30 —— 代码路径已保证(`strategy_files_used` + `stage2_prompt_txt_files`),
   需重启后人工抽检。
3. 48h 后 `outcomes.csv` 可按两文件过滤且有非零样本 —— 由上面的落盘口径修正保证;
   需 48h 人工观察。
4. 执行开关零变化 —— 本次未触碰 `binance_usdm_*` / `monitoring` / 风控闸门。

**人工步骤(不在会话内等待)**:重启、48h shadow 观察、抽检 pending 记录。
