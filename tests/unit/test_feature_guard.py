"""feature_guard 静态体检器的规则回归.

每条规则一个正例(必须命中) + 反例(正确写法必须放行), 外加 CLI 退出码与
--demo 自校验. 体检器本身是纯静态的, 用例不导入也不执行被测源码.
"""

from __future__ import annotations

import json

from tools.feature_guard import (
    RULE_IDS,
    ScanResult,
    main,
    scan_source,
)


def _rules(code: str) -> set[str]:
    return {v.rule for v in scan_source(code).violations}


def _errors(code: str) -> set[str]:
    return {v.rule for v in scan_source(code).errors}


# ---------------------------------------------------------------- 干净样例


def test_clean_lookback_factor_passes():
    code = """def compute(df, idx):
    if idx < 20:
        return 0.0
    window = df["close"].iloc[idx - 19 : idx + 1]
    return float(window.std())
"""
    result = scan_source(code)
    assert result.ok
    assert result.violations == []
    assert result.checked_lines == 5


def test_rolling_window_then_stat_is_not_flagged():
    # rolling 是回溯窗口, 后面的 .mean() 不该被当成整段序列统计
    code = """def compute(df, idx):
    ma = df["close"].rolling(20).mean()
    return float(ma.iloc[idx])
"""
    assert scan_source(code).ok


def test_iloc_truncated_stat_is_not_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[: idx + 1].max())
"""
    assert scan_source(code).ok


def test_slice_upper_exclusive_boundary_is_allowed():
    # 上界不含, idx + 1 正好切到当前 bar
    code = """def compute(df, idx):
    return float(df["open"].iloc[idx - 4 : idx + 1].mean())
"""
    assert scan_source(code).ok


# ------------------------------------------------------------ 前视泄漏


def test_forward_index_offset_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[idx + 1])
"""
    assert "future_idx_offset" in _errors(code)


def test_forward_offset_two_bars_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[idx + 2])
"""
    assert "future_idx_offset" in _errors(code)


def test_open_ended_slice_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[idx:].mean())
"""
    assert "future_idx_offset" in _errors(code)


def test_open_ended_slice_from_past_lower_is_flagged():
    # [idx - 19 :] 上界缺省 => 一路切到序列末尾, 把未来 bar 也算进来了
    code = """def compute(df, idx):
    recent = df.iloc[idx - 19:]
    return float(recent["high"].max() - recent["low"].min())
"""
    assert "future_idx_offset" in _errors(code)


def test_full_slice_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[:].mean())
"""
    assert "future_idx_offset" in _errors(code)


def test_open_ended_slice_on_local_window_is_only_warn():
    # 根变量不是整段行情时无法确认, 降级为 warn 而不是 error
    code = """def compute(df, idx):
    window = df["close"].iloc[idx - 9 : idx + 1]
    return float(window.iloc[3:].mean())
"""
    result = scan_source(code)
    assert result.ok
    assert "future_idx_offset" in {v.rule for v in result.warns}


def test_future_slice_upper_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[idx - 5 : idx + 3].mean())
"""
    assert "future_idx_offset" in _errors(code)


def test_negative_shift_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].shift(-1).iloc[idx])
"""
    assert "future_shift" in _errors(code)


def test_positive_shift_is_allowed():
    code = """def compute(df, idx):
    return float(df["close"].shift(1).iloc[idx])
"""
    assert scan_source(code).ok


def test_centered_rolling_is_flagged():
    code = """def compute(df, idx):
    ma = df["close"].rolling(5, center=True).mean()
    return float(ma.iloc[idx])
"""
    assert "future_centered_rolling" in _errors(code)


def test_bfill_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].bfill().iloc[idx])
"""
    assert "future_backfill" in _errors(code)


def test_fillna_backfill_method_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].fillna(method="bfill").iloc[idx])
"""
    assert "future_backfill" in _errors(code)


def test_ffill_is_allowed():
    code = """def compute(df, idx):
    return float(df["close"].ffill().iloc[idx])
"""
    assert scan_source(code).ok


def test_full_series_stat_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].max() - df["close"].iloc[idx])
"""
    assert "future_full_series_stat" in _errors(code)


def test_tail_index_on_frame_is_flagged():
    code = """def compute(df, idx):
    return float(df["close"].iloc[-1])
"""
    assert "future_tail_index" in _errors(code)


def test_tail_index_on_local_window_is_allowed():
    # 局部窗口取尾值不是前视
    code = """def compute(df, idx):
    window = df["close"].iloc[idx - 9 : idx + 1]
    return float(window.iloc[-1])
"""
    assert scan_source(code).ok


# ------------------------------------------------------------ 危险调用


def test_exec_is_flagged():
    code = """def compute(df, idx):
    exec("x = 1")
    return 0.0
"""
    assert "dangerous_call" in _errors(code)


def test_import_os_is_flagged():
    code = """import os


def compute(df, idx):
    return 0.0
"""
    assert "forbidden_import" in _errors(code)


def test_import_numpy_alias_is_allowed():
    code = """import numpy as np
import pandas as pd
import math


def compute(df, idx):
    return float(np.log1p(pd.Series([1.0]).iloc[0]) + math.e)
"""
    assert scan_source(code).ok


