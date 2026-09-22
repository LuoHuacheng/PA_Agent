# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序，在 **设置** 中填写你的 **API Key**（明文保存在本机 `config/settings.json`，该文件已被 git 忽略，不会入库）。

   也可直接编辑 `config/settings.json` 中的 `base_url`、`model` 等字段，Key 建议通过 GUI 保存以便字段格式校验。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

4. 如需自定义 TradingView 品种别名，复制模板：

   ```cmd
   copy config\tv_symbol_aliases.example.json config\tv_symbol_aliases.json
   ```

> 本文档的字段表由 `scripts/gen_config_readme.py` 从 `pa_agent/config/settings.py` 与本机 `config/settings.json` 自动生成，
> 手工修改会在下次生成时被覆盖；要补充说明请改 `settings.py` 里字段上方的注释。

## settings.json 字段说明

字段、默认值与取值范围来自 `pa_agent/config/settings.py` 的 Pydantic 模型（权威来源）；说明取自代码中字段上方的注释。**本机当前值**一列读自本机 `config/settings.json`。改完代码或配置后重跑 `python scripts/gen_config_readme.py` 即可同步本文档。

根级 `binance_usdm_environment`：`"testnet"`（默认）/ `"live"`。

### provider — AI 提供商

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `model` | str | `deepseek-v4-flash` | `cline-pass/deepseek-v4.1-flash` | — |
| `base_url` | str | `https://api.deepseek.com` | `http://127.0.0.1:3123/v1` | — |
| `api_key` | str | `""` | 已打码 | — |
| `thinking` | bool | true | true | — |
| `reasoning_effort` | `low`, `medium`, `high`, `max` | `high` | `medium` | — |
| `max_output_tokens` | int（≥1, ≤384000） | `384000` | `32000` | 单次请求的输出预算 (thinking + content 合计)。默认沿用客户端全局上限 384000；若网关按渠道夹断 max_tokens (实测 a6api 夹到 4096)，把这里调到 模型真实输出上限，避免"思考吃满额度、正文 JSON 为空"。 |
| `context_window` | int | `2000000` | `1048576` | 仅用于 UI 上下文占用百分比与告警阈值；不参与请求体。请填模型真实窗口。 |
| `transport_retry_max` | int（≥0, ≤5） | `2` | `2` | 传输层（网络/超时）失败的重试次数与退避秒数。只在**本轮尚未向 UI 吐出任何 token** 时重试: 已经 stream 过的调用重放会把同一段内容再推一遍。 2026-09-22 现场 —— 本地网关抖动窗口内 4/50 轮分析以 "peer closed connection without sending complete message body" 直接报废; 单次尝试等于把一次 5 秒抖动放大成整轮信号丢失。0 = 关闭重试(旧行为)。 |
| `transport_retry_backoff_seconds` | float（≥0.0, ≤60.0） | `5.0` | `5.0` | — |

