"""窗口用量统计工具的测试 (2026-09-22)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from tools.usage_by_window import (
    count_window_skips,
    coverage_start_ts,
    in_blocked_window,
    parse_line,
    summarize,
    window_record_paths,
)

KV = "2026-09-22 02:00:00,000 INFO     pa_agent: KV-cache: hit=100 miss=50 total_prompt=150"
DONE = (
    "2026-09-22 02:01:00,000 INFO     pa_agent: DeepSeekClient.stream_chat done: "
    "latency=1000 ms reasoning_chars=0 content_chars=400 deepseek_thinking=True effort=medium"
)


def test_parse_line_extracts_usage_only_from_usage_lines():
    prompt = parse_line(KV)
    assert prompt is not None
    assert (prompt.hit, prompt.miss, prompt.out_chars) == (100, 50, 0)
    assert prompt.ts == datetime(2026, 9, 22, 2, 0, 0)
    done = parse_line(DONE)
    assert done is not None
    assert done.out_chars == 400
    assert (done.hit, done.miss) == (0, 0)
    assert parse_line("2026-09-22 02:00:00,000 INFO     pa_agent: 别的日志行") is None
    assert parse_line("not a log line") is None


def test_in_blocked_window_wraps_midnight():
    start, end = 20, 4
    for hour in (20, 23, 0, 1, 3):
        assert in_blocked_window(datetime(2026, 9, 22, hour), start_hour=start, end_hour=end)
    for hour in (4, 12, 19):
        assert not in_blocked_window(datetime(2026, 9, 22, hour), start_hour=start, end_hour=end)
    # 不跨零点的窗口
    assert in_blocked_window(datetime(2026, 9, 22, 9), start_hour=8, end_hour=12)
    assert not in_blocked_window(datetime(2026, 9, 22, 13), start_hour=8, end_hour=12)
    # start == end 视为空窗口
    assert not in_blocked_window(datetime(2026, 9, 22, 9), start_hour=8, end_hour=8)


def test_summarize_splits_buckets_and_counts_calls():
    samples = [
        parse_line(KV),
        parse_line(DONE),
        parse_line(KV.replace("02:00", "05:00")),
        parse_line(DONE.replace("02:01", "05:01")),
    ]
    out = summarize(samples, start_hour=20, end_hour=4)
    assert out["in_window"]["calls"] == 1
    assert out["in_window"]["prompt_tokens"] == 150
    assert out["in_window"]["hit"] == 100 and out["in_window"]["miss"] == 50
    assert out["in_window"]["out_chars"] == 400
    assert out["outside"]["calls"] == 1
    assert out["outside"]["prompt_tokens"] == 150
    assert out["ignored"] == 0


def test_summarize_ignores_non_usage_lines():
    out = summarize([None, None], start_hour=20, end_hour=4)
    assert out["ignored"] == 2
    assert out["in_window"]["calls"] == 0 and out["outside"]["calls"] == 0


def test_coverage_start_ts_matches_log_span():
    # 分母必须与分子的日志覆盖期对齐, 否则 调用/轮 会被没有日志的那段拉偏。
    samples = [parse_line(KV), parse_line(KV.replace("02:00", "09:00"))]
    assert coverage_start_ts(samples, fallback_ts=0.0) == datetime(2026, 9, 22, 2, 0, 0).timestamp()
    assert coverage_start_ts([], fallback_ts=7.0) == 7.0
    assert coverage_start_ts([None], fallback_ts=7.0) == 7.0


def test_window_record_paths_filters_by_utc_hour(tmp_path):
    # mtime 是 epoch(无时区歧义), 小时必须按 UTC 判, 否则窗口边界会错。
    inside = tmp_path / "in.json"
    outside = tmp_path / "out.json"
    for path, hour in ((inside, 21), (outside, 12)):
        path.write_text("{}", encoding="utf-8")
        stamp = datetime(2026, 9, 22, hour, tzinfo=UTC).timestamp()
        os.utime(path, (stamp, stamp))
    got = window_record_paths(
        tmp_path,
        start_hour=20,
        end_hour=4,
        cutoff_ts=datetime(2026, 9, 1, tzinfo=UTC).timestamp(),
    )
    assert [p.name for p in got] == ["in.json"]


def test_main_oneline_prints_summary(tmp_path, capsys):
    from tools.usage_by_window import main

    logs = tmp_path / "logs"
    logs.mkdir()
    body = KV.replace("02:00", "05:00") + "\n" + DONE.replace("02:01", "05:01") + "\n"
    (logs / "pa_agent.log").write_text(body, encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "binance_usdm_testnet": {
                    "block_session_utc_start_hour": 20,
                    "block_session_utc_end_hour": 4,
                }
            }
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            "--logs-dir",
            str(logs),
            "--records-dir",
            str(tmp_path / "none"),
            "--settings",
            str(settings),
            "--tz-offset",
            "8",
            "--days",
            "9999",
            "--oneline",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "窗口内调用=1" in out
    assert "调用/轮=" in out


def test_count_window_skips_reads_light_skip_flag(tmp_path):
    for name, flag, order_type in (
        ("a.json", True, "不下单"),
        ("b.json", False, "不下单"),
        ("c.json", True, "限价单"),
    ):
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "timestamp_local_iso": "2026-09-22T11:30:00",
                    "stage2_decision": {
                        "light_skip_stage2": flag,
                        "decision": {"order_type": order_type},
                    },
                }
            ),
            encoding="utf-8",
        )
    stats = count_window_skips(sorted(tmp_path.glob("*.json")))
    assert stats["records"] == 3
    assert stats["skipped"] == 2
    assert stats["by_symbol"] == {}  # 文件名不含品种时不乱猜
