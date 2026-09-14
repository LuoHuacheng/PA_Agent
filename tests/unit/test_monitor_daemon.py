"""pa-monitor start 默认后台化: 就绪确认 / 启动失败回显 / 超时兜底 / 前台分支.

后台 worker 用真实的小 subprocess 扮演, 而不是 mock 掉 subprocess.Popen:
要验证的正是 "父进程如何观察子进程", 把它 mock 掉就等于不测.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from pa_agent.config.paths import PROJECT_ROOT
from pa_agent.monitoring.daemon import READY_TIMEOUT_SECONDS, spawn_background

_WRITE_PID = "open(pid_path, 'w', encoding='utf-8').write(json.dumps({'pid': os.getpid()}))\n"


def _fake_worker(tmp_path: Path, body: str) -> list[str]:
    """把 body 写成一个真实脚本, 作为 spawn_background 的 worker 命令行."""
    script = tmp_path / "fake_worker.py"
    script.write_text(
        "import json, os, sys, time\n" "pid_path = os.environ['FAKE_WORKER_PID_PATH']\n" + body,
        encoding="utf-8",
    )
    return [sys.executable, "-u", str(script)]


def test_spawn_background_returns_zero_once_worker_records_its_pid(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pid_path = tmp_path / "monitor.pid"
    monkeypatch.setenv("FAKE_WORKER_PID_PATH", str(pid_path))

    code = spawn_background(
        _fake_worker(tmp_path, _WRITE_PID + "time.sleep(0.5)\n"),
        log_path=tmp_path / "monitor.out",
        pid_path=pid_path,
        ready_timeout=10.0,
    )

    assert code == 0
    assert "[已后台启动]" in capsys.readouterr().out


def test_spawn_background_surfaces_early_worker_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pid_path = tmp_path / "monitor.pid"
    monkeypatch.setenv("FAKE_WORKER_PID_PATH", str(pid_path))

    code = spawn_background(
        _fake_worker(tmp_path, "print('enabled=false, 拒绝启动')\nsys.exit(2)\n"),
        log_path=tmp_path / "monitor.out",
        pid_path=pid_path,
        ready_timeout=10.0,
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "退出码=2" in captured.err
    assert "enabled=false, 拒绝启动" in captured.err


def test_spawn_background_does_not_claim_ready_on_timeout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pid_path = tmp_path / "monitor.pid"
    monkeypatch.setenv("FAKE_WORKER_PID_PATH", str(pid_path))

    code = spawn_background(
        _fake_worker(tmp_path, "time.sleep(0.6)\n"),
        log_path=tmp_path / "monitor.out",
        pid_path=pid_path,
        ready_timeout=0.2,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "未确认" in captured.err
    assert "monitor.out" in captured.err


def test_ready_timeout_default_is_generous_enough_for_real_startup() -> None:
    """真实启动要过 AppContext + Binance 环境校验, 默认超时不能太短."""
    assert READY_TIMEOUT_SECONDS >= 10.0


@pytest.mark.skipif(os.name == "nt", reason="start_new_session 是 POSIX 语义")
def test_background_worker_runs_in_its_own_session(tmp_path: Path, monkeypatch) -> None:
    """关掉终端不再收 SIGHUP 的前提: 子进程另开 session, 与父进程不同."""
    pid_path = tmp_path / "monitor.pid"
    monkeypatch.setenv("FAKE_WORKER_PID_PATH", str(pid_path))

    code = spawn_background(
        _fake_worker(tmp_path, _WRITE_PID + "time.sleep(1.0)\n"),
        log_path=tmp_path / "monitor.out",
        pid_path=pid_path,
        ready_timeout=10.0,
    )

    assert code == 0
    child_pid = int(json.loads(pid_path.read_text(encoding="utf-8"))["pid"])
    assert child_pid != os.getpid()
    assert os.getsid(child_pid) != os.getsid(0)


def test_main_start_backgrounds_by_default(monkeypatch, tmp_path: Path) -> None:
    from pa_agent.monitoring import daemon

    captured: dict[str, object] = {}

    def fake_spawn(worker_argv, **kwargs):
        captured["argv"] = list(worker_argv)
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(daemon, "spawn_background", fake_spawn)
    monkeypatch.setattr("pa_agent.config.paths.MONITORING_PID_PATH", tmp_path / "monitoring.pid")
    monkeypatch.setattr("pa_agent.config.paths.MONITORING_LOG_PATH", tmp_path / "monitor.out")

    from pa_agent.monitoring.cli import main

    assert main(["start"]) == 0
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[1:3] == ["-m", "pa_agent.monitoring.cli"]
    assert argv[-1] == "--foreground"  # 子进程不能再把自己后台化一次
    assert captured["pid_path"] == tmp_path / "monitoring.pid"
    assert captured["log_path"] == tmp_path / "monitor.out"


def test_main_start_foreground_runs_in_current_process(monkeypatch) -> None:
    from pa_agent.monitoring import daemon

    spawned: list[object] = []
    ran: list[int] = []

    monkeypatch.setattr(daemon, "spawn_background", lambda *a, **k: spawned.append(a) or 0)
    monkeypatch.setattr("pa_agent.monitoring.cli.run_monitor", lambda: ran.append(1) or 0)

    from pa_agent.monitoring.cli import main

    assert main(["start", "--foreground"]) == 0
    assert ran == [1]
    assert spawned == []


def test_main_start_refuses_when_monitor_already_running(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from pa_agent.monitoring import daemon

    pid_path = tmp_path / "monitoring.pid"
    pid_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    monkeypatch.setattr(
        "pa_agent.monitoring.cli._process_command",
        lambda _pid: f"python {PROJECT_ROOT / 'run.py'} --monitor",
    )
    monkeypatch.setattr("pa_agent.config.paths.MONITORING_PID_PATH", pid_path)
    spawned: list[object] = []
    monkeypatch.setattr(daemon, "spawn_background", lambda *a, **k: spawned.append(a) or 0)

    from pa_agent.monitoring.cli import main

    assert main(["start"]) == 1
    assert spawned == []
    assert "已在运行" in capsys.readouterr().err


def test_main_start_rejects_unknown_flag(capsys) -> None:
    from pa_agent.monitoring.cli import main

    assert main(["start", "--bogus"]) == 2
    assert "未知参数" in capsys.readouterr().err