### general — 通用设置

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `analysis_bar_count` | int（≥2, ≤5000） | `100` | `60` | — |
| `refresh_interval_ms` | int | `1000` | `1000` | — |
| `context_warning_threshold_pct` | float | `80.0` | `80.0` | — |
| `last_data_source` | mt5 / tradingview | `tradingview` | `tradingview` | — |
| `kline_adjust` | `qfq`, `hfq`, `none` | `qfq` | `qfq` | A-share K-line adjust for East Money / Baostock (qfq=前复权) |
| `last_tradingview_exchange` | str | `""` | `` | TradingView 交易所；空字符串 =（自动）依次探测预设列表 |
| `tradingview_username` | str | `""` | 已打码 | TradingView 登录凭据 (tvDatafeed 登录会话, 匿名限流宽松、部分品种仅登录可见); 两者都非空才登录, 否则退回匿名。也可用环境变量 TRADINGVIEW_USERNAME / TRADINGVIEW_PASSWORD (settings 为空时自动读环境变量)。 |
| `tradingview_password` | str | `""` | 已打码 | — |
| `last_symbol` | str | `XAUUSD` | `BTCUSDT` | — |
| `last_timeframe` | str | `15m` | `30m` | — |
| `decision_flow_auto_play` | bool | true | true | — |
| `decision_flow_play_seconds` | int | `50` | `50` | — |
| `alert_on_order_opportunity` | bool | true | true | 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示） |
| `incremental_max_new_bars` | int（≥0, ≤500） | `10` | `10` | — |
| `decision_stance` | conservative / balanced / aggressive / extreme_aggressive | `balanced` | `conservative` | 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿 |
| `decision_flow_default_zoom_pct` | int（≥10） | `600` | `600` | 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%） |
| `stream_pane_font_pt` | int（≥8, ≤28） | `11` | `11` | 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt） |
| `chart_seq_label_font_pt` | int（≥6, ≤24） | `11` | `11` | K 线图上 #序号 标签的字号（pt） |
| `auto_resume_chart_after_analysis` | bool | false | false | 两阶段分析结束后是否自动恢复 K 线图表实时刷新 |
| `keep_analysis` | bool | false | false | 持续跟踪分析：有新K线收盘时自动触发新一轮分析 |
| `strategy_era` | str | `""` | `gates-2026-09-13` | 策略纪元标记: 每上线一组门控/参数就更新(如 "gates-2026-09-13"), 让盈亏归因不跨纪元混叠(D2; tools/audit/pnl_weekly.py 按此分时报表)。 |
| `cancel_keep_analysis_on_retry` | bool | false | false | 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis |
| `decision_confidence_threshold` | int（≥0, ≤100） | `40` | `50` | 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情） GUI/manual-decision confidence gate. Monitoring auto-execution derives its threshold from the stance tiers (see decision_stance). |
| `enable_next_bar_prediction` | bool | false | false | 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token |
| `structure_flip_cooldown_bars` | int（≥1, ≤50） | `3` | `3` | 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘） |

### prompt — Prompt 组装调优

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `stage2_load_full_strategy_library` | bool | false | false | When True, Stage 2 loads every strategy .txt (legacy/test behaviour). |
| `experience_max_entries` | int（≥0, ≤10） | `0` | `8` | — |
| `experience_max_chars_per_entry` | int（≥100, ≤4000） | `400` | `400` | — |
| `stage1_inject_pattern_briefs` | bool | true | true | Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags). |
| `stage1_kline_rows_limit` | int（≥0, ≤60） | `0` | `40` | 阶段一 K 线表最多渲染的最近 K 行数; 0 = 不裁剪(默认, 全量). 裁剪时 更早的 K 线以十根滚轴概览补充 (Phase C A/B 实验开关). |
| `gate_feedback_enabled` | bool | true | true | 把最近被入口门控拦下的同品种方案回灌进阶段一/阶段二。解决 2026-09-22 现场: BTCUSDT 连续三轮重推同一个被 short_block 硬拦的做空方案, 模型看不到 拦截原因。窗口内无拦截时该块为空, 不占 token。 |
| `gate_feedback_hours` | float（≥0.0, ≤72.0） | `4.0` | `4.0` | — |
| `gate_feedback_max_chars` | int（≥100, ≤4000） | `400` | `400` | — |

### validation — 校验与重试

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `normalization_mode` | strict / lenient | `lenient` | `lenient` | — |
| `stage1_coherence_checks` | bool | false | false | Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default. |
| `stage2_coherence_checks` | bool | false | false | Stage-2 trace / diagnosis cross-checks (not order safety). Off by default. |
| `trace_semantic_checks` | bool | false | false | — |
| `strict_bar_by_bar_features` | bool | false | false | — |
| `disable_truncation_repair` | bool | false | false | Allow Stage 1 truncated JSON tail repair before failing syntax validation. |
| `retry_enabled` | bool | true | true | Re-call API with structured feedback when validation fails (format errors). |
| `retry_max` | int（≥0, ≤5） | `3` | `3` | — |
| `retry_max_semantic` | int（≥0, ≤3） | `1` | `1` | Max retries for category=c semantic errors (subset only). |
| `retry_stage2` | bool | true | true | — |

### monitoring — 多品种周期预警

