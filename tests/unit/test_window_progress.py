"""观察窗口判定规则(§2.13)."""
from __future__ import annotations

from scripts.window_progress import count_opened, verdict


def test_observation_only_before_min_days():
    scen, why = verdict(fills=5, attempts=5, days=1.0)
    assert scen == "观察中"
    assert "3 天后" in why


def test_count_opened_only_counts_in_window(tmp_path):
    log = tmp_path / "pa_agent.log"
    log.write_text(
        "2026-09-18 12:00:00,000 INFO pa_agent: Testnet limit entry filled and protected: AAAUSDT x\n"
        "2026-09-18 19:07:42,297 INFO pa_agent: Testnet limit entry filled and protected: LINKUSDT x\n"
        "2026-09-18 19:08:00,000 INFO pa_agent: 别的日志\n",
        encoding="utf-8",
    )
    cutoff = 1789724814000  # 2026-09-18 17:46:54 +08:00
    assert count_opened(str(log), cutoff) == 1


def test_count_opened_missing_file_is_zero():
    assert count_opened("/nonexistent/pa_agent.log", 0) == 0


def test_optimistic_when_two_or_more_fills():
    scen, why = verdict(fills=2, attempts=3, days=3.1)
    assert scen == "乐观"
    assert "实盘口径" in why


def test_pessimistic_when_few_fills():
    scen, why = verdict(fills=1, attempts=3, days=3.1)
    assert scen == "悲观"
    assert "回放口径" in why


def test_bottleneck_elsewhere_when_nothing_happens():
    scen, _ = verdict(fills=0, attempts=0, days=3.0)
    assert scen == "瓶颈不在节拍"
