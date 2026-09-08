# PA Agent AI 策略优化方案（按优先级）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 PA Agent 的"AI 策略产出"装上客观依据闭环与可验证的优化路径：先让策略效果可测、再收权给程序、再降本提速、最后补信息面。

**Architecture:** 四个独立可交付阶段（Phase A–D），严格按优先级排序、每阶段可单独上线回滚：
A) 结果回灌闭环：把 testnet/成交结果按"路由文件+周期+方向"归组统计，注入 stage2 prompt 作为客观先验，并出置信度校准报告；
B) 程序收权：周期路由改为"程序候选集 + 模型限选/高门槛推翻"，边界与信号棒质量程序预判；
C) 成本优化：prompt 去重与缓存友好排序、事件闸门跳过冗余阶段二；
D) 信息面补强：HTF 程序化摘要、经验库自动沉淀。

**Tech Stack:** Python 3.11、pytest、hypothesis（tests/property）、ruff、PyQt6 GUI、openai 兼容 API（DeepSeek-V4-Flash-0731 等，thinking）、Binance U 本位 Futures Testnet、记录体系 JSON（records/pending）+ CSV（trade_records）。

**Spec:** 前置分析结论见对话《分析当前AI给出策略的依据是什么，是否可以优化》（基线记录：`records/pending/2026-09-08_16-09-58_XRPUSDT_15m.json`；该次增量分析 prompt 173,787 tokens、cache 46%、stage2 延迟 50.7s；`config/settings.json`：`decision_stance: conservative`、`decision_confidence_threshold: 40`、`experience_max_entries: 0`）。核心事实：AI 策略 = Brooks 规则 txt（prompt_engineering/）+ 程序几何特征（kline_features / market_features / structure_levels / pattern_routing）+ LLM 判断（周期、信号棒质量、三价、置信度）+ 增量记忆；无结果回灌、置信度纯自评、阶段一闸门弱过滤、htf_text 为空、经验库关闭。A2 数据口径已评审定稿（评审：`docs/superpowers/plans/2026-09-08-A2-outcome-store-review.md`；Q1–Q5 全部按默认：win_r 主口径、close_reason v1 前缀 best-effort、net==0 计 loss 并同步 trade_pnl_report、币种=白名单∪CSV、文件名日期预筛）。

## Global Constraints

- 模型/API 无改动：沿用现有 router/provider 配置；不改 decision JSON schema 顶层键与枚举（wait|reject|trade|proceed、8 类 cycle_position、direction 枚举），新数据一律走**新增可选字段**。
- 配置只加键不改键：新增项同时写入 `config/settings.json` 与 `config/settings.example.json`，缺省值保证旧配置直接可用。
- prompt 注入遵守现有缓存策略：静态文本固定前缀顺序；动态统计文本放 user 消息后段，长度有硬上限。
- 所有面向用户文案简体中文（同 prompt 硬规则）；代码注释/文档中文。
- 不写 API key、不打印密钥；日志脱敏沿用现状。
- 测试：新逻辑必须有 tests/unit 用例；聚合类函数补 tests/property hypothesis 属性测试；改动复用逻辑后旧测试（tests/unit、tests/property）必须全绿。
- 提交信息格式固定：`<emoji> <type>(scope): 中文描述`（如 `✨ feat(feedback): 新增结果回灌统计`），commit 前 `git status`/`git diff` 复核。
- 结果统计口径全局统一：net = realized + fees（资金费单列）、仅已平仓计胜负、**net==0 计 loss**（trade_pnl_report 同步修改，见 Task A5 Step 2）。
- 只读工具不影响实盘；涉及 testnet 执行路径改动必须 dry_run 全链路验证（probe_binance_env 冒烟）。

---

# Phase A（P0）：结果回灌闭环 + 置信度校准 —— 最高优先级

**为什么最先做**：当前 `trade_confidence`/`estimated_win_rate` 为 LLM 自评无校准；`strategy_files_used`、`detected_patterns` 全量落盘但从未回流。没有客观胜率统计，后面所有"优化是否有效"都无法验证。本阶段交付"测量仪表"。

