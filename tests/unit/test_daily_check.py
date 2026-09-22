"""日检回填告警: 无声停增必须变成有声.

背景: 原实现把子命令的退出码丢掉, 于是"密钥失效 -> outcomes.csv 停更"只表现为台账里
一个 "(无输出)" 字符串, 日检照样退出 0, 没人会发现 —— 而超过 --days 7 窗口之后,
那批成交就永久归因不上了。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_daily_check():
    spec = importlib.util.spec_from_file_location(
        "daily_check_under_test", ROOT / "scripts" / "daily_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dc = _load_daily_check()


def test_refill_reports_success_line(monkeypatch):
    monkeypatch.setattr(
        dc, "_run_checked", lambda args: (0, ["新 OutcomeRows: 3 (uid 幂等去重落盘)"])
    )
    label, ok = dc.outcome_merge_line()
    assert ok is True
    assert label.startswith("新 OutcomeRows")


def test_refill_marks_missing_key_as_failure(monkeypatch):
    """密钥缺失时 outcome_merge 打印一行后 return 0 —— 退出码 0 但一行都没写."""
    monkeypatch.setattr(
        dc,
        "_run_checked",
        lambda args: (
            0,
            ["Testnet API key/secret missing: 无法拉取成交源, outcomes 保持为空。"],
        ),
    )
    label, ok = dc.outcome_merge_line()
    assert ok is False
    assert label.startswith("!! 回填失败")
    assert "Testnet API key" in label


def test_refill_marks_crash_as_failure(monkeypatch):
    monkeypatch.setattr(dc, "_run_checked", lambda args: (137, ["Killed: 9"]))
    label, ok = dc.outcome_merge_line()
    assert ok is False
    assert "rc=137" in label


def _stub_other_steps(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "TSV", tmp_path / "daily_check.tsv")
    monkeypatch.setattr(dc, "LOG_DIR", tmp_path)
    monkeypatch.setattr(dc, "account_equity", lambda: None)
    monkeypatch.setattr(dc, "_run", lambda args: [])
    monkeypatch.setattr(dc, "fill_line", lambda: "fill_stats: n/a")
    monkeypatch.setattr(dc, "orders_count", lambda: 0)
    monkeypatch.setattr(dc, "service_alive", lambda: "no")


def test_main_exits_nonzero_when_refill_fails(monkeypatch, tmp_path, capsys):
    _stub_other_steps(monkeypatch, tmp_path)
    monkeypatch.setattr(dc, "outcome_merge_line", lambda: ("!! 回填失败 rc=0: 无", False))
    assert dc.main() == 1
    printed = capsys.readouterr().out
    assert "永久" in printed
    row = (tmp_path / "daily_check.tsv").read_text(encoding="utf-8")
    assert "!! 回填失败" in row


def test_main_exits_zero_when_refill_succeeds(monkeypatch, tmp_path):
    _stub_other_steps(monkeypatch, tmp_path)
    monkeypatch.setattr(
        dc, "outcome_merge_line", lambda: ("新 OutcomeRows: 0 (uid 幂等去重落盘)", True)
    )
    assert dc.main() == 0