def test_pandas_read_csv_is_flagged():
    code = """import pandas as pd


def compute(df, idx):
    return float(len(pd.read_csv("x.csv")))
"""
    assert "dangerous_call" in _errors(code)


def test_dunder_escape_is_flagged():
    code = """def compute(df, idx):
    return float(len(df.__class__.__subclasses__()))
"""
    assert "dunder_escape" in _errors(code)


# ------------------------------------------------------------ 结构检查


def test_missing_compute_is_flagged():
    code = """def factor(df, idx):
    return 0.0
"""
    assert "missing_compute" in _errors(code)


def test_bad_compute_signature_is_flagged():
    code = """def compute(df):
    return 0.0
"""
    assert "bad_compute_signature" in _errors(code)


def test_print_is_warn_not_error():
    code = """def compute(df, idx):
    print("debug")
    return 0.0
"""
    result = scan_source(code)
    assert result.ok
    assert {v.rule for v in result.warns} == {"debug_print"}


def test_unbounded_while_is_warn():
    code = """def compute(df, idx):
    while True:
        break
    return 0.0
"""
    result = scan_source(code)
    assert result.ok
    assert "unbounded_loop" in {v.rule for v in result.warns}


# ------------------------------------------------------------ 其它契约


def test_syntax_error_is_reported_not_raised():
    result = scan_source("def compute(df, idx)")
    assert not result.ok
    assert result.parse_error is not None
    assert result.violations == []


def test_violations_are_sorted_by_position():
    code = """def compute(df, idx):
    a = df["close"].iloc[-1]
    b = df["close"].shift(-1).iloc[idx]
    return float(a + b)
"""
    lines = [v.line for v in scan_source(code).violations]
    assert lines == sorted(lines)


def test_every_reported_rule_is_declared():
    codes = [
        "import os\n\n\ndef compute(df, idx):\n    return 0.0\n",
        'def compute(df, idx):\n    exec("x")\n    return 0.0\n',
        'def compute(df, idx):\n    return float(df.__dict__["x"])\n',
        'def compute(df, idx):\n    return float(df["c"].iloc[idx + 1])\n',
        'def compute(df, idx):\n    return float(df["c"].shift(-1).iloc[idx])\n',
        'def compute(df, idx):\n    return float(df["c"].rolling(3, center=True).mean().iloc[idx])\n',
        'def compute(df, idx):\n    return float(df["c"].bfill().iloc[idx])\n',
        'def compute(df, idx):\n    return float(df["c"].iloc[-1])\n',
        'def compute(df, idx):\n    return float(df["c"].max())\n',
        "def f(df, idx):\n    return 0.0\n",
        "def compute(df):\n    return 0.0\n",
        "def compute(df, idx):\n    print(1)\n    return 0.0\n",
        "def compute(df, idx):\n    while True:\n        break\n    return 0.0\n",
    ]
    declared = set(RULE_IDS)
    for code in codes:
        for violation in scan_source(code).violations:
            assert violation.rule in declared, violation.rule


def test_noqa_comment_suppresses_line():
    code = """def compute(df, idx):
    return float(df["close"].iloc[idx + 1])  # noqa: feature-guard
"""
    assert scan_source(code).ok


def test_to_dict_shape():
    result = scan_source('def compute(df, idx):\n    return float(df["c"].max())\n')
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["ok"] is False
    assert payload["error_count"] >= 1
    assert payload["violations"][0]["rule"] == "future_full_series_stat"


def test_format_text_has_status_and_rule():
    text = scan_source(
        'def compute(df, idx):\n    return float(df["c"].iloc[idx + 1])\n'
    ).format_text()
    assert text.startswith("FAIL")
    assert "future_idx_offset" in text


# ------------------------------------------------------------ CLI


def test_cli_reports_pass_and_uses_exit_zero(tmp_path, capsys):
    target = tmp_path / "clean.py"
    target.write_text("def compute(df, idx):\n    return 0.0\n", encoding="utf-8")
    assert main([str(target)]) == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_exit_one_when_error(tmp_path, capsys):
    target = tmp_path / "bad.py"
    target.write_text(
        'def compute(df, idx):\n    return float(df["c"].iloc[idx + 1])\n', encoding="utf-8"
    )
    assert main([str(target)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_quiet_hides_passing_files(tmp_path, capsys):
    (tmp_path / "clean.py").write_text("def compute(df, idx):\n    return 0.0\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text(
        'def compute(df, idx):\n    return float(df["c"].iloc[idx + 1])\n', encoding="utf-8"
    )
    assert main([str(tmp_path), "--quiet"]) == 1
    out = capsys.readouterr().out
    assert "bad.py" in out
    assert "clean.py" not in out


def test_cli_json_output_is_valid(tmp_path, capsys):
    (tmp_path / "clean.py").write_text("def compute(df, idx):\n    return 0.0\n", encoding="utf-8")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["name"].endswith("clean.py")


def test_cli_returns_two_on_missing_path(tmp_path):
    assert main([str(tmp_path / "nope.py")]) == 2


def test_cli_returns_two_without_args():
    assert main([]) == 2


def test_demo_self_check_passes(capsys):
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert "个样例符合期望" in out
    assert "BAD" not in out


def test_scan_result_defaults():
    result = ScanResult()
    assert result.ok
    assert result.errors == []
    assert result.warns == []