**阶段验收口径**：
1. `tools/calibration_report.py --days 30` 可输出：各策略文件/周期分组的 次数、胜率、平均 R、平均置信度、Brier score、建议阈值；
2. stage2 prompt 出现历史先验块（样本不足时自动省略，不产生"0 次样本硬编胜率"）；
3. 全量旧测试绿，新增单测/属性测试绿。

### Task A1: 配对逻辑下沉 + 决策归属层（只读重构，不动交易）

**Files:**
- Create: `pa_agent/trading/replay_pairing.py`（把 tools/_pa_sim_common.py 的"成交重建/income 配对"核心逻辑搬入，纯函数、无 IO 依赖的测试友好版）
- Modify: `tools/_pa_sim_common.py`（改为 re-export 新模块，保持 `tools/trade_pnl_report.py`、`tools/sim_trailing_stop.py` 零改动可用）
- Test: `tests/unit/test_replay_pairing.py`、`tests/property/test_replay_pairing.py`

**Interfaces（A2 评审契约 §5.2 落地）：**
- `rebuild_trades(orders, fills, symbols) -> list[Trade]`：沿用现有 LIFO 语义（原 _pa_sim_common.py:181），同仓多腿合并为一条信号仓
- `attach_decisions(trades, decision_rows, *, join_hours=48) -> (list[AttachedTrade], AuditReport)`：①材料 hash 数值形态精确命中（沿用 cid_variants int/float/str 组合）；②多命中消歧：取 `max(plan_ts) ≤ opened_at` 且 gap ≤ join_hours，并列或超窗 → ambiguous（计数不入统计）；③无命中 → unmatched_manual；`AttachedTrade` 附 matched_decision_idx/conf/stop/target；`AuditReport = {matched, unmatched_manual, ambiguous, ambiguous_rows: list}`
- `classify_outcome(net) -> "win"|"loss"`：win 仅 net>0，**net==0 计 loss**（Q3 定稿，供 A5 与 trade_pnl_report 共用，避免口径漂移）

- [ ] Step 1: 通读 `tools/_pa_sim_common.py` 与 `tools/trade_pnl_report.py`，圈出纯函数边界（fetch/IO 留在 CLI，配对与分类下沉）
- [ ] Step 2: 写失败测试 `tests/unit/test_replay_pairing.py`：完全配对 / 部分成交 / 无成交决策；数值形态 65000/65000.0/"65000" 命中同一材料；同一材料两条决策 → 取最近且 ≤opened_at；gap>48h → ambiguous；无 cid → manual；classify_outcome(0.0)="loss"
- [ ] Step 3: 属性测试 `tests/property/test_replay_pairing.py`：任意成交序列下 配对总成交名义 ≤ 账户名义、无重复归属、ambiguous 行数 ≤ 多命中组数（hypothesis）
- [ ] Step 4: 实现 `replay_pairing.py` 至测试绿
- [ ] Step 5: 改 `tools/_pa_sim_common.py` 为薄转发层，跑 `python tools/trade_pnl_report.py --days 5` 与旧测试确认无回归（网络不可用则以 import + 既有单测为准）
- [ ] Step 6: Commit `♻️ refactor(trading): 提取复盘配对与决策归属层`

### Task A2: 结果合并入库（outcome store，v2 契约）

**Files:**
- Create: `pa_agent/feedback/__init__.py`、`pa_agent/feedback/outcome_store.py`
- Test: `tests/unit/test_feedback_outcome_store.py`、`tests/unit/test_feedback_outcome_audit.py`