不提供 GUI 管理。直接编辑 `settings.json` 后重启程序生效。每个目标在自身 K 线自然收盘、再等待
`poll_lead_seconds` 的数据延迟后拉取一次行情，不按秒轮询。目标共用当前
`general.last_data_source` 与 `general.last_tradingview_exchange`，但使用独立订阅，互不覆盖。
`targets` 每项为 `{"symbol": "BTCUSDT", "timeframe": "30m", "enabled": true}`。
状态存于本地忽略文件 `config/monitoring_state.json`。

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | false | true | — |
| `targets` | list[MonitorTarget] | [] | [{"symbol": "BTCUSDT", "timeframe": "30m", "enabled": true}, {"symbol": "ETHUSDT", "timeframe": "30m", "enabled": true}, {"symbol": "SOLUSDT", "timeframe": "30m", "enabled": true}, {"symbol": "LINKUSDT", "timeframe": "30m", "enabled": true}, {"symbol": "ZECUSDT", "timeframe": "30m", "enabled": true}] | — |
| `auto_discover` | AutoDiscoverSettings | `enabled=False market='usdm_futures' rank_by='quote_volume' top_n=10 refresh_minutes=60 timeframe='15m' stablecoin_only=True` | —（未配置） | — |
| `max_concurrent_analyses` | int（≥1, ≤8） | `1` | `3` | — |
| `poll_lead_seconds` | int（≥0, ≤120） | `5` | `5` | — |
| `poll_interval_minutes` | int（≥0, ≤1440） | `0` | `30` | 分析的固定节拍（分钟）。0 = 跟随 targets 的 K 线周期（每根收盘分析一次）； >0 = 按该间隔的整点边界分析，且 >=2 根 K 线未处理时自动回退全量分析。 |
| `poll_retry_attempts` | int（≥0, ≤10） | `3` | `3` | — |
| `poll_retry_seconds` | int（≥1, ≤120） | `5` | `5` | — |
| `light_mode_enabled` | bool | false | true | C3 轻量模式: 无结构事件连续安静达 N 根时跳过阶段二(默认关, monitor only). |
| `light_mode_max_quiet_bars` | int（≥1, ≤24） | `3` | `3` | — |
| `light_mode_skip_blocked_session` | bool | false | true | 入口门控已禁的时段内强制跳过阶段二: 那段时间的单必被拦, 阶段二产出没有下单 价值; 只留阶段一(结构否定离场与诊断记录都靠它)。与 binance_usdm_testnet 的 block_session_* 同源, 闸门一放开这条自动失效 —— 放行期跳过会白丢机会。 |
| `shutdown_timeout_seconds` | float（≥2.0, ≤600.0） | `90.0` | `90.0` | SIGTERM/SIGINT 后允许在途分析收尾的上限（秒）。单轮分析要 1-3 分钟，旧的 硬编码 2s + cancel_futures 等于每重启一次就废掉正在跑的那轮信号 (2026-09-22)。超时才强制结束进程；代价是重启可能等这么久。 |
| `htf_context_enabled` | bool | false | true | D1 HTF 背景摘要: 每根 K 线收盘额外拉取高周期程序特征(默认关). |
| `htf_timeframes` | list[str] | ["1h", "4h"] | ["1h", "4h"] | — |
| `htf_max_summary_chars` | int（≥100, ≤2000） | `400` | `400` | — |

### monitoring.targets 条目

| 字段 | 类型 | 代码默认值 | 说明 |
|------|------|-----------|------|
| `symbol` | str | **必填** | — |
| `timeframe` | str | **必填** | — |
| `enabled` | bool | true | — |

### monitoring.auto_discover — 监控标的自动发现

开启后 `targets` 静态列表被替换为按 24h 行情动态排名的 USDⓈ-M 合约，运行中周期性刷新。

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | false | false | — |
| `market` | `usdm_futures` | `usdm_futures` | `usdm_futures` | USDⓈ-M futures (fapi.binance.com/fapi/v1/ticker/24hr). |
| `rank_by` | `quote_volume`, `price_change_pct`, `market_cap` | `quote_volume` | `market_cap` | Rank by "quote_volume" (24h成交额), "price_change_pct" (涨跌幅绝对值) or "market_cap" (CoinGecko 市值排名, 候选池稳定、近似固定). |
| `top_n` | int（≥1, ≤100） | `10` | `5` | — |
| `refresh_minutes` | int（≥5, ≤1440） | `60` | `60` | — |
| `timeframe` | str | `15m` | `30m` | K-line timeframe applied to auto-discovered targets. |
| `stablecoin_only` | bool | true | true | Keep only USDT-settled pairs (excludes USDC/FDUSD/TUSD/etc. markets). |

