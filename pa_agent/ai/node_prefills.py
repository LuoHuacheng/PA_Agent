"""Program node prefills for Stage 2 (Task B2).

Deterministic pre-judgements injected next to the relevant decision-tree
nodes so the model argues against numbers instead of re-deriving them:

- §6.3  (range boundary): 是/否 + lower|upper|middle, driven by the program
  price_position and the ATR-normalized distance to the nearest envelope edge.
- §9.0  (signal-bar quality): strong|medium|weak|invalid for K1, driven by
  program geometry (bar_type/body/close position/range vs ATR).

The prefill is advisory for boundaries (model may cite structure to relax
it). Quality upgrades (weak -> medium/strong or invalid -> strong/medium)
need explicit K-line evidence in the stage-2 output; downgrades are free.
"""
from __future__ import annotations

from typing import Any

#: ATR-multiplier distance under which price sits "at" a range boundary.
_NEAR_EDGE_ATR = 1.2
#: price_position extremes treated as edge proximity regardless of distance.
_POS_EDGE = 0.25
_QUALITY_RANK = {"invalid": 0, "weak": 1, "medium": 2, "strong": 3}


def rank_quality(quality: str) -> int:
    return _QUALITY_RANK.get(str(quality).strip().lower(), 0)


def prefill_boundary(features: Any) -> tuple[str, str, str] | None:
    """§6.3 prefill -> (answer, branch, evidence); None when no position."""
    position = getattr(features, "price_position", None)
    if position is None:
        return None
    dist_low = getattr(features, "dist_to_low_atr", None)
    dist_high = getattr(features, "dist_to_high_atr", None)
    pos = float(position)
    near_low = pos <= _POS_EDGE and dist_low is not None and float(dist_low) <= _NEAR_EDGE_ATR
    near_high = pos >= 1.0 - _POS_EDGE and dist_high is not None and float(dist_high) <= _NEAR_EDGE_ATR
    if near_low:
        return "是", "lower", (
            f"收盘分位{pos:.2f}, 距下沿{float(dist_low):.2f}xATR"
        )
    if near_high:
        return "是", "upper", (
            f"收盘分位{pos:.2f}, 距上沿{float(dist_high):.2f}xATR"
        )
    branch = "lower" if pos < 1 / 3 else ("upper" if pos > 2 / 3 else "middle")
    return "否", branch, f"收盘分位{pos:.2f}, 远离边界"


def prefill_signal_quality(k1: Any) -> tuple[str, str]:
    """§9.0 K1 signal-bar quality prefill -> (quality, evidence)."""
    bar_type = str(k1.bar_type or "").strip().lower()
    rng = float(k1.range_atr_ratio or 0.0) or 0.0
    body = float(k1.body_ratio or 0.0) or 0.0
    close_pos = float(k1.close_position or 0.0) or 0.0
    bull = bar_type in ("trend_bull", "outside_bull")
    bear = bar_type in ("trend_bear", "outside_bear")
    directional = bull or bear
    # empirical replay (3254 records): the model marks most directionless
    # doji/inside bars invalid unless they carry a real range, so the
    # non-directional invalid floor sits higher than the directional one
    if rng < (0.55 if not directional else 0.35):
        return "invalid", f"K1={bar_type}, 振幅{rng:.2f}xATR 过小"
    if not directional:
        return "weak", f"K1={bar_type}, 无方向性实体, 仅观察"
    side = close_pos if bull else 1.0 - close_pos
    if rng >= 1.0 and side >= 0.7 and body >= 0.6:
        return "strong", f"K1={bar_type}, 振幅{rng:.2f}xATR, 收盘分位{side:.2f}, 实体{body:.2f}"
    if rng >= 0.6 and side >= 0.6 and body >= 0.5:
        return "medium", f"K1={bar_type}, 振幅{rng:.2f}xATR, 收盘分位{side:.2f}"
    return "weak", f"K1={bar_type}, 强度不足(振幅{rng:.2f}xATR/分位{side:.2f})"


def render_node_prefills_block(
    *,
    boundary: tuple[str, str, str] | None,
    quality: tuple[str, str] | None,
) -> str:
    """Stage-2 prefill text block; '' when nothing was computable."""
    if boundary is None and quality is None:
        return ""
    lines = ["## 程序节点预判(§6.3 边界 / §9.0 信号棒质量, 客观参考)", ""]
    if boundary is not None:
        answer, branch, evidence = boundary
        lines.append(f"- §6.3 程序预判: answer={answer}, branch={branch} -- {evidence}")
        lines.append("  模型可结合更长结构给出理由后放宽该预判。")
    if quality is not None:
        qual, evidence = quality
        lines.append(f"- §9.0 K1 信号棒质量预判: {qual} -- {evidence}")
        lines.append(
            "  若你认为应**升级**质量, 必须在 reasoning 或 signal_bar.reason 中"
            "引用具体K线序号(如 K1)与结构证据; 降级无需说明。"
        )
    return "\n".join(lines)
