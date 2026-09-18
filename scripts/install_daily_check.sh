#!/usr/bin/env bash
# 安装/卸载 用户级 launchd 定时任务: 每 6 小时跑一次正期望日检。
#
#   bash scripts/install_daily_check.sh install     # 安装并加载
#   bash scripts/install_daily_check.sh uninstall   # 卸载并删除
#   bash scripts/install_daily_check.sh status      # 查看状态
#
# 需要写 ~/Library/LaunchAgents, 请在普通终端(非受限沙箱)里执行。
set -euo pipefail

LABEL="com.pa-agent.expectancy-daily"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/scripts/launchd/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
ACTION="${1:-install}"

case "$ACTION" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
    sed "s|__REPO__|$REPO|g" "$TEMPLATE" > "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "installed: $TARGET"
    launchctl print "$DOMAIN/$LABEL" | grep -E "state|program|path" | head -5 || true
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "uninstalled: $TARGET"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>&1 | head -20 || true
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}" >&2
    exit 2
    ;;
esac
