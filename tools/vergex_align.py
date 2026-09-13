"""VergeX 成交序列与 Binance K 线对齐分析: 反推决策粒度/止损距离/持仓周期.

读取 tools.vergex_loader.load_dump 产出的成对成交, 结合 K 线缓存计算:
- bar_close_entry_rate: 入场时间落在上根 K 线收盘后 60s 内的比例
  (>0.8 说明策略按"上根收盘决策"建模);
- entry_offset_hist: 入场相对 K 线边界的偏移分桶;
- stop_dist_atr: 止损行 |entry-exit| / ATR14(入场时点) 的中位数,
  用于反推初始止损的 ATR 倍数; 单策略可能跨品种, ATR 按该笔 symbol 的 K 线算;
- hold_hours_median / monthly_pnl / close_reason 分组等人工判读材料.

CLI:
    uv run python tools/vergex_align.py --slug eth_mid --interval 15m

klines 兼容两种形态: 单品种列表 [(ts,o,h,l,c),...] 或
{SYMBOL: [[ts,o,h,l,c],...]} 字典(fetch_klines 落盘的原生格式, 取前 5 列).
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# make repo-root importable when run as a script from anywhere
HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tools.vergex_loader import VergexDump, load_dump  # noqa: E402  (sys.path bootstrap above)

TZ8 = timezone(timedelta(hours=8))
BAR_MS_DEFAULT = 900_000
_PARTIAL_TP_PREFIX = "partial tp"

# ---------- klines normalization


def _coerce_rows(rows) -> list[tuple]:
    """K 线行归一化为 (ts, o, h, l, c): 取前 5 列, 元组行直接透传."""
    if not rows:
        return []
    if isinstance(rows[0], tuple):
        return list(rows)
    return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def _normalize(klines) -> tuple[dict[str, list[tuple]], list[tuple] | None]:
    """返回 ({SYMBOL: rows}, None) 或 ({}, 单品种 rows)."""
    if isinstance(klines, dict):
        return {str(sym).upper(): _coerce_rows(rows) for sym, rows in klines.items()}, None
    return {}, _coerce_rows(klines)


# ---------- ATR


def _tr_at_index(kl: list[tuple], i: int) -> float:
    """第 i 根 TR: max(h-low, |h-prev_c|, |low-prev_c|); 首根无前收则 h-low."""
    _ts, _o, h, low, _c = kl[i]
    if i > 0:
        pc = kl[i - 1][4]
        return max(h - low, abs(h - pc), abs(low - pc))
    return h - low


def _atr_at(klines, ts_ms, period=14) -> float | None:
    """ts_ms 之前(含)最近 period 根 TR 均值; 根数不足或 ATR<=0 返回 None."""
    kl = _coerce_rows(klines)
    if not kl or period <= 0:
        return None
    j = bisect.bisect_right([bar[0] for bar in kl], ts_ms) - 1
    if j + 1 < period:
        return None
    atr = sum(_tr_at_index(kl, i) for i in range(j - period + 1, j + 1)) / period
    return atr if atr > 0 else None


def _atr14(klines) -> float:
    """全局末 14 根 TR 均值(不足 14 根按实际根数); 无法计算返回 0.0."""
    kl = _coerce_rows(klines)
    if len(kl) < 2:
        return 0.0
    atr = _atr_at(kl, kl[-1][0], period=min(14, len(kl)))
    return atr if atr is not None else 0.0


# ---------- close_reason classification


def _is_stop_row(reason: str) -> bool:
    """止损行: 大小写不敏感含 stop/sl/止损, 但排除 Partial TP 分批减仓行."""
    r = reason.strip().lower()
    if r.startswith(_PARTIAL_TP_PREFIX):
        return False
    return "stop" in r or "sl" in r or "止损" in r


def _group_reason(reason: str) -> str:
    """归类: 止损类 / 止盈类(take_profit|止盈) / 指标平仓类(其余)."""
    if _is_stop_row(reason):
        return "止损类"
    r = reason.strip().lower()
    if "take_profit" in r or "止盈" in reason:
        return "止盈类"
    return "指标平仓类"


# ---------- report core


def _offset_bucket(offset_ms: int) -> str:
    if offset_ms < 60_000:
        return "<60s"
    if offset_ms < 300_000:
        return "60-300s"
    return "300-900s"


def _month_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=TZ8).strftime("%Y-%m")


def align_report(dump: VergexDump, klines, *, bar_ms=BAR_MS_DEFAULT) -> dict:
    """成交-K 线对齐指标(键与含义见模块 docstring)."""
    by_sym, single = _normalize(klines)

    def series_for(sym: str) -> list[tuple]:
        return single if single is not None else by_sym.get(sym, [])

    n = len(dump.trades)
    close_cnt: Counter[str] = Counter()
    group_cnt: Counter[str] = Counter()
    offset_hist: Counter[str] = Counter()
    on_boundary = 0
    hold_hours = []
    monthly: dict[str, float] = {}
    stop_dists = []
    for t in dump.trades:
        close_cnt[t.close_reason] += 1
        group_cnt[_group_reason(t.close_reason)] += 1
        offset = t.open_ms % bar_ms
        if offset < 60_000:
            on_boundary += 1
        offset_hist[_offset_bucket(offset)] += 1
        hold_hours.append(t.holding_s / 3600)
        month = _month_key(t.open_ms)
        monthly[month] = monthly.get(month, 0.0) + t.pnl_net
        if _is_stop_row(t.close_reason):
            atr = _atr_at(series_for(t.symbol), t.open_ms)
            if atr is not None:
                stop_dists.append(abs(t.entry - t.exit) / atr)

    return {
        "bar_close_entry_rate": on_boundary / n if n else 0.0,
        "entry_offset_hist": dict(sorted(offset_hist.items())),
        "stop_dist_atr": statistics.median(stop_dists) if stop_dists else None,
        "hold_hours_median": statistics.median(hold_hours) if hold_hours else 0.0,
        "monthly_pnl": dict(sorted(monthly.items())),
        "close_reason_counts": dict(close_cnt.most_common()),
        "close_reason_groups": {
            "止损类": group_cnt.get("止损类", 0),
            "止盈类": group_cnt.get("止盈类", 0),
            "指标平仓类": group_cnt.get("指标平仓类", 0),
        },
        "win_rate": dump.win_rate,
        "n": n,
    }


# ---------- markdown rendering


def _fmt(v: float | None, nd=3) -> str:
    return "N/A" if v is None else f"{v:.{nd}f}"


def _verdict(rep: dict) -> list[str]:
    """固定句式判读段."""
    out = []
    rate = rep["bar_close_entry_rate"]
    if rate > 0.8:
        out.append(
            f"- bar_close_entry_rate={rate:.3f} > 0.8: 入场紧贴上根 K 线收盘, "
            "建模时按上根收盘决策生成器建模。"
        )
    else:
        out.append(
            f"- bar_close_entry_rate={rate:.3f} <= 0.8: 入场不固定在收盘后 60s 内, "
            "建模时按信号触发时点(盘中)生成器处理。"
        )
    sda = rep["stop_dist_atr"]
    if sda is not None and sda > 0:
        out.append(
            f"- stop_dist_atr 中位数={sda:.2f} ATR: 建议初始止损设为 "
            f"{max(0.1, round(sda * 0.75, 2))}~{round(sda * 1.25, 2)} ATR。"
        )
    else:
        out.append("- stop_dist_atr 无样本(无止损行或 K 线不足), 无法反推止损 ATR 倍数。")
    hold = rep["hold_hours_median"]
    if hold < 1:
        band = "分钟级日内短线, 建模持仓周期按分钟级校准"
    elif hold < 8:
        band = "日内波段(小时级), 建模持仓周期按小时级校准"
    elif hold < 48:
        band = "隔夜短线(1-2 日), 建模持仓周期按日级校准"
    else:
        band = "多日趋势持仓, 建模持仓周期按多日校准"
    out.append(f"- hold_hours_median={hold:.2f}h: 属{band}。")
    return out


def _markdown(dump: VergexDump, rep: dict, interval: str) -> str:
    lines = [
        f"# VergeX 对齐报告: {dump.slug} ({dump.name})",
        "",
        f"- K 线周期: {interval} (bar_ms={BAR_MS_DEFAULT})",
        f"- 成交笔数: {rep['n']}",
        "",
        "## 指标",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| bar_close_entry_rate | {_fmt(rep['bar_close_entry_rate'])} |",
        f"| stop_dist_atr (中位数) | {_fmt(rep['stop_dist_atr'])} |",
        f"| hold_hours_median | {_fmt(rep['hold_hours_median'])} |",
        f"| win_rate | {_fmt(rep['win_rate'])} |",
        f"| n | {rep['n']} |",
        "",
        "入场偏移分桶(open_ms % bar_ms):",
        "",
    ]
    for bucket in ("<60s", "60-300s", "300-900s"):
        lines.append(f"- {bucket}: {rep['entry_offset_hist'].get(bucket, 0)}")
    lines += [
        "",
        "## 平仓原因分组",
        "",
        "| 分组 | 笔数 |",
        "| --- | --- |",
    ]
    for grp in ("止损类", "止盈类", "指标平仓类"):
        lines.append(f"| {grp} | {rep['close_reason_groups'].get(grp, 0)} |")
    lines += ["", "## 月度盈亏 (UTC+8, pnl_net 合计)", "", "| 月份 | pnl_net |", "| --- | --- |"]
    for month, pnl in rep["monthly_pnl"].items():
        lines.append(f"| {month} | {pnl:+.2f} |")
    lines += ["", "## Top5 open_reason", ""]
    top_opens = Counter(t.open_reason for t in dump.trades).most_common(5)
    if not top_opens:
        lines.append("(无)")
    for reason, cnt in top_opens:
        lines.append(f"- [{cnt} 次] {reason}")
    lines += ["", "## 判读", "", *_verdict(rep), ""]
    return "\n".join(lines)


# ---------- CLI


def _interval_ms(interval: str) -> int:
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    s = interval.strip().lower()
    if s and s[-1] in mult and s[:-1].isdigit():
        return int(s[:-1]) * mult[s[-1]]
    return BAR_MS_DEFAULT


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", required=True, help="strategy slug, e.g. eth_mid")
    ap.add_argument("--interval", default="15m", help="kline interval (default 15m)")
    ap.add_argument("--cache-dir", default=str(HERE / "research/vergex/data/_klines"),
                    help="dir containing klines.json from fetch_klines")
    ap.add_argument("--data-dir", default=str(HERE / "research/vergex/data"),
                    help="dir containing <slug>/detail.json")
    args = ap.parse_args()

    kpath = Path(args.cache_dir) / "klines.json"
    if not kpath.exists():
        raise SystemExit(f"klines cache missing: {kpath} (run fetch_klines first)")
    klines = json.loads(kpath.read_text(encoding="utf-8"))
    dump = load_dump(Path(args.data_dir) / args.slug / "detail.json", slug=args.slug)
    rep = align_report(dump, klines, bar_ms=_interval_ms(args.interval))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    out = HERE / "research" / "vergex" / f"align_{args.slug}.md"
    out.write_text(_markdown(dump, rep, args.interval), encoding="utf-8")
    print(f"report written: {out}")


if __name__ == "__main__":
    main()
