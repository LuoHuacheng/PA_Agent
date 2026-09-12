"""Unit tests for program cycle candidates (Task B1).

score_cycle runs on explicit metrics (unit boundary values), the frame
builder is smoke-tested for sanity; routing coherence is verified by the
validator-level tests.
"""
from __future__ import annotations


from pa_agent.ai.cycle_candidates import (
    CycleCandidate,
    CycleMetrics,
    render_cycle_candidates_block,
    score_cycle,
    build_metrics,
    constrain_worthy,
)
from pa_agent.ai.kline_features import compute_kline_geometry_features  # noqa: F401


def _m(**over) -> CycleMetrics:
    base = dict(
        n_bars=60,
        width_atr=4.0,
        price_position=0.5,
        zone="middle_third",
        overlap_mean=0.45,
        barbwire_candidate=False,
        swing_structure="mixed",
        breakout_quality="failed",
        ema_drift_atr=0.05,
        spike_amp_atr=0.8,
        spike_aftermath_hint="none",
    )
    base.update(over)
    return CycleMetrics(**base)


def test_range_wins_for_failed_breakout_flat_structure():
    cands = score_cycle(_m())
    assert cands and cands[0].cycle == "trading_range"
    assert cands[0].evidence


def test_trending_range_requires_directional_structure():
    cands = score_cycle(_m(swing_structure="HH+HL", ema_drift_atr=0.5,
                          breakout_quality="none"))
    base = [c for c in cands if not c.evidence.startswith("频谱相邻")]
    names = [c.cycle for c in base]
    assert "trending_tr" in names[:2]
    assert "trading_range" not in names[:2]


def test_channel_width_bands_and_barbwire_extreme():
    bull = _m(swing_structure="HH+HL", ema_drift_atr=0.45, overlap_mean=0.3,
              breakout_quality="none")
    cands = score_cycle(bull)
    assert any("channel" in c.cycle or c.cycle == "micro_channel" for c in cands[:3])
    ext = _m(barbwire_candidate=True, width_atr=1.4, overlap_mean=0.85,
             ema_drift_atr=0.02, breakout_quality="failed")
    assert score_cycle(ext)[0].cycle == "extreme_tr"


def test_spike_detected_for_high_momentum():
    cands = score_cycle(_m(ema_drift_atr=1.1, spike_amp_atr=2.6,
                           swing_structure="HH+HL", breakout_quality="surviving"))
    assert cands and cands[0].cycle == "spike"


def test_no_constraint_when_too_few_bars_or_all_below_min():
    assert score_cycle(_m(n_bars=20)) == []
    assert not constrain_worthy([])
    weak = [CycleCandidate(cycle="trading_range", score=48, evidence="x")]
    assert score_cycle(_m(n_bars=60)) and not constrain_worthy(weak)


def test_constrain_worthy_requires_top_score_and_limit():
    many = [CycleCandidate(cycle=f"c{i}", score=s, evidence="e")
            for i, s in enumerate((64, 60, 59, 58, 57))]
    worthy = constrain_worthy(many)
    assert worthy and len(worthy) <= 3
    close = [CycleCandidate(cycle=f"c{i}", score=61 - i, evidence="e")
             for i in range(3)]
    assert constrain_worthy(close) == []  # gap < 4 -> advisory only


def test_render_block_text():
    cands = [CycleCandidate(cycle="trading_range", score=64,
                            evidence="箱宽4.2xATR, 突破质量failed")]
    text = render_cycle_candidates_block(cands)
    assert "trading_range" in text and "4.2" in text
    assert render_cycle_candidates_block([]) == ""


def test_build_metrics_smoke_on_synthetic_frames():
    # linear uptrend bars (newest first) should yield directional drift
    from pa_agent.data.base import IndicatorBundle, KlineBar, KlineFrame

    # newest first: i=0 is K1; closes fall with i so the newest close is the
    # highest -> the EMA drift over time must be positive
    bars = tuple(
        KlineBar(seq=i + 1, ts_open=float(1_700_000_000 - i * 3600),
                 open=160.0 - i, high=161.5 - i, low=159.0 - i,
                 close=160.8 - i, volume=1000.0, closed=True)
        for i in range(60)
    )
    ind = IndicatorBundle(ema20=tuple(100.0 + i for i in range(60)),
                          atr14=tuple(1.0 for _ in range(60)))
    frame = KlineFrame(symbol="X", timeframe="1h", bars=bars, indicators=ind,
                       snapshot_ts_local_ms=1_700_000_000_000)
    m = build_metrics(frame)
    assert m.n_bars == 60
    assert m.ema_drift_atr is not None and m.ema_drift_atr > 0
