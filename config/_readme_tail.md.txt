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
