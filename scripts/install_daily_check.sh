#!/usr/bin/env bash
# 安装/卸载 用户级 launchd 定时任务: 每 6 小时跑一次正期望日检。
#
#   bash scripts/install_daily_check.sh install     # 安装并加载
#   bash scripts/install_daily_check.sh uninstall   # 卸载并删除
#   bash scripts/install_daily_check.sh status      # 查看状态
#
# 为什么任务跑 Python 而不是 shell
#   仓库在 ~/Documents 下, 受 macOS TCC 保护。三条死路(2026-09-18 实测):
#     1) launchd 拉起 /bin/bash -> 读不了仓库, exit 126 "Operation not permitted";
#     2) 给 /bin/bash 授"完全磁盘访问" -> 登记不上(它在只读系统卷 SSV 上, 系统 TCC 库里没有 bash 条目);
#     3) 复制一份 bash 到用户路径 -> 副本签名失效, Killed: 9, OS_REASON_CODESIGNING。
#   可行解: 用用户路径上的 Python(uv 管理, 无 quarantine) 直接执行 scripts/daily_check.py,
#   再给它授"完全磁盘访问权限"。
#
# 需要写 ~/Library/LaunchAgents 与 launchd 域, 请在普通终端(非受限沙箱)里执行。
set -euo pipefail

LABEL="com.pa-agent.expectancy-daily"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/scripts/launchd/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
STALE_COPY="$HOME/.local/bin/pa-daily-bash"
ACTION="${1:-install}"

# 解析要跑日检的 Python: 优先环境变量, 其次 .venv, 最后 PATH 里的 python3
resolve_py() {
  if [ -n "${PA_DAILY_PY:-}" ]; then echo "${PA_DAILY_PY}"; return; fi
  if [ -x "$REPO/.venv/bin/python" ]; then
    # 必须原样用 .venv/bin/python, 不能 realpath: 那会解析成 uv 的裸解释器,
    # 它的 site-packages 里没有 pa_agent 的依赖。2026-09-20 实测: 日检第 5/6 行
    # 报 ModuleNotFoundError: No module named 'pydantic' 与 No module named 'pa_agent'
    # —— 解释器对了、环境丢了。
    echo "$REPO/.venv/bin/python"
    return
  fi
  command -v python3 || echo /usr/bin/python3
}

PY="$(resolve_py)"

case "$ACTION" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" "$REPO/logs"
    sed -e "s|__REPO__|$REPO|g" -e "s|__PY__|$PY|g" -e "s|__HOME__|$HOME|g" \
      "$TEMPLATE" > "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
    if [ -e "$STALE_COPY" ]; then
      rm -f "$STALE_COPY"
      echo "已删除失效的 bash 副本: $STALE_COPY (可在完全磁盘访问权限里移除它)"
    fi
    echo "installed: $TARGET"
    echo "interpreter: $PY"
    echo
    echo "!! 还需手动授权一次(否则 launchd 读不了 $REPO):"
    echo "   系统设置 -> 隐私与安全性 -> 完全磁盘访问权限 -> +"
    echo "   添加并打开开关: $PY"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "uninstalled: $TARGET"
    ;;
  status)
    echo "--- launchctl ---"
    launchctl print "$DOMAIN/$LABEL" 2>&1 | grep -E 'state =|runs =|last exit reason|last exit code|program =' || true
    echo "--- 解释器 ---"
    echo "interp = $PY"
    echo "--- 最近一次 err ---"
    tail -3 "$HOME/Library/Logs/pa-agent-daily.err" 2>/dev/null \
      || tail -3 "$REPO/logs/daily_check.err" 2>/dev/null || echo "(无)"
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}" >&2
    exit 2
    ;;
esac