**Interfaces（契约 §5.1-§5.3）：**
- `symbols_scope(settings, csv_dir) -> list[str]`：白名单 ∪ trade_records CSV 出现符号（含 ADA/LINK；Q4 定稿）；无白名单回退 DEFAULT_SYMBOLS
- `load_decision_records(records_root: Path, days: int, symbols) -> list[dict]`：**文件名日期预筛**（解析 `YYYY-MM-DD_HH-MM-SS_` 前缀，仅窗口内 + meta.symbol 命中者读盘；Q5 定稿），字段含 meta/symbol/timeframe/ts、stage2_decision.decision、strategy_files_used、stage1_diagnosis.detected_patterns
- `load_csv_fallback(csv_dir, symbols, days)`：pending 未命中行的 S3 兜底（分组键降级：cycle_position/direction 取自 CSV 列，strategy_files=() 且 source="csv"）
- `merge_outcomes(attached, decisions) -> (list[OutcomeRow], AuditReport)`：逐行构造 OutcomeRow（字段见评审 §5.3：uid=sha256(cid|opened_ms|closed_ms)、net_usdt、risk_usdt=qty×|entry−stop|（stop 缺失/相等→None）、win_r、outcome=win|loss|open（closed 且 net>0→win，否则 loss；open 单列）、close_reason=stop|tp|be_stop|timestop|structure|manual|unknown（**v1 best-effort**：平仓成交 orderId→orders.json clientOrderId 前缀 pa-sl-/pa-tp- 归因，无前缀→structure/manual/unknown；Q2 定稿）、source=pending|csv）
- `write_outcome_csv(rows, path)`：追写 `trade_records/outcomes.csv`，uid 幂等（重复跑不重复写）；`write_audit_json(audit, logs_dir)`：落 `logs/outcome_audit_YYYYMMDD.json`（matched/unmatched_manual/ambiguous/csv_only_rows/pending_only_materials/income_diff_by_symbol）

- [ ] Step 1: 失败测试：fixture = 2 笔 fill（1 完整平仓 + 1 未平仓）+ 同材料重复决策 2 条 + manual fill 1 笔 → 断言 OutcomeRow 字段、ambiguous 计数、unmatched 计数、uid 幂等（同输入跑两遍不重复写）、close_reason 前缀归因
- [ ] Step 2: 用真实数据摸清口径：跑一次 `tools/trade_pnl_report.py --detail`（网络可用时）看配对输出与 state 结构，字段映射写入 outcome_store docstring；无 key/断网时以 §3 实测快照（3254 pending、30 组重复材料、43 CSV-only）造回归 fixture
- [ ] Step 3: 实现 `outcome_store.py` 至测试绿（fetch 层不复用：只消费 cache JSON，fetch 动作属 CLI 显式步骤，保证离线单测）
- [ ] Step 4: Commit `✨ feat(feedback): 决策与成交结果合并落库(v2)`
- [ ] Step 5:（v1.1 调查项 D1，不在本任务完成）小脚本探测 close algo 单（pa-sl-/pa-tp-）在 allOrders 的可见性，决定 close_reason 是否升级为精确归因——结论记入 review 文档

**数据缺口记录**：历史存量只有限价单计划（市价单自动成交=0）、无成交源（dry_run/disabled 期）时 outcomes.csv 为空属正常；audit 报告"无成交源"而非报错。

### Task A3: 规则分组统计（rule stats）

**Files:**
- Create: `pa_agent/feedback/rule_stats.py`
- Modify: `pa_agent/config/settings.py`（新增 settings 节，见下）
- Test: `tests/unit/test_feedback_rule_stats.py`

**Interfaces:**
- `StatsGroup = {key: str, n: int, win_rate: float|None, avg_r: float|None, avg_confidence: float|None, expectancy_r: float|None}`
- `build_group_stats(rows: list[OutcomeRow], group_by: tuple[str,...], min_samples: int, days: int) -> dict[str, StatsGroup]`：支持 group_by ∈ {strategy_file, (cycle_position,direction), (cycle_position,direction,patterns 首项)}；win_rate 仅在 n≥min_samples 时非 None
- 只统计已平仓（closed=True）笔算胜率；未平仓计入 open_n 单独字段

