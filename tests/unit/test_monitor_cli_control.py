from __future__ import annotations

import json
import os
from pathlib import Path

from pa_agent.monitoring.cli import (
    _acquire_monitor_pid,
    _arm_stop_watchdog,
    _is_monitor_process,
    _live_monitor_pid,
    _release_monitor_pid,
    _shutdown_monitor_process,
    _signal_name,
    monitor_status,
    stop_monitor,
)


def test_signal_name_labels_signals_for_shutdown_logs() -> None:
    """Shutdown diagnostics need a readable signal name in the log."""
    import signal as _signal

    assert _signal_name(_signal.SIGTERM) == "SIGTERM"
    assert _signal_name(_signal.SIGINT) == "SIGINT"
    assert _signal_name(9999) == "9999"


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


def test_is_stale_pid_record_treats_evidenced_process_as_live(
    tmp_path: Path, monkeypatch
) -> None:
    """命令能证明是本项目监控、但 cwd 取不到时, 必须算活着 (不许双开).

    2026-09-18 发现: POSIX 分支要求 cwd == 项目根, 拿不到 cwd 就直接判 False,
    _is_stale_pid_record 于是把活着的监控当陈旧记录, start 会删 pid 文件并抢占
    —— 两套 watchdog/挂单 watcher 同时管一个账号。Windows 分支早就按"命令行证据"
    认活, POSIX 漏了。lsof 缺失的精简环境(无 ps 或用受限 shell)就会踩到。
    """
    from pa_agent.monitoring.cli import _is_stale_pid_record

    root = tmp_path / "project"
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text(
        json.dumps({"pid": 123, "script": str(root / "run.py")}), encoding="utf-8"
    )
    monkeypatch.setattr("pa_agent.monitoring.cli._pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        "pa_agent.monitoring.cli._process_command",
        lambda _pid: "python -m pa_agent.monitoring.cli start --foreground",
    )
    monkeypatch.setattr("pa_agent.monitoring.cli._process_cwd", lambda _pid: None)

    assert _is_stale_pid_record(pid_path, root=root) is False


def test_is_stale_pid_record_still_replaces_unrelated_process(
    tmp_path: Path, monkeypatch
) -> None:
    """命令行与本项目无关时仍然算陈旧, 不能因为放宽 cwd 要求就认贼作父."""
    from pa_agent.monitoring.cli import _is_stale_pid_record

    root = tmp_path / "project"
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text(json.dumps({"pid": 123}), encoding="utf-8")
    monkeypatch.setattr("pa_agent.monitoring.cli._pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        "pa_agent.monitoring.cli._process_command", lambda _pid: "nginx: master process"
    )
    monkeypatch.setattr("pa_agent.monitoring.cli._process_cwd", lambda _pid: None)

    assert _is_stale_pid_record(pid_path, root=root) is True


def test_status_and_stop_work_without_ps_or_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    """拿不到命令行(ps 被禁)时, 用 pid 文件 + cwd 旁证也要能识别并停止.

    2026-09-18 实测: 沙箱禁 /bin/ps -> _process_command 返回空串 -> status 报
    "未运行"、stop 拒发信号, 服务实际在跑。这里用 cwd == 项目根 + pid 文件是
    本项目写的作为旁证; 双开保护(抢占)不在此放宽, 见下面两条 stale 测试。
    """
    root = tmp_path / "project"
    pid_path = tmp_path / "monitor.pid"
    pid = 424242
    pid_path.write_text(
        json.dumps({"pid": pid, "script": str(root / "run.py")}), encoding="utf-8"
    )
    # 前 3 次查询(两次识别 + stop 的前置检查)算活着, 发完信号后即"退出"
    alive = {"n": 0}

    def pid_exists(_pid: int) -> bool:
        alive["n"] += 1
        return alive["n"] <= 3

    monkeypatch.setattr("pa_agent.monitoring.cli._pid_exists", pid_exists)
    monkeypatch.setattr("pa_agent.monitoring.cli._process_command", lambda _pid: "")
    monkeypatch.setattr("pa_agent.monitoring.cli._process_cwd", lambda _pid: root)
    monkeypatch.setattr("pa_agent.monitoring.cli.time.sleep", lambda _s: None)

    assert _live_monitor_pid(pid_path, root=root) == pid
    assert monitor_status(pid_path, root=root) == 0
    assert f"PID={pid}" in capsys.readouterr().out

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "pa_agent.monitoring.cli.os.kill", lambda p, sig: killed.append((p, sig))
    )
    monkeypatch.setattr("pa_agent.monitoring.cli.os.kill", lambda p, sig: killed.append((p, sig)))
    assert stop_monitor(pid_path, root=root) == 0
    assert killed == [(pid, 15)], "旁证成立时 stop 必须真的发 SIGTERM"


def test_status_without_any_evidence_reports_not_running(tmp_path: Path, monkeypatch, capsys) -> None:
    """命令行与 cwd 都没有证据时, 仍然只能报"未运行", 不许乱认领进程."""
    root = tmp_path / "project"
    pid_path = tmp_path / "monitor.pid"
    pid_path.write_text(json.dumps({"pid": 999}), encoding="utf-8")
    monkeypatch.setattr("pa_agent.monitoring.cli._pid_exists", lambda _pid: True)
    monkeypatch.setattr("pa_agent.monitoring.cli._process_command", lambda _pid: "")
    monkeypatch.setattr(
        "pa_agent.monitoring.cli._process_cwd", lambda _pid: tmp_path / "elsewhere"
    )

    assert _live_monitor_pid(pid_path, root=root) is None
    assert monitor_status(pid_path, root=root) == 1
    assert "监控未运行" in capsys.readouterr().out


def test_stop_watchdog_hard_exits_blocked_shutdown(monkeypatch) -> None:
    """Even if graceful teardown never returns, the watchdog force-exits."""
    exited: list[int] = []
    monkeypatch.setattr("pa_agent.monitoring.cli.os._exit", exited.append)

    watchdog = _arm_stop_watchdog(delay=0.05)
    watchdog.join(timeout=1.0)

    assert exited == [0]
    assert not watchdog.is_alive()


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


def test_main_pnl_dispatches_with_days(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    def fake_report(**kwargs: object) -> None:
        calls.update(kwargs)

    monkeypatch.setattr(
        "pa_agent.trading.binance_usdm_testnet.report_daily_pnl", fake_report
    )
    monkeypatch.setattr(
        "pa_agent.config.paths.SETTINGS_JSON_PATH", tmp_path / "settings.json"
    )
    monkeypatch.setattr(
        "pa_agent.config.settings.load_settings",
        lambda _path: type("S", (), {"binance_usdm_testnet": object()})(),
    )

    from pa_agent.monitoring.cli import main

    assert main(["pnl", "--days", "3", "--csv", "/tmp/x.csv"]) == 0
    assert calls["days"] == 3
    assert calls["csv_path"] == "/tmp/x.csv"


def test_main_pnl_rejects_unknown_flag(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("pa_agent.config.paths.SETTINGS_JSON_PATH", tmp_path / "settings.json")

    from pa_agent.monitoring.cli import main

    assert main(["pnl", "--bogus"]) == 2
