#!/bin/sh
# PA_Agent 无头部署：装依赖 + 生成运行态配置。可重复执行。
#
#   sh scripts/install.sh
#
# 只装 pyproject 主依赖，不装 gui extra（PyQt6/pyqtgraph）—— 服务器不需要 Qt。
# 运行态目录（config/logs/records/experience）全部落在仓库根下，见
# pa_agent/config/paths.py:12 PROJECT_ROOT。
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR=${VENV_DIR:-$ROOT/.venv}

cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
  echo "==> uv sync（无 gui extra，跳过 Qt）"
  uv sync
else
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "ERROR: 需要 python3 或 uv" >&2
    exit 1
  }
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "==> 创建 venv: $VENV_DIR"
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
      echo "ERROR: 创建 venv 失败。Ubuntu/Debian 需先装 python3-venv：" >&2
      echo "       sudo apt update && sudo apt install -y python3-venv python3-pip git" >&2
      exit 1
    fi
  fi
  echo "==> pip install -e ."
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -e .
fi

# settings.json 被 .gitignore 排除；缺省时从 example 复制。默认值全部安全：
# monitoring.enabled=false / binance_usdm_testnet.enabled=false / dry_run=true。
if [ ! -f "$ROOT/config/settings.json" ]; then
  cp "$ROOT/config/settings.example.json" "$ROOT/config/settings.json"
  echo "==> 已生成 config/settings.json（默认全部关闭，需手工填 provider 与交易配置）"
fi
chmod 600 "$ROOT/config/settings.json"

mkdir -p "$ROOT/logs" "$ROOT/records" "$ROOT/experience"

echo "==> 校验导入"
"$VENV_DIR/bin/python" - <<'PY'
from pa_agent.monitoring.cli import main  # noqa: F401
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator  # noqa: F401
from pa_agent.util.qt_compat import HAVE_QT

print(f"    imports OK, HAVE_QT={HAVE_QT}")
PY

cat <<EOF

PA_Agent 依赖已安装。
下一步：
  1. 编辑 $ROOT/config/settings.json：
       provider   —— 模型 / base_url / api_key（必须改成服务器可达的网关）
       general    —— 提示词与 K 线数量
       monitoring —— enabled=true 才会跑；targets 为空会拒绝启动
       binance_usdm_testnet —— 先用 testnet + dry_run=true 跑通
  2. 前置检查：$VENV_DIR/bin/pa-monitor doctor
  3. 前台试跑：$VENV_DIR/bin/pa-monitor start --foreground
     （后台模式 start_new_session 会让 systemd 认为进程已退出，服务里必须 --foreground）
  4. 注册服务：见 deploy/pa-agent.service

注意：
  - config/settings.json 含明文凭据，保持 600 权限，勿提交。
  - tvdatafeed 需 websocket-client>=1.8 且 tvdatafeed 的 __ws_headers 已改为 dict，
    否则 TradingView 握手 100% 失败（见 docs/MAC版部署方法.txt 问题 3.5）。
EOF