- [ ] Step 1: 写失败测试：fixture 含 3 胜 2 负 + 1 未平仓，断言 n/win_rate/expectancy_r/平均置信度、min_samples=10 时 n<10 组返回 win_rate=None
- [ ] Step 2: 实现 `rule_stats.py`（滚动窗口 days 参数化，默认 30/90）
- [ ] Step 3: 新增配置项并写默认值：`"feedback": {"enabled": false, "days": 30, "min_samples": 10, "max_prompt_lines": 6, "group_bys": ["strategy_file", "cycle_direction"]}`（默认关，开=无副作用上线路径）同步 settings.example.json
- [ ] Step 4: Commit `✨ feat(feedback): 策略分组胜率统计与配置`

### Task A4: prompt 先验注入（base-rate injector）

**Files:**
- Modify: `pa_agent/ai/prompt_assembler.py`（在 `_MARKET_FEATURES_AUTHORITY_NOTE` 同级的 stage2 user 消息后段追加统计块；样本不足/未开启则不追加）
- Create: `pa_agent/feedback/base_rate_injector.py`
- Test: `tests/unit/test_feedback_base_rate_injector.py`

**Interfaces:**
- `render_base_rate_block(groups: dict[str, StatsGroup], max_lines: int, today: str) -> str`：输出如 `【历史先验 近30天】震荡区间交易策略.txt: 32 次(平 27) 胜率 44% 均盈亏 -0.1R 平均置信 58 —— 低于全样本均值，历史期望为负，谨慎跟单`；**只陈述统计，不写命令**
- 注入文本长度上限 max_prompt_lines 行；整块字符数上限 800，超出截断为 Top lines（按 n 降序）
- 调用点：stage2 prompt 组装末尾、最终任务指令之前；若组内样本 <min_samples，该组静默省略；全局无任何组达标 → 整块省略（**禁止**输出"无历史数据"占位话术）

- [ ] Step 1: 写失败测试（渲染格式、排序、上限截断、全空省略、转义——数值一律程序生成不允许模型杜撰）
- [ ] Step 2: 实现 `base_rate_injector.py`；在 prompt_assembler 找 stage2 user 内容拼接点（grep `_STAGE2_TAIL_REMINDER`/任务文本末尾），插入 `feedback.enabled` 门控调用
- [ ] Step 3: 单测 prompt_assembler：启用/关闭下输出差异仅限新增块；旧 golden 测试若存在同步更新
- [ ] Step 4: Commit `✨ feat(ai): stage2 注入历史胜率先验`

### Task A5: 置信度校准报告工具（含口径同步）

**Files:**
- Create: `tools/calibration_report.py`
- Modify: `tools/trade_pnl_report.py`（stat() 改用 A1 的 `classify_outcome`：win=net>0、**net==0 计 loss**；Q3 定稿，全工具口径一致）
- Test: `tests/unit/test_calibration_report_math.py`、`tests/unit/test_trade_pnl_report_stat.py`（若原工具无单测则新建覆盖 stat 分组/分桶）

- [ ] Step 1: 写测试：给定 (confidence, realized) 序列（含 net==0 用例）断言分桶 (50-59/60-69/70-79/80+) 实际胜率、Brier score、建议阈值 = max(分桶内 实际胜率≥confidence 边界) 的算法
- [ ] Step 2: 同步 `tools/trade_pnl_report.py`：stat() 引 `classify_outcome`，0 计 loss；重跑其单测/属性测试确认行为一致（网络不可用则以 import + 单测为准）；历史 CSV 重算口径 diff 复核一次
- [ ] Step 3: 实现 CLI：`python tools/calibration_report.py --days 30` 读 outcomes.csv 输出：全样本 Brier、各策略文件/周期组表、conf 分桶校准曲线、source(pending/csv) 占比、建议 `decision_confidence_threshold` 上下调提示；**报告头部显式标注"条件样本：通过机会闸门且自动成交"（R9 选择偏差）**
- [ ] Step 4: 跑通并对 XRP/ETH/SOL 历史 CSV 出首份报告（写 `logs/calibration_report_YYYYMMDD.txt`），确认可读
- [ ] Step 5: Commit `✨ feat(tools): 置信度校准报告工具与口径同步`

