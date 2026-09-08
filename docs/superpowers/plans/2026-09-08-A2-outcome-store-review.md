# A2「决策 × 成交结果合并」数据口径评审

> 评审对象：优化计划《2026-09-08-ai-strategy-optimization.md》Task A2（`pa_agent/feedback/outcome_store.py`）。
> 评审方法：只读代码走查 + 本地存量数据实测（不触网、不改文件）。日期：2026-09-08。

## 1 结论（TL;DR）

**可行，照现有复盘工具的口径扩展可建。** 但计划里 A2 的三个隐含假设需要修正：

1. **trade_records/*.csv 不是成交流，是"机会计划流"**——结果合并必须以账户订单/成交（allOrders + userTrades）为事实源，CSV/pending 只负责补充策略上下文；
2. **同一笔成交可能对应多条相同材料的决策记录**（实测 30 组材料在 24h 内重复），join 必须"最近时间消歧 + ambiguous 审计"，不能 setdefault 取第一条；
3. **历史上从未出现市价单自动成交、双源记录存在 ~9% 漂移**（raw 字符串口径），outcome 行必须带来源与审计计数，否则统计会被污染且无感知。

## 2 现有数据流与口径（证据锚点）

### 2.1 决策落盘：两个互相独立的源

| 源 | 内容 | 覆盖 | 关键字段 |
|----|------|------|----------|
| `records/pending/*.json` | 每轮两阶段分析全量（prompt/响应/诊断/决策/usage） | 3254 文件，2026-08-07 ~ 09-08 | `meta.timestamp_local_ms/symbol/timeframe`、`stage2_decision.decision`（三价+置信度）、`strategy_files_used`、`detected_patterns`（在 stage1_diagnosis） |
| `trade_records/<SYM>_<tf>.csv` | 有单机会决策行 + K 线图 | 474 行（限价 471/突破 3） | 见 `pa_agent/records/trade_logger.py` `_CSV_FIELDNAMES`（36-100）；**无** strategy_files_used / detected_patterns |

CSV 写入条件（monitoring/service.py:121 `_has_order_opportunity` + 878 `save_trade_record`）：order_type ∈ 机会类型 且 trade_confidence ≥ stance 阈值。**同一计划被后续 K 线"延续/更新"时会追加多行**（prev_plan_relation / prev_plan_invalidated 列，trade_logger.py:599-602），所以 CSV 行数 ≫ 实际下单笔数。

### 2.2 执行层事实（binance_usdm_testnet.py）

- 只自动执行 市价单/限价单（:864）；**突破单必须人工复核**（:866）；
- 每 symbol **单一持仓** guard（:941）→ 不存在同向叠仓；
- **每 symbol 只保留一个 resting 限价**，新信号会取消旧的（`_replace_pending_limit` :1236，含部分成交先保护/回滚 :1305-1321）；
- 入场 clientOrderId = `pa-entry-` + sha256(material)[:27]（`_signal_id` :3471 + `_entry_client_id` :387；material = symbol+direction+type+entry+stop+target，**不含 timeframe**）；重试同 id 去重；
- 成交后挂保护单 `pa-sl-` / `pa-tp-` close algo（`_attach_protection` :1327）；保本移动止损 / TP1 部分止盈 runner（:1392+）会改单；结构否定离场走 `structure_exit`（monitoring/service.py:814）市价平仓；
- 状态机：rejected / skipped(disabled/cooldown) / dry_run（不发 API）/ failed / submitted|pending（:812-860）。dry_run 与 disabled 期间**无成交**。

### 2.3 复盘工具已打通的部分（A2 直接复用）

tools/_pa_sim_common.py：`load_decision_index`（:75，pending JSON + CSV 双源建 material-hash 索引）、`rebuild_trades`（:181，逐 fill LIFO 配对重建一笔信号仓，含未平仓）、`_match_decision`（:164，hash 精确 → 最近时间回退）。tools/trade_pnl_report.py：指标口径 `net = realized + fees`（:32，**资金费另列**）、win = net>0（:40）、按 closed 算胜率、未平仓浮盈单独拆分、income 对账（:205-222）、时间本地 +8。

## 3 存量数据实测快照（2026-09-08，本地只读）

| 项 | 数值 |
|----|------|
| records/pending JSON | 3254（08-07~09-08；08-12~08-30 断档，疑似清理/迁移） |
| pending 决策类型分布 | 不下单 2158 / 限价单 463 / 突破单 2 / **市价单 0** |
| trade_records CSV 行 | 474（限价 471 / 突破 3） |
| 市价/限价 materials（pending） | 431 个唯一材料 / 463 次 |
| 24h 内重复材料（歧义候选） | **30 组（多出 34 行，≈7%）** |
| CSV 行无对应 pending 材料 | **43 行**（BNB 8 / BTC 12 / ETH 15 / SOL 5 / 其余 3） |
| pending 材料无对应 CSV 行 | 38 个 |

> 43/38 为 raw 字符串比对的上界：真实 join 用数值形态（cid_variants 的 int/float/str 组合）会消化一部分。结论仍是：**双源漂移真实存在，必须合并 + 审计，禁止单源**。

## 4 口径问题清单（R1-R12）

| # | 问题 | 证据 | 影响 | 处置 |
|---|------|------|------|------|
| R1 | CSV 是计划流不是成交流；市价单历史=0 | 2.1/2.2、§3 | 直接用 CSV 算胜率会得到"计划胜率"（大部分限价从未触发） | 成交事实源 = allOrders/userTrades；CSV 只补上下文 |
| R2 | 同一材料多次出现 → cid 相同，join 歧义 | §3（30 组） | 错配→把 A 计划的结果记到 B 计划（周期/策略文件不同） | 精确规则：候选多命中取"plan_ts ≤ opened_at 且最近"；gap>48h 或无 ≤ 候选 → ambiguous 计数不入统计 |
| R3 | 执行会改价格：程序止损外扩/价格 tick 规整/risk 锚 | binance 文件 stop 外扩逻辑 + stage2_normalizer.py:642 注释 | 记录 stop 与实单止损不一致 → R 计算偏差 | 风险基数 qty×|entry−stop| 的 stop 取**决策记录最终值**（normalizer 已对齐）；实单侧 stop 留待 D1 调查后校验 |
| R4 | 突破单/人工单无自动成交 | :866、manual cid | 误入统计 | 排除：非 pa-entry cid 的成交 → unmatched(manual)；突破单决策无 fills 自然消失，审计计数记录 |
| R5 | 手工/结构离场无 pa-sl/pa-tp 前缀 | structure_exit → close_market_position | close_reason 归因缺失 | v1 best-effort：平仓成交 orderId → allOrders clientOrderId 前缀判定；无前缀 → structure/manual/unknown |
| R6 | 部分成交/部分止盈拆腿 | LIFO + TP1 partial | 单笔信号多 fill | 语义定为"一笔信号整仓"：rebuild 后 realized 汇总，net 与初始风险比 → R |
| R7 | cid 材料不含 timeframe | :3471-3482 | 同 symbol 不同周期同价材料互撞 | 消歧时间维度天然带上（决策记录自带 tf）；碰撞残留计入 ambiguous 审计 |
| R8 | 时间基准：记录 ts=分析提交时刻 vs 订单 time/成交 time；限价单可能挂数小时 | meta.timestamp_local_ms；resting limit | 窗口与邻近匹配错误 | 匹配用 opened_at（成交时刻）；规则：plan_ts ≤ opened_at，48h 上限；fetch 窗口提前 24h 余量（现有 fetch_start 逻辑沿用） |
| R9 | 选择偏差：CSV/pending 只含过闸门的机会；rejected/skipped 未结构化落盘 | service.py:121、execution 状态仅日志 | base rate 是"过闸且成交"条件胜率 | 文档显式标注条件；rejection 结构化采集列为 v1.1（monitor 已有状态字面量，缺落盘） |
| R10 | pending 3254 文件全扫太重 | §3 | 每次统计几百 MB IO | 文件名日期预筛（窗口 days）+ symbol glob；增量索引推后 |
| R11 | 双源漂移 43/38 | §3 | 分组键（strategy_files）缺失时降级 | OutcomeRow 带 source 字段；CSV-only 行用 diag_cycle_position/direction 分组，strategy_files=unknown 并计数 |
| R12 | 零净额归类与现有报告不一致风险 | pnl stat：net==0 既不算 win 也不算 loss | 校准分桶口径漂移 | 沿用现有口径（0 计入 closed 不计胜负）或显式改 0→loss（见 Q3），全工具统一 |

## 5 修订后的 A2 数据契约 v2

### 5.1 数据源（只读，全部已有代码能力）

- **S1 成交事实**：`fetch_account_data`（tools/_pa_sim_common.py:122）拉 allOrders + userTrades + income + positions（需 testnet key；无 key 时 A2 静默产出"无成交源"审计，不崩）
- **S2 决策全量**：records/pending JSON（文件名日期预筛；`load_decision_index` 既有解析逻辑）
- **S3 决策兜底**：trade_records CSV（S2 未命中时；字段降级见 R11）

### 5.2 Join 规则（A1 replay_pairing 抽取时按此定）

1. 候选：cid_variants 数值形态 hash（沿用 :60-72）命中决策记录；
2. 多命中消歧：取 `max(plan_ts) ≤ opened_at` 且 gap ≤ 48h；并列/超窗 → ambiguous（计数、不入统计、落 `outcomes_audit.csv`）；
3. 无命中：unmatched(manual) 计数；不为它造策略行；
4. 同一 orderId 多 fill / 同仓多腿：在 rebuild_trades LIFO 层合并（沿用），一条信号仓 = 一个 OutcomeRow；
5. timeframe/符号一致性：时间消歧天然确定 tf；审计输出保留 (symbol, material, opened_at, 命中记录数)。

### 5.3 OutcomeRow（dataclass，字段=来源）

`uid`(sha256(cid|opened_ms|closed_ms))、`symbol`、`timeframe`(S2/S3)、`direction`、`order_type`、`entry_avg`、`qty`、`stop`、`target`（S2）、`net_usdt`(realized+fees)、`fees_usdt`、`risk_usdt`(qty×|entry−stop|，stop 缺失/相等→None)、`win_r`(net/risk|None)、`outcome`(win|loss|flat|open，0 归 flat 见 Q3)、`close_reason`(stop|tp|be_stop|timestop|structure|manual|unknown，best-effort)、`conf`、`cycle_position`、`diag_direction`、`strategy_files`(tuple，S2 only)、`patterns`(tuple，S2 only)、`stance`、`model`、`ts_record/ts_open/ts_close`、`source`(pending|csv)。

### 5.4 统计口径（与 trade_pnl_report 强制一致）

- net = realized + fees；funding 单列不混入；
- 胜率 = win/closed（open 单独计数展示）；
- win_r 主口径、net_usdt 备查；
- 分组键默认 (strategy_file) 与 (cycle_position, diag_direction)（min_samples 门控沿用计划 Phase A3）；
- income 对账 diff 每币输出（现有 :205-222 逻辑复用），diff>容差 → 审计告警。

### 5.5 审计输出（v2 新增，A2 必交）

每轮统计附带 `audit`：{matched, unmatched_manual, ambiguous, csv_only_rows, pending_only_materials, dry_run_era_est?, income_diff_by_symbol} → 落 `logs/outcome_audit_YYYYMMDD.json`。

## 6 对优化计划文件的修订（Task A1/A2/A5）

1. Task A1（replay_pairing 抽取）：配对核心按 §5.2 规则封装；除工具复用外同时被 feedback 调用；接口签名以 §5.3 为准（`rebuild_trades` 之上再加"决策归属+歧义审计"一层 `attach_decisions(trades, decisions) -> (rows, audit)`）；
2. Task A2：outcome_store 读写 §5.1-S1 的缓存 JSON（fetch 层复用），fetch 动作放 CLI 显式步骤，模块内只消费 cache——便于离线单测与审计重放；
3. Task A5（calibration）：报告头部注明"条件样本（通过机会闸门且自动成交）"；分桶列同时给 n 与 source 占比。

## 7 待拍板问题

- **Q1 R 口径**：主用 win_r = net/(qty×|entry−stop|)（可跨币比较），USDT 备查——同意？
- **Q2 close_reason**：v1 做 best-effort 前缀归因（pa-sl/pa-tp/无前缀三类），还是先做 D1 调查（close algo 订单在 allOrders 的可见性）再定？
- **Q3 零净额**：net==0 沿用现有"计入 closed、不计胜负"，还是显式改 0→loss（校准桶更稳）？
- **Q4 币种范围**：settings 白名单 ∪ CSV 出现 symbols（含 ADA/LINK）？
- **Q5 扫描成本**：v1 文件名日期预筛即可，还是直接上增量索引？

> 默认建议：Q1 同意、Q2 v1 先 best-effort 并保留 D1 为 v1.1 调查项、Q3 改 0→loss 且同步 trade_pnl_report 口径、Q4 白名单∪CSV、Q5 v1 先预筛。

## 执行结论（2026-09-08 补充）
- D1 调查完成：close 条件单走 /fapi/v1/algoOrder(clientAlgoId=pa-sl-/pa-tp-)；触发成交回传 allOrders 且带同前缀 cid(65 条真实归因 stop 27/tp 23/structure_or_manual 15，无 unknown) → 前缀归因定稿。
- 源偏好修正：同订单双写时 CSV 晚约 6 分钟会赢下消歧并丢 strategy_files → prefer_pending_over_csv(同材料 15 分钟内剔除) + outcomes.csv uid 刷新；实测 pending 58 / csv 7。
- 校准首报：胜率 38.5%、Brier 0.2728(偏乐观)，落 logs/calibration_report_20260908.txt。
