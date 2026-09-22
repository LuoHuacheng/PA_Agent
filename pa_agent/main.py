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