**Phase A 冒烟**：①离线：fetch 一次账户数据（只读）→ 重建历史 → 复核 `logs/outcome_audit`（matched/unmatched/ambiguous/income diff，diff 超容差告警）；②`feedback.enabled=true` + testnet dry_run 跑 3 天 → 复核 outcomes.csv uid 去重正确 → `calibration_report.py` 输出稳定 → ③无异常再切 dry_run=false 试运行 2–4 周收集真样本。**评审闸**：样本 ≥50 时，若某组实际胜率与模型 confidence 差 >15 个百分点，记录并提交 Phase B 证据。

---

# Phase B（P1）：周期路由与节点判定程序收权

**为什么第二**：周期误判 → 错策略文件 → 全链错误；现在模型自选 8 类周期无程序约束。A 的统计先验证"模型判断错在哪"，B 再收权，顺序不可反。

**文件规划：**
- Create: `pa_agent/ai/cycle_candidates.py`、`pa_agent/ai/node_prefills.py`
- Modify: `pa_agent/ai/prompt_assembler.py`（stage1 注入 `cycle_candidates` 块）、`pa_agent/ai/coherence_checks.py`（新校验）、`pa_agent/ai/prompts/schemas.py` 或 stage1 提示文本（候选约束说明）
- Test: `tests/unit/test_cycle_candidates.py`、`tests/unit/test_node_prefills.py`、`tests/unit/test_coherence_checks.py`（扩展）

### Task B1: 程序周期候选集

- [ ] Step 1: 测试先行：给定程序特征（EMA 斜率、ATR 归一化箱宽、通道拟合残差、breakout_quality、barbwire_score、波段枢轴数、price_position），断言候选输出 top-2 及每候选附带证据串；规则表在模块 docstring 写明
- [ ] Step 2: 实现：候选规则尽量复用已算特征（barbwire≥阈→tight_channel/铁丝网系、MM 目标+单边枢轴→通道系、箱宽≤X×ATR 且双枢轴→trading_range、spike 特征→spike/极速系、残余无法归类→unknown 空候选=不约束）
- [ ] Step 3: 注入 stage1 user 消息：`【程序周期候选】top1: trading_range(证据…), top2: trending_tr(证据…); 输出 cycle_position 须命中候选；不认可需走 node_overrides 并逐条引 K 线`
- [ ] Step 4: coherence_checks 扩展：模型输出不在候选集且无 node_overrides → 判定为一致性失败走现有重试通道
- [ ] Step 5: 用 `tools/replay_direction_gates.py` 同思路跑历史 records 重放：统计"旧输出 vs 候选集"偏差率，偏差率 >15% 则回查候选规则阈值（调宽候选或修规则）
- [ ] Step 6: Commit `✨ feat(ai): 周期路由程序候选集约束`

### Task B2: 边界与信号棒质量预判

- [ ] Step 1: 测试：`prefill_boundary(price_position, dist_atr_ratio) -> 是|否`（price_position ≤0.25 或距最近结构位 <0.5×ATR → 下沿=是，≥0.75 同理上沿）；`prefill_signal_quality(kline_features…) -> strong|medium|weak|invalid` 初判（趋势棒+顺向+极点收盘→medium 起，+突破/结构事件→strong；反向前提→invalid）——规则先粗后精
- [ ] Step 2: 实现并把预判文本注入 stage2 对应节点（§6.3、§9.0）作为"程序预判，可给理由降级、升级需引具体 K 线证据"
- [ ] Step 3: coherence 校验：升级未引证据 → 重试提示
- [ ] Step 4: 重放旧 records 对比预判与模型回答分歧率，分歧高节点继续加规则
- [ ] Step 5: Commit `✨ feat(ai): 边界与信号棒质量程序预判`

**Phase B 冒烟**：monitor/testnet 跑 2 周；用 Phase A 报告对比 B 前后"同组胜率/置信度差"；重试率不得上升（模型被逼越界时会乱写，需盯 retry 日志 `pa_agent.log`）。

---

# Phase C（P2）：成本与延迟优化

**为什么第三**：纯省成本，不动正确性；必须等 A 的仪表上线后才能证明"省钱没伤质量"。

