#!/bin/bash
# PA Agent 多标的 K 线收盘后台监控（无头）
# 用法：双击运行，或 source 后按提示；Ctrl+C 停止。
set -euo pipefail
cd "$(dirname "$0")"

# 安全：Testnet 凭据从环境变量读取；未设置则只发通知、不下单。
if [ -z "${BINANCE_USDM_TESTNET_API_KEY:-}" ] || [ -z "${BINANCE_USDM_TESTNET_API_SECRET:-}" ]; then
  echo "[提示] 未设置 Testnet 凭据，监控将只发通知、不自动下单。"
  echo "       如需自动下单，请先 export 两个环境变量后再运行。"
  read -r -p "按回车键关闭窗口..." 
  exit 0
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[错误] 未找到项目虚拟环境。请在此目录执行：uv sync --extra dev"
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

echo "[PA Agent 监控已启动] 目标见 config/settings.json → monitoring.targets"
exec ".venv/bin/python" run.py --monitor
