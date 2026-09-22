#!/usr/bin/env bash
# 生成/刷新「无图形界面」派生分支 (默认 no-gui)。
#
# 为什么用"生成式"而不是手工维护一条分叉分支: GUI 是叶子模块 —— 除
# pa_agent/main.py 与测试之外没有任何生产代码引用它, 所以每次从 base 全量重建
# 即可, 天然不会与主干产生 rebase 冲突。该分支**永不** merge 回主干; 主干前进后
# 重跑本脚本就刷新 (旧分支被强制指向新生成物)。
#
# 用法:
#   scripts/gen_no_gui_branch.sh                 # base=main
#   scripts/gen_no_gui_branch.sh --base HEAD     # base=当前提交
#   scripts/gen_no_gui_branch.sh --base main --branch no-gui --no-verify
set -euo pipefail

BRANCH=no-gui
BASE=main
RUN_VERIFY=1

usage() { sed -n '2,14p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:?--base 需要一个 ref}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch 需要一个分支名}"; shift 2 ;;
    --no-verify) RUN_VERIFY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
  echo "工作树不干净, 先 commit 或 stash:" >&2
  git status --short >&2
  exit 1
fi

BASE_SHA=$(git rev-parse --verify --quiet "${BASE}^{commit}") || {
  echo "base ref 不存在: $BASE" >&2
  exit 1
}
ORIGINAL=$(git rev-parse --abbrev-ref HEAD)
echo "[1/6] $BRANCH <- $BASE ($BASE_SHA); 原分支 $ORIGINAL"

git switch -C "$BRANCH" "$BASE_SHA" >/dev/null

echo "[2/6] 移除 pa_agent/gui (Qt 实现)"
if [ -d pa_agent/gui ]; then
  git rm -r -q pa_agent/gui
  # 清掉遗留 __pycache__: 目录还在的话 pa_agent.gui 仍会作为 namespace
  # package 被 find_spec 找到, 第 6 步自检就白做了。
  rm -rf pa_agent/gui
else
  echo "  已移除, 跳过"
fi

echo "[3/6] 移除依赖 Qt 的测试 (保留 tests/unit/test_headless_without_qt.py)"
GUI_TESTS=(
  tests/e2e/test_smoke_free_chat.py
  tests/e2e/test_smoke_happy_path.py
  tests/e2e/test_smoke_no_order.py
  tests/e2e/test_smoke_switch_mid_flight.py
  tests/integration/test_next_bar_prediction.py
  tests/integration/test_switch_mid_analysis.py
  tests/property/test_next_bar_prediction_perf.py
  tests/unit/test_chart_decision_overlay.py
  tests/unit/test_chart_fit_view.py
  tests/unit/test_chart_skip_redraw.py
  tests/unit/test_chart_widget_no_lines_when_not_trading.py
  tests/unit/test_debug_widget_masks_key.py
  tests/unit/test_decision_panel.py
  tests/unit/test_future_trend_panel.py
  tests/unit/test_order_opportunity.py
  tests/unit/test_overlay_lines.py
  tests/unit/test_support_resistance_chart.py
  tests/unit/test_token_indicator_thresholds.py
  tests/unit/test_validation_retry.py
)
for f in "${GUI_TESTS[@]}"; do
  if [ -e "$f" ]; then git rm -q "$f"; fi
done

echo "[4/6] 移除 gui-only 死代码"
for f in pa_agent/data/refresh_loop.py tests/unit/test_refresh_loop_warmup.py; do
  if [ -e "$f" ]; then git rm -q "$f"; fi
done

echo "[5/6] 重写无 Qt 入口 + demo 包按需导入"
cat > pa_agent/main.py <<'PY'
"""Application entry point — 无图形界面分支 (no-gui)。

本分支由 scripts/gen_no_gui_branch.sh 生成: pa_agent.gui 在这个分支上不存在,
启动直接进入无头监控。GUI 入口保留在主干分支的 pa_agent/main.py;
本分支永不 merge 回主干。
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

_HELP = "用法: pa-agent   # 无头分支: 前台启动监控, 等同 pa-monitor start --foreground"


def main(argv: list[str] | None = None) -> int:
    # run_monitor() 自带 bootstrap / 配置校验 / PID 文件锁 / SIGINT+SIGTERM 优雅退出,
    # 这里不重写一遍未测的循环。
    from pa_agent.monitoring.cli import run_monitor

    argv = list(sys.argv if argv is None else argv)
    if any(arg in {"-h", "--help"} for arg in argv[1:]):
        print(_HELP)
        return 0
    logger.info("PA Agent 以无头模式启动 (no-gui 分支)")
    return run_monitor()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
PY

cat > pa_agent/demo/__init__.py <<'PY'
"""Demo mode: replay saved analysis records in the UI."""
from pa_agent.demo.record_loader import (
    frame_from_record_klines,
    is_demo_playable,
    list_pending_record_paths,
    load_analysis_record,
    pick_playable_demo_record,
    pick_random_record_path,
    try_load_analysis_record,
)

__all__ = [
    "DemoReplayer",
    "frame_from_record_klines",
    "is_demo_playable",
    "list_pending_record_paths",
    "load_analysis_record",
    "pick_playable_demo_record",
    "pick_random_record_path",
    "try_load_analysis_record",
]


def __getattr__(name: str):
    """DemoReplayer 依赖 Qt, 改按需导入 (no-gui 分支)。

    原先在包顶层 eager import: tools/ 下 9 个脚本都只想要 record_loader, 却会
    顺着包 __init__ 把 PyQt6 拖进来 —— 无 Qt 环境直接挂。
    """
    if name == "DemoReplayer":
        from pa_agent.demo.replayer import DemoReplayer

        return DemoReplayer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
PY

git add -A
if git diff --cached --quiet; then
  echo "  无改动 (已是目标状态), 不新建提交"
else
  git commit -q -m "🔥 chore(gui): 派生无图形界面分支，移除全部 Qt 依赖"
  echo "  已提交 $(git rev-parse --short HEAD)"
fi

if [ "$RUN_VERIFY" = "1" ]; then
  echo "[6/6] 无 Qt 自检 (屏蔽 PyQt6/pyqtgraph/sip 后导入核心模块)"
  PY_BIN="${PYTHON:-}"
  if [ -z "$PY_BIN" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then PY_BIN="$ROOT/.venv/bin/python"; else PY_BIN=python3; fi
  fi
  "$PY_BIN" - <<'PY'
import importlib.util
import sys


class _BlockQt:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"PyQt6", "pyqtgraph", "sip"}:
            raise ImportError("Qt blocked for no-gui branch self-check")
        return None


sys.meta_path.insert(0, _BlockQt())

import pa_agent.main  # noqa: E402
import pa_agent.demo.record_loader  # noqa: E402
from pa_agent.monitoring.cli import run_monitor  # noqa: E402

assert callable(pa_agent.main.main), "pa_agent.main.main 不可调用"
assert callable(run_monitor), "run_monitor 不可调用"
assert importlib.util.find_spec("pa_agent.gui") is None, "pa_agent.gui 仍然存在"
print("NO-GUI-BRANCH-OK")
PY
else
  echo "[6/6] 跳过自检 (--no-verify)"
fi

echo "完成。切换: git switch $BRANCH ; 回原分支: git switch $ORIGINAL"
echo "提示: 该分支永不 merge 回主干; 主干前进后重跑本脚本刷新。"
