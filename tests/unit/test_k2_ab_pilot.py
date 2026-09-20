"""L1 试点: arm B(2 根新增) 由 stored 提示词拼接得到 —— 纯函数单测."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from k2_ab_pilot import section_block, data_rows, splice_arm_b  # noqa: E402

_STORED = chr(10).join([
    "## 阶段一增量更新任务",
    "上方是你上一轮完成的阶段一诊断。",
    "## 当前分析目标更新",
    "",
    "品种:ZECUSDT 周期:30m K线数量:60 新增已收盘K线:1",
    "",
    "## 新增 K线数据(共1根，序号1=最新已收盘；含阳阴列)",
    "",
    "序号 | 时间             | 开盘价",
    "-----+------------------+-------",
    "1    | 2026-09-20 11:30 | 1453.20",
    "说明：下表仅含最近 N 根已收盘 K 线。",
    "",
    "## 新增 K线几何特征(共1根；多棒形态按完整60根窗口计算)",
    "",
    "序号 | 类型",
    "-----+------------",
    "1    | trend_bull",
    "",
    "## 程序结构辅助特征",
    "无关内容",
])

_RENDERED = _STORED.replace("新增已收盘K线:1", "新增已收盘K线:2").replace(
    "(共1根", "(共2根"
).replace(
    "1    | 2026-09-20 11:30 | 1453.20",
    "1    | 2026-09-20 11:30 | 1453.20" + chr(10) + "2    | 2026-09-20 11:00 | 1451.10",
).replace(
    "1    | trend_bull",
    "1    | trend_bull" + chr(10) + "2    | trend_bear",
)


def test_section_block_bounds_exclude_the_next_header():
    block = section_block(_STORED, "## 新增 K线数据")
    assert block.startswith("## 新增 K线数据")
    assert "新增 K线几何特征" not in block
    assert "说明：下表仅含最近 N 根" in block


def test_data_rows_picks_numbered_table_rows_only():
    rows = data_rows(section_block(_STORED, "## 新增 K线数据"))
    assert rows == ["1    | 2026-09-20 11:30 | 1453.20"]


def test_splice_arm_b_reproduces_the_two_bar_message():
    out = splice_arm_b(_STORED, _RENDERED)
    assert out == _RENDERED
    assert out.count("新增已收盘K线:2") == 1
    assert out.count("(共2根") == 2
    assert data_rows(section_block(out, "## 新增 K线几何特征")) == [
        "1    | trend_bull", "2    | trend_bear"]


def test_splice_arm_b_tolerates_indicator_drift_but_rejects_other_bars():
    stored = _STORED.replace(
        "1    | 2026-09-20 11:30 | 1453.20",
        "1    | 2026-09-20 11:30 | 1453.20 | 1474.6099 | 16.2011",
    )
    rendered = _RENDERED.replace(
        "1    | 2026-09-20 11:30 | 1453.20",
        "1    | 2026-09-20 11:30 | 1453.20 | 1474.7845 | 16.3145",
    )
    out = splice_arm_b(stored, rendered)
    assert "1    | 2026-09-20 11:30 | 1453.20 | 1474.6099 | 16.2011" in out, "K1 行必须保持生产原样"
    other = rendered.replace("2026-09-20 11:30", "2026-09-20 12:00")
    try:
        splice_arm_b(stored, other)
    except ValueError as exc:
        assert "不是同一根棒" in str(exc)
    else:
        raise AssertionError("K1 不是同一根棒时必须拒绝拼接")


def test_splice_arm_b_keeps_everything_else_byte_identical():
    out = splice_arm_b(_STORED, _RENDERED)
    for anchor in ("## 程序结构辅助特征", "无关内容", "上方是你上一轮完成的阶段一诊断。"):
        assert anchor in out
    assert out.split("## 程序结构辅助特征")[1] == _STORED.split("## 程序结构辅助特征")[1]
