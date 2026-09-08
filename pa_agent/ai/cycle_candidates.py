"""Program cycle-position candidates (Task B1).

Phase-B step 1: instead of letting the model freely pick one of the eight
cycle spectrum labels, the program proposes a small candidate set from
deterministic metrics and the stage-1 routing must land inside it (a
high-bar node_overrides escape hatch stays available). The rules below are
deliberately generous (top-3, score >= 50) and only become a hard constraint
when constrain_worthy() is true (top score >= 55) — ambiguous markets simply
produce an empty set and no constraint at all.

Metrics come from SimpleMarketFeatures plus EMA drift / spike amplitude
computed on the frame (all deterministic, no model opinion).

Rule table (v1, calibrated by historical replay; see
docs/superpowers/plans/2026-09-08-ai-strategy-optimization.md Task B1):
- trading_range      : box-like range, breakout failed/testing/none, weak drift
- trending_tr        : tilted range: monotonic swings + clear drift
- micro/tight/normal/broad_channel: monotonic swings + drift, width bands
- spike              : extreme recent momentum / amplitude
- extreme_tr         : barbwire + narrow + flat
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pa_agent.ai.market_features import compute_simple_market_features
from pa_agent.data.base import KlineFrame
from pa_agent.indicators.atr import atr_full
from pa_agent.indicators.ema import ema_full

#: cycles with an English-side direction nuance stay direction-agnostic here;
#: the direction itself is the §2.3 program vote, not this module's job.
MIN_CANDIDATE_SCORE = 50.0
CONSTRAIN_TOP_SCORE = 55.0
MAX_CANDIDATES = 3
_MIN_BARS = 30
_DRIFT_WINDOW = 8
_EMA_PERIOD = 20
_ATR_PERIOD = 14
_SPIKE_AMP_ATR = 2.0
_SPIKE_AMP_SOFT_ATR = 1.2
_SPIKE_DRIFT_ATR = 0.8
_DRIFT_STRONG_ATR = 0.25
_RANGE_BREAKOUTS = ("failed", "testing", "none")
#: width bands (range width / ATR) per channel family (tuned heuristics).
_WIDTH_MICRO_MAX = 1.8
_WIDTH_TIGHT_MAX = 3.0
_WIDTH_NORMAL_MAX = 5.2
_BARBWIRE_WIDTH_MAX = 3.0
_BARBWIRE_DRIFT_MAX = 0.3


@dataclass(frozen=True)
class CycleCandidate:
    cycle: str
    score: float
    evidence: str


@dataclass(frozen=True)
class CycleMetrics:
    n_bars: int
    width_atr: float | None = None
    price_position: float | None = None
    zone: str = "unknown"
    overlap_mean: float | None = None
    barbwire_candidate: bool = False
    swing_structure: str = "insufficient"
    breakout_quality: str = "none"
    ema_drift_atr: float | None = None
    spike_amp_atr: float | None = None
    spike_aftermath_hint: str = "none"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _flat_drift(drift: float | None) -> bool:
    return drift is None or abs(drift) <= _DRIFT_STRONG_ATR


def _directional(metrics: CycleMetrics) -> str | None:
    """'bull'/'bear' when swings and EMA drift agree, else None."""
    drift = metrics.ema_drift_atr
    if drift is None or abs(drift) < _DRIFT_STRONG_ATR:
        return None
    bull_swings = metrics.swing_structure == "HH+HL"
    bear_swings = metrics.swing_structure == "LL+LH"
    if drift > 0 and bull_swings:
        return "bull"
    if drift < 0 and bear_swings:
        return "bear"
    return None


def _range_score(metrics: CycleMetrics) -> tuple[float, str] | None:
    if metrics.breakout_quality not in _RANGE_BREAKOUTS:
        return None
    if metrics.breakout_quality == "none" and not _flat_drift(metrics.ema_drift_atr):
        return None  # clean move, not a range
    score = 50.0
    notes: list[str] = []
    if metrics.breakout_quality == "failed":
        score += 8.0
        notes.append("突破质量failed")
    elif metrics.breakout_quality == "testing":
        score += 5.0
        notes.append("边界测试中")
    if metrics.zone == "middle_third":
        score += 3.0
    if metrics.swing_structure in ("mixed", "insufficient"):
        score += 6.0
        notes.append("波段方向不明确")
    if metrics.ema_drift_atr is not None and abs(metrics.ema_drift_atr) > 0.6:
        score -= 12.0
    if metrics.width_atr is not None and metrics.width_atr > 9.0:
        score -= 8.0
    if metrics.barbwire_candidate:
        score -= 6.0
    if score < MIN_CANDIDATE_SCORE:
        return None
    parts = [f"箱宽{metrics.width_atr or 0:.1f}xATR"]
    parts.extend(notes)
    parts.append("均线漂移" + (f"{metrics.ema_drift_atr:+.2}xATR" if metrics.ema_drift_atr is not None else "平"))
    return _clamp(score, 0.0, 80.0), "、".join(parts)


def _trending_range_score(metrics: CycleMetrics) -> tuple[float, str] | None:
    direction = _directional(metrics)
    if direction is None:
        return None
    drift = abs(metrics.ema_drift_atr or 0.0)
    if metrics.breakout_quality not in _RANGE_BREAKOUTS:
        return None
    if metrics.width_atr is not None and metrics.width_atr > 9.0:
        return None
    score = 52.0 + _clamp(drift * 15.0, 0.0, 16.0)
    if metrics.breakout_quality == "failed":
        score -= 4.0
    return _clamp(score, 0.0, 80.0), (
        f"波段{'HH+HL' if direction == 'bull' else 'LL+LH'}、"
        f"均线漂移{metrics.ema_drift_atr:+.2}xATR、箱宽{metrics.width_atr or 0:.1f}xATR"
    )


def _channel_candidates(metrics: CycleMetrics) -> list[CycleCandidate]:
    direction = _directional(metrics)
    if direction is None:
        return []
    drift = abs(metrics.ema_drift_atr or 0.0)
    if metrics.overlap_mean is not None and metrics.overlap_mean >= 0.6:
        return []  # overlapping mess, not a clean channel
    width = metrics.width_atr
    if width is None:
        return []
    bands: list[tuple[str, float, float]] = [
        ("micro_channel", 0.0, _WIDTH_MICRO_MAX),
        ("tight_channel", _WIDTH_MICRO_MAX, _WIDTH_TIGHT_MAX),
        ("normal_channel", _WIDTH_TIGHT_MAX, _WIDTH_NORMAL_MAX),
        ("broad_channel", _WIDTH_NORMAL_MAX, 1e9),
    ]
    out: list[CycleCandidate] = []
    for cycle, lo, hi in bands:
        if lo < width <= hi:
            score = 55.0 + _clamp((2.0 - abs(width - lo) * 0.3) * 4.0, -6.0, 10.0)
            score += _clamp(drift * 10.0, 0.0, 6.0)
            out.append(CycleCandidate(
                cycle=cycle,
                score=_clamp(score, 0.0, 78.0),
                evidence=(
                    f"箱宽{width:.1f}xATR、均线漂移{metrics.ema_drift_atr:+.2}xATR"
                    f"、波段{metrics.swing_structure}"
                ),
            ))
    return out


def _spike_score(metrics: CycleMetrics) -> tuple[float, str] | None:
    amp = metrics.spike_amp_atr
    drift = metrics.ema_drift_atr
    if amp is None or drift is None:
        return None
    strong = amp >= _SPIKE_AMP_ATR
    soft = amp >= _SPIKE_AMP_SOFT_ATR and abs(drift) >= _SPIKE_DRIFT_ATR
    if not (strong or soft):
        return None
    # one big candle inside a failed-breakout range is a rejection bar, not a
    # spike: suppress unless the amplitude is extreme or the move survives
    flat_swings = metrics.swing_structure in ("mixed", "insufficient")
    if metrics.breakout_quality == "failed" and flat_swings and amp < 2.6:
        return None
    score = 62.0 + _clamp((amp - _SPIKE_AMP_ATR) * 8.0, 0.0, 12.0)
    if metrics.breakout_quality == "surviving":
        score += 4.0
    return _clamp(score, 0.0, 85.0), (
        f"近端单棒振幅{amp:.1f}xATR、均线漂移{drift:+.2}xATR、"
        f"突破质量{metrics.breakout_quality}"
    )


def _extreme_tr_score(metrics: CycleMetrics) -> tuple[float, str] | None:
    if not metrics.barbwire_candidate:
        return None
    if metrics.width_atr is not None and metrics.width_atr > _BARBWIRE_WIDTH_MAX:
        return None
    if not _flat_drift(metrics.ema_drift_atr):
        return None
    if metrics.breakout_quality not in ("failed", "none"):
        return None
    score = 60.0
    if metrics.overlap_mean is not None and metrics.overlap_mean >= 0.8:
        score += 8.0
    return _clamp(score, 0.0, 80.0), (
        f"铁丝网{barbwire_label(metrics.overlap_mean)}、箱宽{metrics.width_atr or 0:.1f}xATR"
    )


def barbwire_label(overlap: float | None) -> str:
    if overlap is None:
        return "?"
    return "强" if overlap >= 0.8 else "中"


def score_cycle(metrics: CycleMetrics) -> list[CycleCandidate]:
    """Deterministic candidates (score >= 50, top 3); [] when unclassifiable."""
    if metrics.n_bars < _MIN_BARS:
        return []
    cands: list[CycleCandidate] = []
    rng = _range_score(metrics)
    if rng:
        cands.append(CycleCandidate("trading_range", rng[0], rng[1]))
    trng = _trending_range_score(metrics)
    if trng:
        cands.append(CycleCandidate("trending_tr", trng[0], trng[1]))
    cands.extend(_channel_candidates(metrics))
    spike = _spike_score(metrics)
    if spike:
        cands.append(CycleCandidate("spike", spike[0], spike[1]))
    ext = _extreme_tr_score(metrics)
    if ext:
        cands.append(CycleCandidate("extreme_tr", ext[0], ext[1]))
    cands.sort(key=lambda c: (-c.score, c.cycle))
    cands = [c for c in cands if c.score >= MIN_CANDIDATE_SCORE][:MAX_CANDIDATES]
    return _expand_neighbors(cands, metrics)


#: cycles that may sit next to each other on the continuous spectrum
_FAMILY_ADJACENCY: dict[str, tuple[str, ...]] = {
    "micro_channel": ("tight_channel",),
    "tight_channel": ("micro_channel", "normal_channel"),
    "normal_channel": ("tight_channel", "broad_channel", "trending_tr"),
    "broad_channel": ("normal_channel", "trending_tr", "trading_range"),
    "trending_tr": ("broad_channel", "trading_range", "normal_channel"),
    "trading_range": ("trending_tr", "broad_channel", "extreme_tr"),
    "extreme_tr": ("trading_range",),
    "spike": (),
}

#: a pick beyond the whole candidate set + its family neighbours is only
#: treated as a routing error when the program is this confident and clear.
CLEAR_TOP_SCORE = 60.0
CLEAR_GAP_MIN = 4.0


def _expand_neighbors(
    cands: list[CycleCandidate], metrics: CycleMetrics
) -> list[CycleCandidate]:
    """Add spectrum-adjacent alternatives when boundaries are fuzzy.

    Cycle labels are continuous: punishing the immediate neighbour of a
    program candidate would force false retries. Neighbours are added unless
    the top candidate is ultra-clear (score >= 68) with no close runner-up;
    the result never exceeds MAX_CANDIDATES.
    """
    del metrics  # width/trend conditions are folded into the adjacency table
    out = list(cands)
    names = [c.cycle for c in out]
    top = out[0].score if out else 0.0
    gap = (top - out[1].score) if len(out) > 1 else 0.0

    def add(cycle: str) -> None:
        if cycle not in names and len(out) < MAX_CANDIDATES:
            out.append(CycleCandidate(cycle, max(MIN_CANDIDATE_SCORE, top - 1.0),
                                      "频谱相邻的周期候选(程序边界模糊)"))
            names.append(cycle)

    if len(out) < MAX_CANDIDATES and not (top >= 68.0 and gap >= 6.0):
        for cycle in [c.cycle for c in out]:
            for adj in _FAMILY_ADJACENCY.get(cycle, ()):
                add(adj)
    out.sort(key=lambda c: (-c.score, c.cycle))
    return out[:MAX_CANDIDATES]


def constrain_worthy(candidates: list[CycleCandidate]) -> list[CycleCandidate]:
    """Candidates that actually constrain routing (clear program read).

    A routing error is only raised when the *base* (non-adjacency) top
    candidate is both confident (>= CLEAR_TOP_SCORE) and clearly ahead of the
    base runner-up (>= CLEAR_GAP_MIN); otherwise the block stays advisory and
    no retry is forced. Adjacency candidates do not dilute the gap.
    """
    if not candidates:
        return []
    base = [c for c in candidates if not c.evidence.startswith("频谱相邻")]
    base = base or candidates
    if base[0].score < CLEAR_TOP_SCORE:
        return []
    if len(base) > 1 and base[0].score - base[1].score < CLEAR_GAP_MIN:
        return []
    return candidates[:MAX_CANDIDATES]


def build_metrics(frame: KlineFrame) -> CycleMetrics:
    """Deterministic metrics for one frame (SimpleMarketFeatures + drift)."""
    bars = list(frame.bars)
    n = len(bars)
    if n == 0:
        return CycleMetrics(n_bars=0)
    features = compute_simple_market_features(frame)
    base = CycleMetrics(n_bars=n)
    base = CycleMetrics(
        n_bars=n,
        width_atr=_num(features.range_width_atr),
        price_position=_num(features.price_position),
        zone=str(features.zone or "unknown"),
        overlap_mean=_num(features.overlap_mean_10),
        barbwire_candidate=bool(features.barbwire_candidate),
        swing_structure=str(features.swing_structure or "insufficient"),
        breakout_quality=str(features.breakout_quality or "none"),
        ema_drift_atr=None,
        spike_amp_atr=None,
        spike_aftermath_hint=str(features.spike_aftermath_hint or "none"),
    )
    if n < _MIN_BARS:
        return base
    oldest_first = list(reversed(bars))
    closes = [float(b.close) for b in oldest_first]
    ema = ema_full(closes, _EMA_PERIOD)
    atr = atr_full(
        [float(b.high) for b in oldest_first],
        [float(b.low) for b in oldest_first],
        closes,
        _ATR_PERIOD,
    )
    atr_last = atr[-1]
    ema_last = ema[-1]
    if math.isnan(atr_last) or math.isnan(ema_last) or atr_last <= 0:
        return base
    back = min(_DRIFT_WINDOW, n - _EMA_PERIOD)
    if back >= 1 and not math.isnan(ema[-1 - back]):
        base = CycleMetrics(
            n_bars=n,
            width_atr=base.width_atr,
            price_position=base.price_position,
            zone=base.zone,
            overlap_mean=base.overlap_mean,
            barbwire_candidate=base.barbwire_candidate,
            swing_structure=base.swing_structure,
            breakout_quality=base.breakout_quality,
            ema_drift_atr=(ema_last - ema[-1 - back]) / atr_last,
            spike_amp_atr=_recent_spike_amp(bars[:5], atr_last),
            spike_aftermath_hint=base.spike_aftermath_hint,
        )
    return base


def _recent_spike_amp(bars, atr: float) -> float | None:
    amps = []
    for b in bars:
        body = abs(float(b.close) - float(b.open))
        rng = float(b.high) - float(b.low)
        amps.append(max(body, rng * 0.0) if rng > 0 else 0.0)
    if not amps:
        return None
    return round(max(amps) / atr, 2) if atr > 0 else None


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def render_cycle_candidates_block(candidates: list[CycleCandidate]) -> str:
    """Stage-1 prompt block; '' when the program does not constrain routing."""
    worthy = constrain_worthy(candidates)
    if not candidates:
        return ""
    lines = ["## 程序周期候选集（阶段一 cycle_position 路由参考）", ""]
    if worthy:
        lines.append(
            "输出 `cycle_position` **必须命中下方候选之一**；"
            "若你认为程序遗漏明确结构证据，可走 `node_overrides`（node_id=1.2）"
            "并逐条引用具体 K 线序号与结构特征，不接受模糊理由。"
        )
    else:
        lines.append("（程序置信不足，候选仅供趋势/区间倾向参考，不强制命中。）")
    lines.append("")
    for cand in candidates:
        lines.append(f"- {cand.cycle}（置信 {cand.score:.0f}）：{cand.evidence}")
    return "\n".join(lines)