### Task C1: prompt 成本审计基线

- [ ] Step 1: `tools/audit_prompt_cost.py`：遍历 records/pending 最近 N 条，复用 `pa_agent/ai/token_counter.py` 输出各 section（规则文件/特征表/K 线表/指令块）token 占比表，落 `logs/prompt_audit_YYYYMMDD.txt`
- [ ] Step 2: Commit `✨ feat(tools): prompt 成本审计工具`（输出即 C2 依据）

### Task C2: 静态前缀缓存友好化 + 去重瘦身

- [ ] Step 1: 审计表确认最大块（预期：策略规则全文 + 防呆指令重复），列出可合并/去重项（如 4 个"硬约束/禁止"块合并为一个，语言要求表压缩）
- [ ] Step 2: 调整 prompt_assembler 组装顺序：全部静态文本 → system 首条；动态（K 线/特征/诊断/统计）→ 后段；同类规则文件相邻固定排序
- [ ] Step 3: 回归：旧测试绿；重放 2 条 records 确认输出 schema 语义等价（trace 校验通过率不变）
- [ ] Step 4: 用 C1 工具重测：目标 中位数 prompt tokens 降 ≥25%、cache 命中 ≥65%（基线 46%）
- [ ] Step 5: Commit `⚡ perf(ai): prompt 去重与缓存友好排序`

### Task C3: 事件闸门轻量模式（可选开关，默认关）

- [ ] Step 1: 程序事件检测：复用 program_features 变化（新突破/区域切换/swing 更新/barbwire 翻转/新 signal 质量≥medium）→ `event_flag`
- [ ] Step 2: 配置 `"light_mode": {"enabled": false, "max_quiet_bars": 3}`：无事件且连续安静达 N 根时 monitor 跳过阶段二只出阶段一 keepalive（记录 skipped_stage2=true，不推送）
- [ ] Step 3: 测试：事件触发矩阵用例；冒烟在 testnet 验证
- [ ] Step 4: Commit `⚡ perf(monitor): 无事件安静期跳过阶段二`

**Phase C 验收**：A/B 对照（同品种同窗口）——light_mode 期间推送数与手工判定一致率 ≥95%（复核样本 30+）；平均每 bar 分析 token 与延迟下降数据入报告。

---

# Phase D（P3）：HTF 摘要 + 经验库自动沉淀

**为什么最后**：HTF/经验都是"更多输入"，输入质量依赖 A 的测量与 B 的约束稳定后才有意义。

### Task D1: 高周期程序化摘要

- [ ] Step 1: 调研现有数据源支持（TradingView/yfinance 可拉同品种 1h/4h），确认 monitor 取数路径可复用；检查 `trend_context.py` 是否已有跨周期雏形可扩展
- [ ] Step 2: 新增 `pa_agent/ai/htf_summary.py`：只对 HTF 算程序特征摘要（EMA 斜向/通道向/最近 swing/距结构位），输出 ≤400 字紧凑文本注入 `htf_text`
- [ ] Step 3: 配置 `"htf": {"enabled": false, "timeframes": ["1h", "4h"]}`；测试渲染与 token 增量上限
- [ ] Step 4: Commit `✨ feat(ai): 高周期程序化摘要注入`

### Task D2: 经验库自动沉淀 + 读取启用

- [ ] Step 1: outcome_store 增 `export_experience_cases()`：已平仓且 |win_r|≥1 的完整案例（含该轮 stage1_diagnosis 摘要 + decision + outcome）写入 `experience/<cycle_position>/success_cases|failure_cases/<symbol>_<ts>.json`（对应现有目录骨架）
- [ ] Step 2: 确认 `pa_agent/records/experience_reader.py` 检索键（周期/方向/setup），不足则补
- [ ] Step 3: 配置 `experience_max_entries` 由 0 上调（如 8）前，先做一次注入长度实测（每例 ≤400 字符约束已有）
- [ ] Step 4: Commit `✨ feat(feedback): 经验案例自动沉淀`

