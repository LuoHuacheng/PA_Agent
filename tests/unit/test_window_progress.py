"""观察窗口判定规则(§2.13)."""
from __future__ import annotations

from scripts.window_progress import verdict


def test_observation_only_before_two_days():
    scen, _ = verdict(fills=5, attempts=5, days=1.0)
    assert scen == "观察中"


def test_optimistic_when_two_or_more_fills():
    scen, why = verdict(fills=2, attempts=3, days=2.1)
    assert scen == "乐观"
    assert "实盘口径" in why


def test_pessimistic_when_few_fills():
    scen, why = verdict(fills=1, attempts=3, days=2.1)
    assert scen == "悲观"
    assert "回放口径" in why


def test_bottleneck_elsewhere_when_nothing_happens():
    scen, _ = verdict(fills=0, attempts=0, days=3.0)
    assert scen == "瓶颈不在节拍"
