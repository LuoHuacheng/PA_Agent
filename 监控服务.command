#!/bin/bash
# PA Agent 多标的 K 线收盘后台监控（无头）
# 用法：双击运行，或 source 后按提示；Ctrl+C 停止。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[错误] 未找到项目虚拟环境。请在此目录执行：uv sync --extra dev"
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

echo "[PA Agent 监控已启动] 目标见 config/settings.json → monitoring.targets"
exec ".venv/bin/python" run.py --monitor
