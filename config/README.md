# 本地配置说明

本目录下的**运行时文件**默认已被 `.gitignore` 忽略，不会进入 Git 仓库。

仓库同样**不会上传**：`records/`（分析落盘）、`experience/`（经验库内容）、`logs/`、`trade_records/`（交易 CSV/截图）、`.env`、根目录临时图片与个人笔记等。仅源代码、`prompt_engineering/` 策略文本、`tests/` 与 `docs/` 说明文档会进入 GitHub。

## 首次使用

1. 复制模板为本地配置：

   ```cmd
   copy config\settings.example.json config\settings.json
   ```

2. 启动程序，在 **设置** 中填写你的 **API Key**（会加密写入 `api_key_encrypted`）。

   也可直接编辑 `config/settings.json` 中的 `base_url`、`model` 等字段，Key 仍建议通过 GUI 保存以便自动加密。

3. `config/exception_state.json` 由程序在需要时自动创建，一般无需手动复制。结构可参考 `exception_state.example.json`。

4. 如需自定义 TradingView 品种别名，复制模板：

   ```cmd
   copy config\tv_symbol_aliases.example.json config\tv_symbol_aliases.json
   ```

## `settings.json` 字段说明

配置包括 `provider`、`general`、`prompt`、`validation`，以及可选的 `binance_usdm_testnet` 和 `monitoring`。

### provider — AI 提供商

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider.model` | string | `"deepseek-v4-flash"` | 模型名称（须与网关支持的名称一致） |
| `provider.base_url` | string | `"https://api.deepseek.com"` | OpenAI 兼容 API 根地址。DeepSeek：`https://api.deepseek.com`；MiMo：`https://api.xiaomimimo.com/v1`（程序自动处理 `enable_thinking` 与 `reasoning_content` 回放） |
| `provider.api_key` | string | `""` | API Key（明文，内存中临时使用；不持久化到文件） |
| `provider.api_key_encrypted` | string | `""` | 加密后的 Key；留空表示未配置（通过 GUI 保存时自动加密写入） |
| `provider.thinking` | bool | `true` | 是否启用思考/推理类扩展参数（依模型与网关而定）。关闭可 3–5 倍提速但分析质量下降 |
| `provider.reasoning_effort` | string | `"high"` | 推理深度：`low` / `medium` / `high` / `max` |
| `provider.context_window` | int | `2000000` | 用于上下文占用提示的窗口大小（tokens） |

### general — 通用设置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `general.last_data_source` | string | `"mt5"` | K 线数据来源：`mt5` / `tradingview`（GUI 下拉选项）；`akshare` / `yfinance`（仅代码支持） |
| `general.last_tradingview_exchange` | string | `""` | TradingView 交易所。空字符串 =（自动）依次探测预设列表。如 `OANDA`、`SSE`、`HKEX` 等 |
| `general.last_symbol` | string | `"XAUUSDm"` | 默认品种。MT5 需含后缀（如 `m`），TradingView 用标准名（如 `XAUUSD`） |
| `general.last_timeframe` | string | `"15m"` | 默认周期，如 `1m`、`5m`、`15m`、`1h`、`4h`、`1d` |
| `general.analysis_bar_count` | int | `100` | 提交分析时使用的 K 线数量（2–5000） |
| `general.refresh_interval_ms` | int | `1000` | 图表自动刷新间隔（毫秒） |
| `general.context_warning_threshold_pct` | float | `80.0` | 上下文占用警告阈值（百分比） |
| `general.decision_stance` | string | `"balanced"` | 阶段二交易倾向：`conservative` / `balanced` / `aggressive` / `extreme_aggressive` |
| `general.incremental_max_new_bars` | int | `10` | 增量分析触发阈值：新增已收盘 K 线 ≤ 此值时自动走增量模式（0–500） |
| `general.auto_resume_chart_after_analysis` | bool | `false` | 分析结束后是否自动恢复「图表实时更新」 |
| `general.keep_analysis` | bool | `false` | 持续跟踪分析：新 K 线收盘时自动触发新一轮分析 |
| `general.cancel_keep_analysis_on_retry` | bool | `false` | 校验失败触发重试后自动关闭 `keep_analysis` |
| `general.alert_on_order_opportunity` | bool | `true` | 阶段二给出交易方案时播放警报音、弹窗提示，并自动切换到「决策」页 |
| `general.decision_flow_auto_play` | bool | `true` | 决策树可视化自动播放 |
| `general.decision_flow_play_seconds` | int | `50` | 决策树可视化自动播放时长（秒） |
| `general.decision_flow_default_zoom_pct` | int | `600` | 决策树可视化默认缩放百分比（≥10） |
| `general.stream_pane_font_pt` | int | `11` | 「实时」页等宽字体字号（pt，8–28） |
| `general.chart_seq_label_font_pt` | int | `11` | K 线图上序号标签的字号（pt，6–24） |

### monitoring — 多品种周期预警

