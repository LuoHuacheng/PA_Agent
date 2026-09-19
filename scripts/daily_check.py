"""会话外日检: 与 scripts/daily_check.sh 等价, 但由 Python 直跑(不经 bash).

为什么需要这个入口
------------------
launchd 拉起的 /bin/bash 读不了 ~/Documents(TCC 保护), 报 exit 126
"Operation not permitted"; 而给 /bin/bash 授"完全磁盘访问"**登记不上**——它在只读
系统卷(SSV)上, 2026-09-18 实测系统 TCC 库里没有 bash 条目。复制一份 bash 也不行:
副本签名失效, 内核 Killed: 9, launchd 报 last exit reason = OS_REASON_CODESIGNING。

用户路径上的 Python(uv 管理) 没有这些问题, 且可以被授予完全磁盘访问权限。
所以定时任务改跑本脚本。

用法:
    /Users/hc/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 \
        scripts/daily_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = REPO / "logs"
TSV = LOG_DIR / "daily_check.tsv"


def _run(args):
    """跑一个子命令, 返回其输出行(制表符已换成空格)."""
    proc = subprocess.run([sys.executable, *args], cwd=REPO,
                          capture_output=True, text=True)
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    return [ln.replace("\t", " ") for ln in lines]


def _first(lines, default="(无输出)"):
    return lines[0] if lines else default


def _last(lines, default="(无输出)"):
    return lines[-1] if lines else default


def orders_count():
    d = REPO / "records" / "orders"
    try:
        return len(list(d.iterdir()))
    except OSError:
        return 0


def service_alive():
    """用信号探测监控进程; ps 在受限环境里不可用."""
    try:
        pid = int(json.loads((REPO / "config" / "monitoring.pid").read_text(encoding="utf-8"))["pid"])
        os.kill(pid, 0)
        return "yes"
    except Exception:
        return "no"


def fill_line():
    """限价挂单成交率: 单边行情里它飙低就是"策略天然空仓"的报警."""
    try:
        from pa_agent.monitoring.fill_stats import STATE_KEY, summarize

        raw = json.loads((REPO / "config" / "monitoring_state.json").read_text(encoding="utf-8"))
        return summarize(raw.get(STATE_KEY, {}))
    except Exception as exc:  # 日检绝不因遥测缺失而失败
        return f"fill_stats: 不可读 ({exc})"


def main():
    all_line = _first(_run(["tools/expectancy_report.py"]))
    post = _first(_run(["tools/expectancy_report.py", "--gate-since"]))
    lossb = _last(_run(["tools/expectancy_report.py", "--gate-since", "--loss-bound", "2"]))
    rollback = _last(_run(["tools/expectancy_report.py", "--rollback-check"]))
    gate = _last(_run(["scripts/verify_gate.py"]))
    fill = fill_line()
    orders = orders_count()
    service = service_alive()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = "\t".join([
        ts, f"全部: {all_line}", f"上线后: {post}", f"路B: {lossb}",
        f"回滚: {rollback}", f"闸门: {gate}", f"成交率: {fill}",
        f"orders={orders}", f"service={service}",
    ])
    LOG_DIR.mkdir(exist_ok=True)
    with open(TSV, "a", encoding="utf-8") as fh:
        fh.write(row + "\n")

    print(f"--- 日检 ({ts}) ---")
    print(f"1) 全部历史: {all_line}")
    print(f"2) 上线后  : {post}      <- 看 n 的进度")
    print(f"3) 路B判定 : {lossb}      <- 现行判定目标")
    print(f"4) 回滚检查: {rollback}")
    print(f"5) 闸门    : {gate}")
    print(f"6) 成交率  : {fill}   <- 单边行情天然空仓报警")
    print(f"7) 账本条目: {orders}  (必须 > 0)   服务存活: {service}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
