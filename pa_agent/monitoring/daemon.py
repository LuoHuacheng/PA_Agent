"""把监控进程从当前终端剥离, 并确认它真的起来了.

为什么是 subprocess 重新 exec 而不是 double-fork:

- 跨平台一致 -- Windows 没有 fork, 只能用 DETACHED_PROCESS;
- 子进程是直接 exec 出来的, 没有中间层, config/monitoring.pid 里记的就是
  真实工作进程的 PID, stop/status 无需做 PID 换算.

POSIX 下用 start_new_session=True 另开 session: 进程不再持有控制终端,
关掉终端不再收到 SIGHUP, 这是 "关掉 terminal, 服务继续跑" 的关键.

子进程的 stdout/stderr 追加写入 log_path (logs/monitor.out), configure_logging
的控制台 handler / 裸 print / 未捕获 traceback 都因此留痕; 重定向后 isatty()
为假, 控制台 handler 自动不带 ANSI 色码.

就绪判定: 等到 PID 文件里出现子进程自己的 PID, 且子进程仍存活. 子进程若在就绪前
退出 (配置非法 / 双开 / monitoring.enabled=false), 把这一段新增输出回显到父终端
并透传退出码 -- 启动期错误不能被静默吞掉.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from pa_agent.config.paths import PROJECT_ROOT

#: 真实启动要先过 AppContext + 设置校验 + Binance 环境校验, 给足冷启动时间.
READY_TIMEOUT_SECONDS = 15.0
_POLL_SECONDS = 0.1
#: 回显失败输出时的上限, 避免把整份 monitor.out 倒进终端.
_TAIL_BYTES = 4000


def _pid_file_records(pid_path: Path, pid: int) -> bool:
    """PID 文件里是否已记录该 PID (复用 cli 里唯一的 PID 文件读取实现)."""
    from pa_agent.monitoring.cli import _read_pid

    return _read_pid(pid_path) == pid


def _echo_new_log_bytes(log_path: Path, start_offset: int) -> None:
    """回显本次启动新增的日志尾部, 让父终端直接看到失败原因."""
    try:
        with open(log_path, "rb") as handle:
            handle.seek(start_offset)
            data = handle.read()[-_TAIL_BYTES:]
    except OSError:
        return
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return
    print(f"--- {log_path} 输出 ---", file=sys.stderr)
    print(text, file=sys.stderr)


def spawn_background(
    worker_argv: Sequence[str],
    *,
    log_path: Path,
    pid_path: Path,
    ready_timeout: float = READY_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> int:
    """在后台拉起 worker: 就绪返回 0, 否则返回非 0 并说明原因.

    cwd 默认固定到项目根: _is_monitor_process 对 -m 入口要求进程 cwd 与项目根
    一致, 否则 pa-monitor status/stop 会认不出这个进程.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start_offset = log_path.stat().st_size if log_path.exists() else 0

    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "cwd": str(cwd or PROJECT_ROOT),
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    with open(log_path, "ab") as log_file:
        popen_kwargs["stdout"] = log_file
        proc = subprocess.Popen(list(worker_argv), **popen_kwargs)

        deadline = time.monotonic() + max(ready_timeout, 0.0)
        while True:
            exit_code = proc.poll()
            if exit_code is not None:
                print(
                    f"[后台启动失败] 子进程已退出, 退出码={exit_code}",
                    file=sys.stderr,
                )
                _echo_new_log_bytes(log_path, start_offset)
                return exit_code if exit_code > 0 else 1
            if _pid_file_records(pid_path, proc.pid):
                print(f"[已后台启动] PID={proc.pid} 日志: {log_path}")
                return 0
            if time.monotonic() >= deadline:
                print(
                    f"[后台启动未确认] {ready_timeout:.0f}s 内未就绪, 进程仍在启动中; "
                    f"PID={proc.pid}. 请用 pa-monitor status 复核, 日志: {log_path}",
                    file=sys.stderr,
                )
                return 1
            time.sleep(_POLL_SECONDS)
