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

---

**免责声明**：本工具仅供学习与研究，不构成投资建议。交易有风险，决策后果自负。

本项目采用 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 发布。