不提供 GUI 管理。直接编辑 `settings.json` 后重启程序生效。每个目标在自身 K 线自然收盘、再等待短暂数据延迟后拉取一次行情，不会按秒轮询。目标共用当前 `general.last_data_source` 和 `general.last_tradingview_exchange`，但使用独立订阅，互不覆盖。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `monitoring.enabled` | `false` | 总开关。关闭时不创建后台监控任务 |
| `monitoring.targets` | `[]` | 监控列表。每项为 `symbol`、`timeframe`、`enabled`，如 `{"symbol":"XAUUSD","timeframe":"15m","enabled":true}` |
| `monitoring.max_concurrent_analyses` | `1` | 同时执行的 AI 分析上限（1–8）。提高会增加 API 并发与 token 消耗 |
| `monitoring.poll_lead_seconds` | `5` | K 线收盘后等待数据源落库的秒数（0–120） |
| `monitoring.poll_retry_attempts` | `3` | 数据未更新或临时失败时的重试次数（0–10） |
| `monitoring.poll_retry_seconds` | `5` | 重试之间的等待秒数（1–120） |

后台分析沿用当前交易机会和 `general.decision_confidence_threshold` 判定。同一品种、周期、已收盘 K 线只会处理一次，状态存于本地忽略文件 `config/monitoring_state.json`。监控只发送飞书/PushPlus 通知，不会触发 Binance Testnet 自动执行。

### prompt — Prompt 组装调优

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt.stage2_load_full_strategy_library` | bool | `false` | 阶段二是否加载全部 22 个策略文件（通常仅路由匹配的策略文件） |
| `prompt.experience_max_entries` | int | `3` | 经验库最大加载条目数（0–10） |
| `prompt.experience_max_chars_per_entry` | int | `400` | 每条经验最大字符数（100–4000） |
| `prompt.stage1_inject_pattern_briefs` | bool | `true` | 阶段一是否注入模式判定表和速查 brief（减少 missed tags） |

### validation — 校验与重试

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `validation.normalization_mode` | string | `"lenient"` | 归一化模式：`strict`（严格拒绝异常值）/ `lenient`（容忍轻微偏差） |
| `validation.stage1_coherence_checks` | bool | `false` | 阶段一跨字段一致性检查（闸门 trace、逐 K 摘要、模式标签等） |
| `validation.stage2_coherence_checks` | bool | `false` | 阶段二诊断与 trace 交叉检查 |
| `validation.trace_semantic_checks` | bool | `false` | 语义一致性检查（方向/信号逻辑冲突检测） |
| `validation.strict_bar_by_bar_features` | bool | `false` | 严格逐 K 特征校验（开启后对特征字段做严格验证） |
| `validation.disable_truncation_repair` | bool | `false` | 禁用流式 JSON 截断尾部修复 |
| `validation.retry_enabled` | bool | `true` | 校验失败时是否自动重试 |
| `validation.retry_max` | int | `3` | 格式错误（category a）最大重试次数（0–5） |
| `validation.retry_max_semantic` | int | `1` | 语义错误（category c）最大重试次数（0–3） |
| `validation.retry_stage2` | bool | `true` | 阶段二校验失败时是否重试 |

## 安全提醒

### binance_usdm_testnet — Binance U 本位 Testnet 自动执行

API Key 与 Secret Key 保存在本机、已被 Git 忽略的 `config/settings.json` 中。请勿将此文件发送、上传或提交到 Git。

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `false` | 自动执行总开关 |
| `api_key` | `""` | Binance Demo Trading API Key |
| `api_secret` | `""` | Binance Demo Trading Secret Key |
| `dry_run` | `true` | 为 `true` 时不发送任何 Binance 请求 |
| `emergency_stop` | `true` | 熔断开关，必须设为 `false` 才可能执行 |
| `require_analysis_symbol_match` | `true` | 分析品种必须和交易品种完全一致 |
| `symbol` | `"BTCUSDT"` | 仅允许交易的 U 本位 Testnet 品种 |
| `symbol_whitelist` | `["BTCUSDT"]` | 允许交易品种白名单 |
| `leverage` | `1` | 逐品种设定杠杆，范围 1–5 |
| `max_notional_usdt` | `20.0` | 单笔最大名义价值，范围 0–1000 USDT |
| `cooldown_minutes` | `30` | 相同计划的去重冷却期 |

当前仅自动处理带完整 TP/SL 的 `市价单`，限价单和突破单只保留原有提醒。执行前校验 Testnet 品种规则、数量步长、最小名义价值、单向持仓模式和保护价方向；入场后任一保护单创建失败，立即尝试市价平仓。无实盘 URL 或实盘开关。

- **不要**将 `config/settings.json`、`config/exception_state.json`、`config/tv_symbol_aliases.json` 提交到 Git。
- 若曾误提交 API Key，请立即在服务商处**作废并轮换**密钥。
- 建议在仓库根目录执行：`powershell -ExecutionPolicy Bypass -File tools\setup_git_secrets.ps1`
