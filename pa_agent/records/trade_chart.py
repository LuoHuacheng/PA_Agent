"""交易记录 K 线图渲染(从 trade_logger 拆出, 存储与绘图分离)。"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # 无头环境; 函数内再 import pyplot/Line2D

logger = logging.getLogger(__name__)

# Maximum bars to show in the chart image
_CHART_MAX_BARS = 50


def render_trade_chart(bars_newest_first: list[Any], ema20_newest_first: list[float],
                  symbol: str, timeframe: str, image_path: Path,
                  entry_price: float | None = None,
                  stop_loss_price: float | None = None,
                  take_profit_price: float | None = None,
                  take_profit_price_2: float | None = None,
                  order_direction: str = "",
                  order_type: str = "",
                  diagnosis_confidence: str = "",
                  trade_confidence: str = "",
                  estimated_win_rate: str = "") -> bool:
    """Draw a candlestick + EMA20 chart and save to *image_path*.

    Returns True on success, False if matplotlib is unavailable.
    bars_newest_first: list of KlineBar (or dict with open/high/low/close/ts_open/seq).
    ema20_newest_first: aligned EMA20 values (NaN for warm-up bars).
    entry_price / stop_loss_price / take_profit_price / take_profit_price_2: optional price levels drawn
    as horizontal dashed lines extending into the right-side margin.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.lines import Line2D
    except ImportError:
        logger.warning("matplotlib not installed; skipping chart generation")
        return False

    # Render Chinese labels with a CJK-capable font.
    import matplotlib.font_manager as _fm
    _cjk_chain = [
        # macOS: PingFang / Hiragino / Heiti ships with the OS
        "PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti", "Arial Unicode MS",
        # Windows / Linux
        "Microsoft YaHei", "SimHei", "WenQuanYi Micro Hei",
        "Noto Sans CJK SC", "Source Han Sans CN",
    ]
    # Pin the first installed CJK face as the primary family, and expose the
    # whole chain as the sans-serif fallback.  matplotlib >= 3.6 falls back per
    # glyph along the list, so even when a cold font-cache build races with
    # another thread and the ttflist scan looks incomplete, drawing still
    # resolves a CJK face instead of silently rendering tofu boxes.
    _available = {f.name for f in _fm.fontManager.ttflist}
    _primary = next((_fc for _fc in _cjk_chain if _fc in _available), None)
    matplotlib.rcParams["font.sans-serif"] = [*_cjk_chain, "DejaVu Sans"]
    matplotlib.rcParams["font.family"] = _primary or "sans-serif"
    matplotlib.rcParams["axes.unicode_minus"] = False

    # Limit to _CHART_MAX_BARS
    bars = list(reversed(bars_newest_first[:_CHART_MAX_BARS]))  # oldest → newest
    emas = list(reversed(ema20_newest_first[:_CHART_MAX_BARS]))

    n = len(bars)
    if n == 0:
        return False

    fig, ax = plt.subplots(figsize=(16, 7), facecolor="#0d1117")
    ax.set_facecolor("#0d1117")

    # ── Candles ───────────────────────────────────────────────────────────────
    bar_width = 0.6
    for i, bar in enumerate(bars):
        # Support both KlineBar dataclass and plain dict
        if hasattr(bar, "open"):
            o, h, l, c = bar.open, bar.high, bar.low, bar.close
            seq = getattr(bar, "seq", None)
        else:
            o = float(bar.get("open", 0))
            h = float(bar.get("high", 0))
            l = float(bar.get("low", 0))
            c = float(bar.get("close", 0))
            seq = bar.get("seq")

        is_bull = c >= o
        color = "#26a641" if is_bull else "#f85149"

        # Wick
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=2)
        # Body
        body_low = min(o, c)
        body_height = max(abs(c - o), (h - l) * 0.005)
        rect = mpatches.FancyBboxPatch(
            (i - bar_width / 2, body_low),
            bar_width,
            body_height,
            boxstyle="square,pad=0",
            facecolor=color,
            edgecolor=color,
            linewidth=0,
            zorder=3,
        )
        ax.add_patch(rect)

        # Sequence label on every 10th bar (newest = seq 1 at right)
        if seq is not None and seq % 10 == 0:
            ax.text(
                i, h * 1.0003, f"K{seq}",
                color="#8b949e", fontsize=6.5, ha="center", va="bottom", zorder=4,
            )

    # ── EMA20 line ────────────────────────────────────────────────────────────
    ema_x, ema_y = [], []
    for i, v in enumerate(emas):
        if not math.isnan(float(v)):
            ema_x.append(i)
            ema_y.append(v)
    if ema_x:
        ax.plot(ema_x, ema_y, color="#fbbf24", linewidth=1.2, zorder=5, label="EMA20")

    # ── Styling ───────────────────────────────────────────────────────────────
    # Reserve ~8 bar-widths on the right for price labels
    _RIGHT_MARGIN = 8
    ax.set_xlim(-1, n - 1 + _RIGHT_MARGIN)
    ax.tick_params(colors="#8b949e", labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor("#30363d")
    ax.set_title(
        f"{symbol} {timeframe}  —  最近 {n} 根K线（K1=最新收盘）",
        color="#e6edf3", fontsize=10, pad=8,
    )

    # ── Order type badge (top-left corner) ────────────────────────────────────
    if order_type:
        _ot = str(order_type).strip()
        _ot_colors = {
            "限价单": "#fbbf24",   # amber
            "突破单": "#a78bfa",   # purple
            "市价单": "#34d399",   # teal
        }
        _ot_color = _ot_colors.get(_ot, "#8b949e")
        ax.text(
            0.01, 0.97, _ot,
            transform=ax.transAxes,
            color=_ot_color, fontsize=9, fontweight="bold",
            va="top", ha="left",
            bbox=dict(facecolor="#161b22", edgecolor=_ot_color,
                      linewidth=1.2, alpha=0.9, pad=3, boxstyle="round,pad=0.3"),
            zorder=10,
        )

    # ── Confidence badges (top-left, right of order type) ─────────────────────
    # Show diagnosis_confidence / trade_confidence / estimated_win_rate as a
    # compact info row just below the order-type badge.
    _conf_parts = []
    if diagnosis_confidence:
        _conf_parts.append(f"诊断置信 {diagnosis_confidence}")
    if trade_confidence:
        _conf_parts.append(f"交易置信 {trade_confidence}")
    if estimated_win_rate:
        _conf_parts.append(f"胜率 {estimated_win_rate}")
    if _conf_parts:
        _conf_text = "   ".join(_conf_parts)
        ax.text(
            0.01, 0.905, _conf_text,
            transform=ax.transAxes,
            color="#cbd5e1", fontsize=8,
            va="top", ha="left",
            bbox=dict(facecolor="#161b22", edgecolor="#30363d",
                      linewidth=0.8, alpha=0.85, pad=3, boxstyle="round,pad=0.3"),
            zorder=10,
        )

    # ── Entry / SL / TP horizontal lines ──────────────────────────────────────
    # Determine bull/bear from order_direction for colour defaults
    _is_long = "short" not in order_direction.lower() and "做空" not in order_direction
    _ENTRY_COLOR = "#60a5fa"   # blue
    _TP_COLOR    = "#4ade80"   # green
    _TP2_COLOR   = "#86efac"   # lighter green
    _SL_COLOR    = "#f87171"   # red

    _price_lines: list[tuple[float, str, str]] = []  # (price, color, label)
    if entry_price is not None:
        _price_lines.append((entry_price, _ENTRY_COLOR, f"入场  {entry_price}"))
    if take_profit_price is not None:
        _price_lines.append((take_profit_price, _TP_COLOR, f"TP1  {take_profit_price}"))
    if take_profit_price_2 is not None:
        _price_lines.append((take_profit_price_2, _TP2_COLOR, f"TP2  {take_profit_price_2}"))
    if stop_loss_price is not None:
        _price_lines.append((stop_loss_price, _SL_COLOR, f"止损  {stop_loss_price}"))

    _label_x = n - 1 + _RIGHT_MARGIN - 0.3  # anchor for right-side text
    for _price, _color, _label in _price_lines:
        # Dashed line from bar 0 to right margin
        ax.axhline(_price, color=_color, linewidth=1.0, linestyle="--",
                   alpha=0.85, zorder=6)
        # Price label at right margin
        ax.text(
            _label_x, _price, _label,
            color=_color, fontsize=7.5, ha="right", va="center",
            bbox=dict(facecolor="#0d1117", edgecolor="none", alpha=0.7, pad=1.5),
            zorder=7,
        )

    # ── Direction arrow ───────────────────────────────────────────────────────
    # Draw a prominent up/down arrow at the right edge of the last bar to show
    # trade direction.  The arrow is anchored at entry_price when available,
    # otherwise at the last bar's close.
    if entry_price is not None or n > 0:
        # Arrow anchor Y
        _last_bar = bars[-1] if bars else None
        if _last_bar is not None:
            _last_close = (_last_bar.close if hasattr(_last_bar, "close")
                           else float(_last_bar.get("close", 0)))
            _last_high  = (_last_bar.high if hasattr(_last_bar, "high")
                           else float(_last_bar.get("high", 0)))
            _last_low   = (_last_bar.low if hasattr(_last_bar, "low")
                           else float(_last_bar.get("low", 0)))
        else:
            _last_close = _last_high = _last_low = entry_price or 0

        # Estimate price range for sizing the arrow
        all_prices = []
        for _b in bars:
            if hasattr(_b, "high"):
                all_prices += [_b.high, _b.low]
            else:
                all_prices += [float(_b.get("high", 0)), float(_b.get("low", 0))]
        _price_range = max(all_prices) - min(all_prices) if all_prices else 1.0
        _arrow_len = _price_range * 0.06   # 6% of visible range
        _arrow_x   = n - 1                 # x = last bar index

        if _is_long:
            # Up arrow: tail at low, head above
            _tail_y = (_last_low - _price_range * 0.01)
            _head_y = _tail_y + _arrow_len
            ax.annotate(
                "", xy=(_arrow_x, _head_y), xytext=(_arrow_x, _tail_y),
                arrowprops=dict(arrowstyle="-|>", color="#4ade80",
                                lw=2.5, mutation_scale=18),
                zorder=8,
            )
            ax.text(
                _arrow_x, _tail_y - _price_range * 0.005, "做多",
                color="#4ade80", fontsize=8, ha="center", va="top",
                fontweight="bold", zorder=9,
            )
        else:
            # Down arrow: tail at high, head below
            _tail_y = (_last_high + _price_range * 0.01)
            _head_y = _tail_y - _arrow_len
            ax.annotate(
                "", xy=(_arrow_x, _head_y), xytext=(_arrow_x, _tail_y),
                arrowprops=dict(arrowstyle="-|>", color="#f87171",
                                lw=2.5, mutation_scale=18),
                zorder=8,
            )
            ax.text(
                _arrow_x, _tail_y + _price_range * 0.005, "做空",
                color="#f87171", fontsize=8, ha="center", va="bottom",
                fontweight="bold", zorder=9,
            )

    legend_handles = [Line2D([0], [0], color="#fbbf24", linewidth=1.5, label="EMA20")]
    if entry_price is not None:
        legend_handles.append(Line2D([0], [0], color=_ENTRY_COLOR, linewidth=1.0,
                                     linestyle="--", label="入场"))
    if take_profit_price is not None:
        legend_handles.append(Line2D([0], [0], color=_TP_COLOR, linewidth=1.0,
                                     linestyle="--", label="TP1"))
    if take_profit_price_2 is not None:
        legend_handles.append(Line2D([0], [0], color=_TP2_COLOR, linewidth=1.0,
                                     linestyle="--", label="TP2"))
    if stop_loss_price is not None:
        legend_handles.append(Line2D([0], [0], color=_SL_COLOR, linewidth=1.0,
                                     linestyle="--", label="止损"))

    ax.legend(handles=legend_handles,
              facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3", fontsize=8)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.grid(axis="y", color="#21262d", linewidth=0.5, zorder=1)
    ax.set_xticks([])

    plt.tight_layout()
    image_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(str(image_path), dpi=120, bbox_inches="tight",
                facecolor="#0d1117")
    plt.close(fig)
    logger.info("Trade chart saved: %s", image_path)
    return True


