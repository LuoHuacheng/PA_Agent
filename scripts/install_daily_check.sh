#!/usr/bin/env bash
# 安装/卸载 用户级 launchd 定时任务: 每 6 小时跑一次正期望日检。
#
#   bash scripts/install_daily_check.sh install     # 安装并加载
#   bash scripts/install_daily_check.sh uninstall   # 卸载并删除
#   bash scripts/install_daily_check.sh status      # 查看状态
#
# ⚠️ 为什么不用 /bin/bash
#   它在只读系统卷(SSV)上, macOS 的 TCC **拒绝**把它登记进"完全磁盘访问权限"
#   (2026-09-18 实测: 点选添加后 TCC.db 里根本没有 bash 条目), 于是一旦仓库在
#   ~/Documents 下, launchd 拉起的 bash 连脚本都读不了 -> exit 126。
#   解法: 复制一份 bash 到**用户路径**, 给这份副本授权:
#       ~/.local/bin/pa-daily-bash
#
# 需要写 ~/Library/LaunchAgents 与 launchd 域, 请在普通终端(非受限沙箱)里执行。
set -euo pipefail

LABEL="com.pa-agent.expectancy-daily"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO/scripts/launchd/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
BASH_COPY="$HOME/.local/bin/pa-daily-bash"
ACTION="${1:-install}"

case "$ACTION" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs" "$(dirname "$BASH_COPY")"
    if [ ! -x "$BASH_COPY" ] || ! cmp -s /bin/bash "$BASH_COPY"; then
      cp /bin/bash "$BASH_COPY"
      chmod 755 "$BASH_COPY"
      echo "已复制 bash -> $BASH_COPY"
    fi
    sed -e "s|__REPO__|$REPO|g" -e "s|__BASH__|$BASH_COPY|g" "$TEMPLATE" > "$TARGET"
    plutil -lint "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "installed: $TARGET"
    echo
    echo "!! 还需手动授权一次(否则 launchd 读不了 $REPO):"
    echo "   系统设置 -> 隐私与安全性 -> 完全磁盘访问权限 -> +"
    echo "   添加这个文件并打开开关: $BASH_COPY"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "uninstalled: $TARGET"
    echo "(保留 $BASH_COPY; 想彻底清掉可 rm 它, 并从完全磁盘访问权限里移除)"
    ;;
  status)
    echo "--- launchctl ---"
    launchctl print "$DOMAIN/$LABEL" 2>&1 | grep -E 'state =|runs =|last exit code|program =' || true
    echo "--- 授权目标 ---"
    ls -l "$BASH_COPY" 2>&1 || echo "(尚未创建)"
    echo "--- 最近一次 err ---"
    tail -3 "$REPO/logs/daily_check.err" 2>/dev/null || echo "(无)"
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}" >&2
    exit 2
    ;;
esac
