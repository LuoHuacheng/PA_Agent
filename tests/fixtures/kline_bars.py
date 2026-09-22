"""Synthetic newest-first K-line lists for tests."""

from __future__ import annotations

from pa_agent.data.base import KlineBar


def make_trending_bars(
    n: int,
    *,
    base_ts: float = 1_700_000_000.0,
    step_sec: float = 900.0,
    with_forming: bool = True,
) -> list[KlineBar]:
    """Build an uptrend, newest-first: the newest closed bar prints a fresh high.

    与 make_newest_first_bars 的区别: 那里所有棒同价(平坦序列), 放到真实门控里会被
    读成中性震荡, 于是 LLM 的多单在归一化/方向门控阶段被降级成「不下单」。需要
    "方向确实是多头" 的用例用这个。
    """
    bars: list[KlineBar] = []
    newest_level = 2000.0 + (n - 1) * 2.0
    if with_forming:
        p = newest_level + 4.0
        bars.append(
            KlineBar(
                seq=1,
                ts_open=base_ts,
                open=p,
                high=p + 1.0,
                low=p - 1.0,
                close=p + 0.5,
                volume=100.0,
                closed=False,
            )
        )
    start = 2 if with_forming else 1
    for idx, seq in enumerate(range(start, start + n)):
        p = newest_level - idx * 2.0
        bars.append(
            KlineBar(
                seq=seq,
                ts_open=base_ts - (seq - 1) * step_sec,
                open=p,
                high=p + 2.0,
                low=p - 0.5,
                close=p + 1.5,
                volume=100.0,
                closed=True,
            )
        )
    return bars


def make_newest_first_bars(
    n: int,
    *,
    base_ts: float = 1_700_000_000.0,
    step_sec: float = 900.0,
    with_forming: bool = True,
) -> list[KlineBar]:
    """Build *n* closed bars plus optional forming bar at index 0 (seq=1)."""
    bars: list[KlineBar] = []
    if with_forming:
        bars.append(
            KlineBar(
                seq=1,
                ts_open=base_ts,
                open=2000.0,
                high=2010.0,
                low=1990.0,
                close=2005.0,
                volume=100.0,
                closed=False,
            )
        )
    start = 2 if with_forming else 1
    for seq in range(start, start + n):
        bars.append(
            KlineBar(
                seq=seq,
                ts_open=base_ts - (seq - 1) * step_sec,
                open=2000.0,
                high=2010.0,
                low=1990.0,
                close=2005.0,
                volume=100.0,
                closed=True,
            )
        )
    return bars
