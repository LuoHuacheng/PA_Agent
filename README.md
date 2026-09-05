# PA Agent — AI K线分析辅助工具（桌面端）

---

面向主观交易者的 **价格行为（Price Action）** AI 辅助决策工具。从 **MT5 / TradingView / yfinance / AkShare** 读取 K 线，将结构化 K 线数据与预计算特征送入大模型做**两阶段分析**（市场诊断 → 交易决策），不是截图识图。默认不连接交易所、不执行下单；可显式开启受限的 Binance U 本位 Futures Testnet 市价自动执行。

---

## 主要功能

- 📈 **多数据源**：MT5（Windows）、TradingView（全平台）、yfinance（期货/加密货币）、AkShare（A 股）
- 🧠 **两阶段 AI 分析**：市场诊断 → 策略路由 → 交易决策（限价/突破/市价或不下单）
- 🔄 **增量分析与持续跟踪**：新增 K 线时复用上次结论；开启 `keep_analysis` 后新 K 线收盘自动触发新一轮分析
- 🌳 **决策树可视化**：赛博科幻风格可交互流程图，自动播放闸门→策略路径动画
- 🔮 **未来走势预期**：AI 预测下一根 K 线方向和下一个市场周期位置
- 💬 **分析后自由追问**：完整对话会话管理器，实时推理流 + Token 进度条，对话历史持久化
- 📚 **经验库**：按周期位置检索历史案例供分析参考
- 📝 **完整落盘**：Prompt、原始响应、诊断/决策 JSON、Token 用量、追问记录
- 🛡️ **可配置校验体系**：JSON 校验、一致性检查、语义校验、截断修复、失败自动重试
- 🔒 **API Key** 本地加密存储
- 🧪 **可选 Testnet 自动交易**：仅 Binance U 本位 Testnet、市价单/限价单信号（突破单需人工复核），默认熔断和 dry-run；入场成功后强制创建止损与止盈保护单

---

## 环境要求

| 项目     | 要求                                                       |
| -------- | ---------------------------------------------------------- |
| 操作系统 | Windows 10 / 11（主支持）、macOS 12+（TradingView 数据源） |
| Python   | 3.11+                                                      |
| 数据源   | MT5 / TradingView / yfinance / AkShare **至少配置一种**    |
| 网络     | 可访问所配置的 AI API（如 DeepSeek、PackyAPI 等）          |

---

## 快速开始

直接在系统中安装（推荐部署在本机）：

```cmd
pip install -e .
# 启动图形界面（两种方式等价，任选其一）
pa-agent
python -m pa_agent.main
```

首次启动后在**设置**中填写 **Base URL**、**模型名** 与 **API Key**。

> 如需隔离环境也可创建虚拟环境：`python -m venv .venv` 后激活再 `pip install -e .`。

**安装内容**：PyQt6（GUI 框架）+ pyqtgraph（K 线图表绘图）+ numpy/pandas（数据处理）+ openai（AI API 客户端）+ **akshare/baostock（A 股数据源）** + json 校验、模型定义等全套依赖。

> 若需运行测试（pytest）或代码格式化（ruff/black），额外安装：`pip install -e ".[dev]"`。

### uv 隔离环境（可选）

