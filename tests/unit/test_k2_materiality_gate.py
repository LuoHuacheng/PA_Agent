"""Phase 0 门控: K2 材料性 与 模型反应 的纯函数单测.

工具是一次性分析件(tools/k2_materiality_gate.py), 只读 records/pending;
这里只钉住纯计算部分, 保证分层表与判定阈值可复算。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from k2_materiality_gate import (  # noqa: E402
    atr14,
    gate_verdict,
    k2_materiality,
    reaction,
    wilson_ci,
)


def _bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_atr14_is_mean_true_range_of_last_14_bars_newest_first():
    # 15 根棒: K1(最新) TR=2, 其余每根 TR=1 -> 最后 14 根 = 1 个 2 + 13 个 1
    bars = [_bar(100, 101, 99, 100)]
    for _ in range(14):
        bars.append(_bar(100, 100.5, 99.5, 100))
    atr = atr14(bars)
    assert atr is not None
    assert round(atr, 4) == round((2.0 + 13 * 1.0) / 14, 4)


def test_atr14_returns_none_when_not_enough_bars():
    assert atr14([_bar(1, 2, 0.5, 1)] * 5) is None


def test_k2_materiality_body_and_breaks_and_opposite_and_outside():
    k1 = _bar(100, 101, 99, 100.5)   # 阳线
    k2 = _bar(100, 102, 98, 99)      # 阴线 + outside(高更高低更低) + 破前高
    prev = [_bar(100, 100.2, 99.8, 100) for _ in range(20)]
    m = k2_materiality(k1, k2, atr=2.0, prev_bars=prev)
    assert m["body_atr"] == 0.5          # |99-100| / 2.0
    assert m["material_body"] is True    # >= 0.5
    assert m["breaks_prev"] is True      # 102 > 前 20 根最高 100.2
    assert m["opposite"] is True         # K2 阴 / K1 阳
    assert m["outside"] is True          # high>K1.high 且 low<K1.low


def test_k2_materiality_flags_immaterial_inside_bar():
    k1 = _bar(100, 101, 99, 100.5)
    k2 = _bar(100.2, 100.8, 99.4, 100.3)  # 小实体、被 K1 包住、未破前高低
    prev = [_bar(100, 100.9, 99.3, 100) for _ in range(20)]
    m = k2_materiality(k1, k2, atr=2.0, prev_bars=prev)
    assert m["material_body"] is False
    assert m["breaks_prev"] is False
    assert m["opposite"] is False
    assert m["outside"] is False


def test_reaction_detects_direction_cycle_confidence_and_plan_change():
    prev_diag = {"direction": "bullish", "cycle_position": "normal_channel"}
    prev_dec = {"decision": {
        "order_type": "限价单", "order_direction": "做多", "entry_price": 100,
        "stop_loss_price": 95, "take_profit_price": 110, "trade_confidence": 50}}
    diag = {"direction": "bearish", "cycle_position": "normal_channel"}
    dec = {"decision": {
        "order_type": "不下单", "order_direction": None, "entry_price": None,
        "stop_loss_price": None, "take_profit_price": None, "trade_confidence": 42}}
    r = reaction(prev_diag, prev_dec, diag, dec)
    assert r["direction_changed"] is True
    assert r["cycle_changed"] is False
    assert r["conf_delta"] == -8
    assert r["plan_now"] is False
    assert r["plan_changed"] is True     # 有计划 -> 无计划 也算实质变化


def test_reaction_plan_changed_is_none_when_neither_round_has_a_plan():
    prev_diag = {"direction": "bullish", "cycle_position": "x"}
    prev_dec = {"decision": {"order_type": "不下单", "trade_confidence": 30}}
    r = reaction(prev_diag, prev_dec, dict(prev_diag), dict(prev_dec))
    assert r["plan_now"] is False
    assert r["plan_changed"] is None


def test_wilson_ci_brackets_the_point_estimate():
    lo, hi = wilson_ci(3, 10)
    assert 0.0 <= lo < 0.3 < hi <= 1.0


def test_gate_verdict_splits_at_15pp():
    used = gate_verdict(material_rate=0.40, immaterial_rate=0.20, material_n=60, immaterial_n=60)
    assert used["reaction_gap_pp"] == 20.0
    assert used["verdict"] == "already_used_k2"
    worth = gate_verdict(material_rate=0.30, immaterial_rate=0.25, material_n=60, immaterial_n=60)
    assert worth["reaction_gap_pp"] == 5.0
    assert worth["verdict"] == "worth_ab"
    thin = gate_verdict(material_rate=0.40, immaterial_rate=0.10, material_n=5, immaterial_n=5)
    assert thin["verdict"] == "insufficient_sample"