### feedback — 结果回灌闭环

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | false | true | — |
| `days` | int（≥1, ≤365） | `30` | `30` | 统计滚动窗口（自然日）。 |
| `min_samples` | int（≥1, ≤1000） | `10` | `10` | 分组至少需多少已平仓样本才给出胜率/期望（门控，禁止小样本硬编）。 |
| `max_prompt_lines` | int（≥1, ≤30） | `6` | `6` | 注入 stage2 的历史先验最大行数。 |
| `group_bys` | list[str] | ["strategy_file", "cycle_direction"] | ["strategy_file", "cycle_direction"] | 决策可使用的分组键（strategy_file / cycle_direction）。 |
| `join_hours` | int（≥1, ≤720） | `48` | `48` | 决策记录与成交归属的最大时间窗（小时）。 |

### feishu — 飞书通知

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | true | false | — |
| `webhook_url` | str | `""` | 空 | — |
| `secret` | str | `""` | 空 | — |
| `app_id` | str | `""` | `` | — |
| `app_secret` | str | `""` | 空 | — |
| `notify_on_order_only` | bool | true | true | True = only push when there is an order opportunity. |

### pushplus — PushPlus 通知

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | false | false | — |
| `token` | str | `""` | 空 | — |

### telegram — Telegram Bot 通知

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | false | true | — |
| `bot_token` | str | `""` | 已打码 | — |
| `chat_id` | str | `""` | 已打码 | — |

### binance_usdm_testnet / binance_usdm_live — 自动执行

不提供 GUI 管理，直接编辑 `settings.json` 后重启生效。`binance_usdm_testnet` 与
`binance_usdm_live` **字段同构**（同一份 Pydantic 模型），由根级
`binance_usdm_environment`（`"testnet"` / `"live"`，默认 `testnet`）选择活动环境；
两节同时 `enabled`、或切到 `live` 而 live 节缺密钥时**直接拒绝启动**。
下表"本机当前值"一列取自本机 `settings.json` 的 **testnet 节**。

| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |
|------|------|-----------|-----------|------|
| `enabled` | bool | false | true | — |
| `api_key` | str | `""` | 已打码 | — |
| `api_secret` | str | `""` | 已打码 | — |
| `dry_run` | bool | true | false | — |
| `emergency_stop` | bool | true | false | — |
| `require_analysis_symbol_match` | bool | true | true | — |
| `symbol` | str | `BTCUSDT` | `BTCUSDT` | — |
| `symbol_whitelist` | list[str] | ["BTCUSDT"] | ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "ZECUSDT"] | — |
| `leverage` | int（≥1, ≤20） | `1` | `20` | — |
| `max_notional_usdt` | float（>0, ≤10000.0） | `100.0` | `250.0` | — |
| `risk_per_trade_usdt` | float（≥0.0, ≤1000.0） | `0.0` | `4.0` | P2-1 单笔风险等额仓位: 0=关闭(维持名义=保证金*杠杆), 0<x<=1000 时按 风险金/\|入场价-止损价\| 计算 quantity, 每笔最大亏损(不含费)相同; 所需名义仍受 max_notional_usdt*leverage 上限约束, 超出拒单. |
| `cooldown_minutes` | int（≥1, ≤1440） | `30` | `30` | — |
| `require_trader_equation` | bool | true | true | Enforce the §10.3 trader's equation (win_rate×reward > (1-win_rate)×risk) before automating a market order. Default on for safety. |
| `limit_order_enabled` | bool | true | true | — |
| `market_order_enabled` | bool | true | true | 市价单自动执行。与 limit/breakout 对称的显式开关, 默认开(= 与历史行为一致)。 注意: 提示词全局硬禁令限制的是「追单式」市价(追尖峰/追第一根突破棒/高潮后追), 不是市价单本身 —— 趋势线/结构位被触碰且出现拒绝信号 K 之后的顺势市价是允许的, 见 prompt_engineering/提示词大纲_人设与思维方式.txt 硬禁令 2。 |
| `limit_fill_timeout_minutes` | int（≥1, ≤1440） | `60` | `120` | Resting limit entries use a fill watcher and attach TP/SL after fill. |
| `limit_poll_interval_seconds` | int（≥2, ≤300） | `10` | `60` | — |
| `limit_repricing_min_ticks` | int（≥0, ≤1000） | `3` | `3` | 同价位轮换冷却：上一笔挂单被撤后，若新方案的入场价与它相差不超过 N 跳， 且在冷却窗内，则跳过本次挂单（同价位反复撤挂只赔价差与手续费）。 任一参数取 0 即关闭该闸门。 |
| `limit_repricing_cooldown_minutes` | int（≥0, ≤1440） | `30` | `60` | — |
| `min_stop_mode` | `fixed`, `atr` | `fixed` | `atr` | Stop loss minimum distance from the entry/mark price (percent). "fixed" mode: constant min_stop_distance_pct floor. "atr" mode: floor = max(min_stop_distance_pct, min_stop_atr_multiple × 分析周期 ATR%), where ATR% comes from the latest analyzed bar (decision.atr_pct, injected by the monitor) - high-volatility symbols get a noise-safe minimum while quiet symbols keep tight structural stops. Without atr_pct the plain min_stop_distance_pct floor applies. (P0-2 / 动态止损下限) |
| `min_stop_distance_pct` | float（≥0.0, ≤10.0） | `0.45` | `0.2` | — |
| `min_stop_atr_multiple` | float（≥0.0, ≤10.0） | `1.5` | `1.2` | — |
| `pause_monitoring_on_rate_limit` | bool | true | true | Rate-limit recovery: 限流失败不重试(one-shot, 见 execute_market_signal docstring)。 检测到 Testnet 共享 IP 限流(HTTP 418/-1003, banned until)时, 暂停监控的 行情分析与信号推送直到封禁到期(错过的不补发信号); False = 保持现状. |
| `rate_limit_no_until_seconds` | int（≥5, ≤3600） | `60` | `60` | 限流响应无 banned-until 时间戳(如纯 429)时按此秒数估算封禁窗口. |
| `user_data_stream_enabled` | bool | false | true | 用户数据流(user-data websocket): True = 订阅订单/账户推送事件, REST 轮询 降级为低频对账(共享 IP 限流缓解, P0)。默认关闭, 人工验证连通后开启。 |
| `user_data_stream_ws_url` | str | `""` | 空 | 用户数据流 WS 网关; 留空 = 默认测试网网关。live 迁移时换成主网网关。 |
| `user_data_event_refresh_gap_seconds` | int（≥1, ≤300） | `10` | `10` | WS 事件触发快照刷新/唤醒的最小间隔(秒): 事件风暴防抖. |
| `trend_lookback_days` | int（≥5, ≤120） | `7` | `7` | --- 日线大趋势护栏 (逆势单保护) --- 以币种日线 close 的 trend_lookback_days 天涨跌幅定义大趋势; \|涨跌\| <= trend_neutral_band_pct 视为无趋势 (不做限制). 仅作用于白名单内的币种. 命名不写死天数: 窗口本身是配置值, 早期 trend_30d_* 的写法在窗口调成 7 天 之后已经名不副实 (2026-09-10 改名). |
| `trend_neutral_band_pct` | float（≥0.0, ≤100.0） | `1.0` | `1.5` | — |
| `counter_trend_block` | bool | false | true | 逆大趋势的单直接拒绝。2026-09-10 实证: 逆势单的方向命中比同期市场基线低 24 个百分点(顺势 45.9% / 逆势 37.9% / 基线 62.1%), 是样本里最大的亏损来源。 开启后 counter_trend_min_confidence 与 counter_trend_size_scale 不再参与 判定(先拦, 再谈减仓)。 |
| `counter_trend_min_confidence` | int（≥0, ≤100） | `55` | `55` | 逆大趋势的单所需最低 trade_confidence; 不足直接拒绝 (0 = 关闭该门槛). |
| `counter_trend_size_scale` | float（≥0.1, ≤1.0） | `0.5` | `0.5` | 逆大趋势且通过门槛的单, 杠杆 (名义仓位随杠杆同比例) 乘以此系数, 最小 1x. |
| `block_short_entry` | bool | false | true | --- 入口环境门控 (2026-09-13 shadow_replay 45 天变体验证后新增) --- 只拦"自动下单"这一步: 命中的方案不下单, 但照常推送(Telegram)并在记录里写 execution.status="gate_blocked" —— 否则被拦的信号对操作者完全不可见, 且 连续性守卫会把一个从未提交的方案当成在场挂单一直等下去。 依据(口径 2026-09-20 订正, 见 docs/闸门裁定表.md §4/§5.1): T2 权威样本 logs/shadow_rows_baseline45.csv: 做空 22 笔胜率 32% 净/风险 -0.33, 90%CI [-2.81,+0.32] 跨零 ⇒ 做空单档没有独立成立的证据, 它随「做多+通道/区间」配方(n=14, 90%CI [+1.01,+4.90])一起上线; trending_tr 在修正费用后的 245 笔样本里 99 笔 -144.56U(90%CI [-2.17,-0.72] 全负); 实盘 73 笔中 neutral 诊断单 20 笔胜率 25%。 旧注释引用的"空单 87 笔胜率 16% / trending_tr 118 笔 -0.55"出自未修正费用 模型的 242 笔样本(logs/_variants_exp.txt), 已被上述口径取代, 不再引用。 2026-09-19 45 天矩阵复跑(修正费用模型后): 全量成交 245 笔 净 -119.45U 净/风险 -0.12; 单禁 trending_tr 后 146 笔 +25.11U 净/风险 +0.04 —— 这是 整轮回放里单独一档收益最大的开关, 所以**保持硬拦, 不做软化**。 (原计划把 trending_tr 降级为"缩放仓位"的软控制, 回放证据推翻了它: 禁 trending_tr 净/风险 +0.043, 而禁 neutral 只有 -0.066、RR>=1.5 只有 -0.069; 组合 禁空+禁trending_tr+禁该时段 = 净/风险 +0.116, 是矩阵最优。 同一份回放里"trending_tr 段"成交 99 笔净 -144.56U, 是唯一的净亏大项。) |
| `block_trending_tr_entry` | bool | false | true | — |
| `block_neutral_diag_entry` | bool | false | true | — |
| `block_cycle_allowlist` | list[str] | [] | ["broad_channel", "normal_channel", "trading_range"] | 周期位置白名单: 非空时只允许这些 cycle_position 入场(空 = 不启用)。 2026-09-18 修正费用模型后重跑 45 天回放: trending_tr 20 笔 -29.58U, tight_channel/spike 3 笔 -8.03U; 通道与区间(broad/normal/trading_range) 22 笔 +39.18U。比 block_trending_tr_entry 多挡 tight_channel/spike, 且可灰度、可回放调参而不改代码。 |
| `block_session_entry` | bool | false | true | 禁低流动性时段自动开仓: 回放 45 天该时段 74 笔亏 -290U(美盘尾段+亚盘早段 低流动性); 与 trending_tr 仅 51% 交集, 独立有效 — 组合后模型流从 -0.40R/笔 收到 ≈0.00R (shadow_replay --matrix 2026-09-13)。 2026-09-19 起窗口用 UTC 定义, 不再依赖机器本地时区; 默认 20:00-03:59 UTC 与原 04:00-11:59(UTC+8) 是同一段墙钟时间, 既有证据继续成立。 2026-09-19 45 天矩阵复跑: 禁该窗口在 gtx/off 下净/风险 +0.09(全部 -0.12), 仍是现行组合(禁空+禁trending_tr+禁该窗口)里贡献最大的一档。 |
| `block_session_utc_start_hour` | int（≥0, ≤23） | `20` | `20` | 低流动性窗口(UTC 小时, 跨零点时 start > end)。 |
| `block_session_utc_end_hour` | int（≥0, ≤24） | `4` | `4` | — |
| `entry_gates_mode` | `on`, `dry_run` | `on` | `on` | 入口门控模式: on = 命中即拦(现行行为); dry_run = 命中只记录/计数/日志, 照常下单。 默认必须是 on —— direction_gates_mode 的默认是 dry_run(先观察), 但入口门控的 5 个 block_* 已在实盘拦了很多天, 照抄那个默认就是一次无人察觉的行为变更。 dry_run 的用途是"放行同时留痕": 入口门控命中即 return 会短路掉方向闸门, 想测出方向闸门的真实命中率, 只能先让它被放行(2026-09-22)。 |
| `breakout_order_enabled` | bool | false | true | --- 突破单(STOP_MARKET)自动执行 (2026-09-19 新增, 默认关) --- 动机: 单边行情里模型选对了入场方式却拿不到货。decision_nodes 的周期→方法 映射里 trending_tr → "11.2 突破单", 但执行层此前只认市价单/限价单, 突破单 一律 rejected 丢人工队列; 模型退而求其次写限价单, 45 天回放成交率仅 31.2% (245/786), 单边行情下大量挂单在现价下方被价格走掉。 触发价取 decision.entry_price —— 归一化层已保证它是依据 K 极值 ±1 tick 且 严格越过极值, 这里只做跳动的再对齐(多单向上取整)。 默认 False: 样本只有 45 天/2 条历史突破单, 先落地代码, 由操作者决定何时开。 |
| `breakout_size_scale` | float（≥0.1, ≤1.0） | `0.5` | `0.5` | 突破单名义仓位缩放(触发即市价, 有滑点), 灰度期先半仓。 |
| `min_plan_reward_risk` | float（≥0.0, ≤20.0） | `0.0` | `1.5` | 计划盈亏比下限: 低于该值的方案在入口直接拦(0 = 关闭)。require_trader_equation 用的是模型自报的 estimated_win_rate, 一个负边际的计划照样能算过, 只能做粗筛; 45 天回放(2026-09-17, min_stop_atr=1.5 / 超时 30)显示 RR>=1.5 只留 36% 成交 却砍掉 85% 的净亏 (-138.40U -> -20.74U), 比置信度门槛有效得多 (trade_conf>=55 反而 -22U)。价格缺失/方向矛盾一律按不合格处理。 |
| `max_plan_reward_risk_tp2` | float（≥0.0, ≤20.0） | `0.0` | `3.0` | TP2(远端目标)盈亏比上限: 模型的 take_profit_price_2 超出该值时, 在落盘与 执行之前把**目标拉近**到 entry ± max_rr×risk(0 = 关闭, 默认关闭)。 与 TP1 的 2.0 上限相反 —— TP1 靠"推远止损"压 RR, TP2 只动 reward, 风险 口径(定仓数量/止损价)一律不变。之所以需要: 790 份计划里 TP2 中位 3.28R、 p75 4.27R、最高 21.37R, 而实测均 MFE 只有 +0.855R(短线单 0.22R), 这些远端 目标从未兑现, 实际出场落在 12h time-stop 上 —— 等于用挂号信押一个没人收的 地址。缺 45 天回放旋钮(见 docs/出场侧实验.md 的口径), 所以先灰度观察。 |
| `stop_working_type` | `MARK_PRICE`, `CONTRACT_PRICE` | `MARK_PRICE` | `MARK_PRICE` | 防护单触发基准(workingType)。默认 MARK_PRICE 保持既有行为; 实测 2026-09-16 20:28 ZEC 标记价一度戳到 1207.33(最新价最低只到 1208.63), 把按计划挂在 1208.15 的止损点掉, 随后价格反转直奔原计划止盈 —— priceProtect=TRUE 并未 挡住"标记价单方面背离"引发的触发。官方文档在本机不可达(非公网 IP), 语义 未经厂商确认, 所以把基准显式化: 要用最新价触发就设 CONTRACT_PRICE。 |
| `daily_loss_limit_usdt` | float（≥0.0, ≤10000.0） | `0.0` | `30.0` | --- 日度亏损熔断 --- 当日已实现净亏损(REALIZED_PNL + COMMISSION, 不含资金费)达到该值时停止自动 开新仓, 次日(本地日)自动解除。0 = 关闭。账本查询 weight 30, 所以当天一旦 触发就不再重复查询(直接读运行时 state)。 |
| `breakeven_stop_trigger` | str | `1r` | `off` | --- 保本移动止损 (breakeven stop) --- 持仓浮盈达标后把交易所 STOP 保护单移到入场价, 将"赚过又回吐"的亏损转成 保本离场. 触发条件: 1r = 浮盈 >= 1R (risk = \|entry - stop\|); tp = 浮盈触及 TP1; 1r_or_tp = 两者先到先触发; 也支持分数 R (如 "0.5r" = 浮盈达 0.5R 即 移保本), 用于 RR 较高/浅浮盈回头的单; off = 关闭该功能. |
| `breakeven_min_confidence` | int（≥0, ≤100） | `55` | `55` | 仅对 trade_confidence >= 此值的信号启用保本止损 (0 = 全部启用). |
| `breakeven_poll_seconds` | int（≥2, ≤600） | `10` | `60` | 持仓守护线程轮询 mark price 的间隔秒数. |
| `snapshot_stale_seconds` | int（≥0, ≤600） | `0` | `0` | 账户快照(poller)可接受的最大新鲜度, 秒: 0 = 自动 (max(45, 轮询周期 × 1.2), 保证轮询间隙内守护线程不因快照过期 集体回退直连 REST 而放大共享 IP 限流)。 |
| `tp_partial_close_pct` | float（≥0.0, ≤100.0） | `0.0` | `50` | --- TP1 部分止盈 / runner(TP2) --- 到达 TP1 时平掉此比例的仓位(0 = 关闭, 维持现状全平; 50 = 平一半), 剩余仓位保本后继续持有至 TP2. 0-100 闭区间. |
| `time_stop_minutes` | int（≥0, ≤10080） | `0` | `720` | P2-2 持仓超时(分钟): 0=关闭; >0 时持仓超过该时长(自保护单挂上起算)后, time-stop 线程市价清掉剩余仓位(TP2 阶段剩余半仓同样适用). 上限 7 天. |
| `conf_feedback_mode` | `off`, `on` | `off` | `off` | P2-3 conf->胜率回流运行时闸门: off=只用模型 estimated_win_rate(默认; 回归显示样本不足, 不宜自动生效); on=当同 conf 5 分位桶实测样本 >= conf_feedback_min_samples 时, executor 用桶实测胜率覆盖模型值参与 交易者方程. 桶数据由 tools/trade_pnl_report.py --conf-buckets-out 生成. |
| `conf_feedback_min_samples` | int（≥5, ≤200） | `30` | `30` | — |
| `structure_exit_mode` | `off`, `dry_run`, `on` | `dry_run` | `on` | --- 结构否定自动离场 (structure-failure exit) --- 已开仓位在静态保护单之外, 还可响应诊断否定: 当 stage-1 方向连续 structure_exit_confirm_bars 根已收盘K与持仓相反, 且最新收盘已跌破入场价 (做多) / 升破入场价 (做空) 而尚未触及静态止损时, 提前市价平仓. 模式: off = 关闭; dry_run = 只通知不成交 (默认, 安全观察); on = 自动平仓. |
| `structure_exit_confirm_bars` | int（≥1, ≤6） | `2` | `2` | 诊断反向连续确认所需已收盘K线数 (每轮分析一根新K). |
| `direction_gates_mode` | `off`, `dry_run`, `on` | `dry_run` | `on` | --- 方向质量闸门 (direction gates) --- 下单前的程序化方向过滤: G1 限价/突破单入场价贴近最近收盘价(<=2跳, 追价而非回踩), G2 与上一轮诊断方向相反的即时新单. 模式: off = 关闭; dry_run = 只记录不拦截 (默认, 先观察); on = 拒绝并拦截执行与通知. |