项目也支持使用 [uv](https://docs.astral.sh/uv/) 进行环境隔离，依赖版本通过 `uv.lock` 锁定，保证可复现安装，且不会污染系统 Python。

```cmd
# 1. 安装 uv（仅需一次）
pip install uv
# 或官方脚本：curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 首次运行或依赖变更时，make 自动创建 .venv 并同步依赖
make uv-run

# 3. 之后每次启动
make uv-run
# 或手动：uv run python -m pa_agent.main
```

> 运行测试：`make uv-test`，代码检查：`make uv-lint`。

---

## 详细说明

完整操作界面说明见 [`PA_Agent使用文档.md`](PA_Agent使用文档.md)，配置字段说明见 [`config/README.md`](config/README.md)。

### Binance U 本位 Futures Testnet 自动交易

默认关闭。此功能不支持实盘 URL，自动处理 `市价单` 与 `限价单`（`limit_order_enabled` 可关限价；`突破单` 需人工复核）。在 `config/settings.json` 的 `binance_usdm_testnet` 中，确认：

1. `enabled: true`，`emergency_stop: false`，先保持 `dry_run: true` 验证流程。
2. 分析品种与 `symbol` 完全一致，且在 `symbol_whitelist` 内。
3. 仅完成 dry-run 验证后，才将 `dry_run` 改为 `false`。

密钥保存在本机、被 Git 忽略的 `config/settings.json` 的 `binance_usdm_testnet.api_key` 与 `api_secret` 中，绝不写入日志。不要分享、上传或提交该文件。API Key 应只启用交易权限，禁止提现。自动执行使用单向持仓模式；单笔名义价值受 `max_notional_usdt` 限制，杠杆上限 20 倍（`leverage` 可配置，默认 20）。

### 无头监控（headless monitor，`pa-monitor`）

不打开图形界面、按 K 线收盘自动运行的监控模式，直接执行 `pa-monitor start`：

```cmd
pa-monitor start      # 启动监控
pa-monitor status     # 查看运行状态
pa-monitor stop       # 停止（优雅关停，超时兜底强退）
pa-monitor pnl        # 每日实现盈亏只读统计
pa-monitor pnl --days 30 --csv pnl.csv   # 自定义天数 / 导出 CSV
```

监控品种来自 `config/settings.json` → `monitoring.targets`（静态）或 `auto_discover`（按成交额自动选 Binance U 本位 Top N）。每个 K 线收盘时点自动拉取数据并做两阶段分析：信号达标（类型为市价/限价/突破单且置信度 ≥ `decision_confidence_threshold`）时推送 Telegram 通知，并在启用时于 Binance U 本位 Testnet 自动下单（限价单含止损止盈保护）。日志见 `logs/pa_agent.log`，数据源为 TradingView（可在 `general` 配置登录凭据缓解匿名限流）。

### 复盘分析 CLI 工具（`tools/`）

针对 Testnet 自动交易结果的**只读**复盘工具：用 `config/settings.json` 中 Binance U 本位 Testnet 的 API Key/Secret 拉取账户订单与成交，再与落盘的 pa-entry 决策信号配对重建交易；**不创建任何订单、不修改任何交易状态**。需先在 `binance_usdm_testnet` 配置好密钥（见上文），并在仓库根目录、项目 Python 环境（.venv 激活或已 `pip install -e .`）下运行。`tools/_pa_sim_common.py` 是两者共用的取数与配对逻辑（不应直接运行）。

#### 逐笔盈亏报告 `tools/trade_pnl_report.py`

按 pa-entry 信号重建每笔交易（按成交 LIFO 配对），附上决策置信度，输出按 **置信度分段 / 开仓日 / 方向 / 币种** 的盈亏统计（已平净盈亏、手续费、浮盈、胜率等），并与账户 income 流水（已实现盈亏 + 手续费 + 资金费）对账，另汇总入场订单状态分布：

```cmd
python tools/trade_pnl_report.py                          # 最近 5 天，置信度分界 55
python tools/trade_pnl_report.py --days 14 --symbols ETHUSDT,ZECUSDT
python tools/trade_pnl_report.py --conf-cut 60 --detail --out report.json
```

| 参数 | 默认 | 说明 |
| ---- | ---- | ---- |
| `--days` | 5 | 分析窗口（本地自然日） |
| `--symbols` | BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ZECUSDT | 逗号分隔的币种列表 |
| `--conf-cut` | 55 | 置信度分界：输出 ≥cut、<cut、无信号（手动/外部）与全部 4 组统计 |
| `--detail` | 关 | 逐笔打印交易明细 |
| `--out` | 无 | 分组/按日/按币种统计写入 JSON 文件 |

#### 止损规则模拟 `tools/sim_trailing_stop.py`

将每笔**已平仓**交易（由成交重建并匹配到带止损/止盈的决策）在真实 K 线上逐 bar 回放，对比「入场即挂固定止损/止盈、持有到触发」的基线，与下列保护规则的实际净值差异（R = 初始风险 `|entry − stop|`）：

| 规则 | 含义 |
| ---- | ---- |
| `be05r` / `be1r` | 浮盈达 0.5R / 1R 后止损移至成本价（保本） |
| `trail05r` / `trail1r` | 止损随最高浮盈回撤 0.5R / 1R 上移（追踪止损） |
| `be_tp` | 行情触达止盈价位后将止损移至成本价 |

```cmd
python tools/sim_trailing_stop.py                 # 最近 5 天、默认全部规则
python tools/sim_trailing_stop.py --days 14 --rules be1r,be_tp
python tools/sim_trailing_stop.py --interval 5m --out result.json
```

| 参数 | 默认 | 说明 |
| ---- | ---- | ---- |
| `--days` | 5 | 回放窗口（本地自然日） |
| `--symbols` | BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ZECUSDT | 逗号分隔的币种列表 |
| `--interval` | 1m | K 线周期：`1m` / `5m` / `15m` |
| `--rules` | be05r,be1r,trail05r,trail1r,be_tp | 待模拟规则（逗号分隔） |
| `--big-win-usdt` | 8.0 | 「大赢家」判定阈值：实际净盈亏超过该 USDT 数的交易 |
| `--detail` | 关 | 打印逐笔与基线差异的分布（前 12 笔） |
| `--out` | 无 | 逐规则汇总写入 JSON 文件 |

输出表格列：`rule` 规则、`net` 模拟净盈亏、`tp` / `sl` 止盈/止损触发笔数、`miss` 无法回放笔数（实际平仓并非静态 SL/TP 触发）、`kept` 模拟后仍盈利的大赢家笔数、`vs-actual` 相对基线实际净值的差额。

---

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。

本项目采用 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 发布。