**Phase D 验收**：开启后 prompt 增加量受控（audit 工具确认）、重放无 schema 破坏；经验检索命中率与"是否改变决策"抽样人工复核。

---

# 整体依赖与决策顺序

1. **Phase A 先行且不可跳**：它是 B/C/D 的测量仪表与回归标尺。
2. A5 校准报告每 2–4 周跑一次，作为所有后续阶段的 go/no-go 输入。
3. B 与 A 可并行开发但 **B 上线须在 A 有 ≥50 样本之后**；C、D 任何时间可做但效果验收都以 A 的报告为准。
4. 每阶段独立提交、独立回滚（配置开关全部默认关/不动默认行为），GUI 路径（pa_agent/gui/*）本方案不动，除 prompt_assembler 共享入口外不碰界面代码。

# 风险与对策

- **样本不足/市场regime切换**：统计一律滚动窗口 + min_samples 门控 + 分组含 regime（cycle_position），报告注明窗口；不把短窗口当永恒真理。
- **模型被候选集逼到乱填**：候选为空=不约束（unknown 类）；coherence 失败走现有重试而非硬拒；重放分歧率 >15% 先调规则再上线。
- **测试网量小无统计意义**：A 阶段先接 testnet + 历史 CSV 存量数据（复盘工具已能重建），实盘数据同一管线。
- **prompt 瘦身破坏输出稳定性**：只删重复说明不改语义；trace 校验通过率与重试率作为回归指标写进 C 验收。
- **范围蔓延**：Phase A 若数据链路复杂，A2 先只接 testnet 订单来源一种，账户 income 对账放 A5 之后增量做。

---

# 执行记录（截至 2026-09-08）

- Phase A 完成
- Phase A 全部完成并真机验证：testnet 合并 65 条(win 25/loss 40，全已平仓)；close_reason=stop 27/tp 23/structure_or_manual 15。
- D1 结论：2025-12 起条件单走 Algo Service(/fapi/v1/algoOrder)，但触发成交回传 allOrders 且带 pa-sl-/pa-tp- 前缀，best-effort 归因有效，无需升级查询。
- 口径修正：归属优先 pending 源(CSV 15 分钟同材料剔除)；outcomes.csv 同 uid 刷新 → 来源 pending 58 / csv 7，strategy_files 分组恢复。
- 运行态(不入库)：feedback.enabled=true(closed>=50)；stage1_kline_rows_limit=40 A/B 中(本地估 71,000→68,432 tokens/轮)；首份校准报告：胜率 38.5%、Brier 0.2728、上涨通道 +1.63R / 区间系 -0.3R。
- Phase B 已完成提交(c56fb3c, 9f3b24e)，待实跑冒烟；Phase C：C1/C2 完成、去重路线判死、C3 light_mode 未开发；Phase D 未开工。
- 分支 feat/phase-a-feedback-loop 未合并 main。
- Phase C 完成：C1 audit(阶段一60,280/阶段二115,772 tokens中位,缓存66%/34%)、C2 表裁剪开关(默认关)、C3 轻量模式(默认关, commit 95005f1)。
- Phase D 完成：D2 经验案例自动沉淀(commit 2222997, 28 条真实案例已导出, 经验库 reader 启用 max_entries=8)；D1 HTF 程序化摘要(commit 560f02f, monitor 可选拉取 1h/4h 注入 stage1, 默认关)。
- 待运行时验证项：monitor 实跑冒烟(light/HTF 先关)、A/B(kline 40)观察 2-4 天、每周 outcome_merge+calibration、B 后重试率对比。
- **双源漂移与历史口径变化**：CSV/pending 匹配不到的行进 audit 不硬凑；trade_pnl_report 改 0→loss 后历史 CSV 按新口径重算一次并复核 diff（net==0 极罕见）。
- **无成交源期**：dry_run/disabled/无 testnet key 时 outcomes.csv 为空属正常，audit 报"无成交源"不报错；存量历史无市价单成交、只有限价计划，统计显著性不足前不做结论（min_samples 门控兜底）。