## 实盘(Live)一键切换

代码层已按环境解耦：根级 `binance_usdm_environment`（`testnet` / `live`，默认 `testnet`，既有配置行为不变）选择活动环境；REST/WS 网关、运行时状态文件（`trade_records/binance_usdm_live_state.json`）、通知与日志标签全部随环境自动解析，测试网与实盘互不串扰。

实盘切换步骤（全部在 `settings.json` 完成）：

1. 参照 `config/settings.example.json` 把 `binance_usdm_live` 节补全（字段与 `binance_usdm_testnet` 同构），填入主网 API Key/Secret（只开交易权限、禁提现、建议 IP 白名单），保持 `dry_run: true`、`emergency_stop: true`、`enabled: true`。
2. 将 `binance_usdm_environment` 改为 `"live"` 并重启 `pa-monitor`；启动即校验：测试网与实盘两节同时 `enabled`、或 live 节缺密钥时**直接拒绝启动**。
3. 冒烟预检（只读、不下单）：`python tools/probe_binance_env.py --env live`（可选 `--listen-key` 验证 listenKey 生命周期）。
4. 实弹：先 `dry_run: false` 小仓位跑 3-5 单人工盯盘，确认无误再 `emergency_stop: false`。

两节同时 `enabled` 属配置错误；挂单 watcher/保本 guard 记录各自独立存放，不会拿实盘账户去管测试网挂单（反之亦然）。

## 安全提醒

- **不要**将 `config/settings.json`、`config/exception_state.json`、`config/tv_symbol_aliases.json` 提交到 Git。
- 若曾误提交 API Key，请立即在服务商处**作废并轮换**密钥。
- 建议在仓库根目录执行：`powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1`
