from __future__ import annotations

import json
import os
from pathlib import Path

from pa_agent.monitoring.cli import (
    _acquire_monitor_pid,
    _is_monitor_process,
    _release_monitor_pid,
    _shutdown_monitor_process,
)


def test_pid_guard_rejects_live_monitor_instance(tmp_path: Path, monkeypatch) -> None:
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    monkeypatch.setattr(
        "pa_agent.monitoring.cli._process_command",
        lambda _pid: f"python {Path.cwd() / 'run.py'} --monitor",
    )

    assert _acquire_monitor_pid(pid_path) is False


def test_pid_guard_replaces_stale_record_for_live_unrelated_process(
    tmp_path: Path, monkeypatch
) -> None:
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text('{"pid": 123}', encoding="utf-8")
    monkeypatch.setattr("pa_agent.monitoring.cli._pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        "pa_agent.monitoring.cli._process_command", lambda _pid: "other-project --monitor"
    )
    monkeypatch.setattr("pa_agent.monitoring.cli._process_cwd", lambda _pid: tmp_path)

    assert _acquire_monitor_pid(pid_path) is True
    assert f'"pid": {os.getpid()}' in pid_path.read_text(encoding="utf-8")

    _release_monitor_pid(pid_path)
    assert not pid_path.exists()


def test_pid_guard_removes_stale_pid_and_acquires(tmp_path: Path, monkeypatch) -> None:
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text(json.dumps({"pid": 424242}), encoding="utf-8")
    monkeypatch.setattr("pa_agent.monitoring.cli._pid_exists", lambda _pid: False)

    assert _acquire_monitor_pid(pid_path) is True
    assert f'"pid": {os.getpid()}' in pid_path.read_text(encoding="utf-8")

    _release_monitor_pid(pid_path)
    assert not pid_path.exists()


def test_is_monitor_process_requires_exact_project_script(tmp_path: Path) -> None:
    root = tmp_path / "project"
    expected = str(root / "run.py")
    assert _is_monitor_process(f"python {expected} --monitor", root) is True
    assert _is_monitor_process(f"python {root / 'other' / 'run.py'} --monitor", root) is False
    assert _is_monitor_process(f"python {expected}", root) is False


def test_is_monitor_process_module_entry_requires_matching_cwd(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    command = "python -m pa_agent.monitoring.cli"
    monkeypatch.setattr("pa_agent.monitoring.cli._process_cwd", lambda _pid: root)
    assert _is_monitor_process(command, root, pid=123) is True
    monkeypatch.setattr("pa_agent.monitoring.cli._process_cwd", lambda _pid: tmp_path)
    assert _is_monitor_process(command, root, pid=123) is False
    assert _is_monitor_process(command, root) is False


def test_shutdown_forces_exit_after_blocked_monitor_stop(tmp_path: Path, monkeypatch) -> None:
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    class BlockedMonitor:
        def stop(self, timeout: float) -> bool:
            assert timeout == 0.01
            return False

    exited: list[int] = []
    monkeypatch.setattr("pa_agent.monitoring.cli.os._exit", exited.append)

    _shutdown_monitor_process(BlockedMonitor(), pid_path, timeout=0.01, stop_requested=True)

    assert exited == [0]
    assert not pid_path.exists()


def test_main_stop_dispatches_to_stop_monitor(monkeypatch, tmp_path: Path) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        "pa_agent.monitoring.cli.stop_monitor",
        lambda path: calls.append(path) or 0,
    )
    monkeypatch.setattr("pa_agent.config.paths.MONITORING_PID_PATH", tmp_path / "monitoring.pid")

    from pa_agent.monitoring.cli import main

    assert main(["stop"]) == 0
    assert calls == [tmp_path / "monitoring.pid"]
