"""HTF (higher-timeframe) programmatic summaries (Phase D, Task D1).

Deterministic compact context derived from a higher-timeframe KlineFrame:
EMA drift, envelope position/width, swing structure, breakout quality. No LLM
opinion — the text is factual and capped at *max_chars* so monitor-injected
prompts stay cheap. The block is advisory: stage-1 rules keep the analyzed
timeframe authoritative.
"""
from __future__ import annotations

import math
from typing import Any

from pa_agent.ai.market_features import compute_simple_market_features

_DRIFT_BACK_BARS = 8


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def summarize_htf(htf_frame: Any, *, max_chars: int = 400) -> str:
    """One compact summary line set for an HTF frame; '' when unusable."""
    bars = getattr(htf_frame, "bars", None) or ()
    if not bars:
        return ""
    try:
        indicators = getattr(htf_frame, "indicators", None)
        ema = tuple(getattr(indicators, "ema20", ()) or ()) if indicators else ()
        atr = tuple(getattr(indicators, "atr14", ()) or ()) if indicators else ()
        if not ema or not atr:
            return ""
        latest = bars[0]
        close = float(latest.close)
        ema0 = float(ema[0])
        atr0 = float(atr[0])
        back = min(_DRIFT_BACK_BARS, len(ema) - 1, len(ema) - 1)
        drift = None
        if back >= 1 and not math.isnan(ema[back]) and atr0 > 0 and not math.isnan(atr0):
            drift = (ema0 - float(ema[back])) / back / atr0
        feats = compute_simple_market_features(htf_frame)
        parts = [f"收盘 {close:.6g} | EMA20 {ema0:.6g}"]
        if drift is not None:
            parts.append(f"EMA漂移 {drift:+.3f}xATR/根")
        zone = str(getattr(feats, "zone", "") or "")
        if zone:
            parts.append(f"箱内位置 {zone}")
        width = _num(getattr(feats, "range_width_atr", None))
        if width is not None:
            parts.append(f"箱宽 {width:.1f}xATR")
        swing = str(getattr(feats, "swing_structure", "") or "")
        if swing and swing != "insufficient":
            parts.append(f"波段 {swing}")
        bq = str(getattr(feats, "breakout_quality", "") or "")
        if bq and bq != "none":
            parts.append(f"突破质量 {bq}")
        text = " | ".join(parts)
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
    except (TypeError, ValueError, AttributeError, IndexError):
        return ""


def build_htf_context_text(parts: dict[str, str]) -> str:
    """Assemble per-timeframe summaries into the stage-1 HTF context block."""
    usable = {tf: text for tf, text in parts.items() if text}
    if not usable:
        return ""
    lines = [
        "## 更高时间框架(程序摘要)",
        "以下为 HTF 程序特征压缩, 仅供长程背景参考, 不得覆盖当前分析周期结构判断。",
        "",
    ]
    for tf in sorted(usable):
        lines.append(f"[{tf}] {usable[tf]}")
    return "\n".join(lines)
