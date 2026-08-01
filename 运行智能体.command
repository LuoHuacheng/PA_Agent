#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "[错误] 未找到项目虚拟环境。请在此目录执行：uv sync --extra dev"
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

exec ".venv/bin/python" run.py
