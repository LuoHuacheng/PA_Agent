#!/usr/bin/env python3
"""账户污染诊断: 找出策略不可能产生的交易.

背景 (2026-09-21)
----------------
30 天窗口里账户实现 -1963.64U, 而策略范围只有 -258.89U —— 85% 的亏损来自策略没碰过的
品种. 逐个查实后发现单看"品种不在白名单"还不够: 更硬的一条是**名义仓位超过配置上限**.
实例: 2026-08-31 16:58:47 一笔 HEMIUSDT BUY 1,779,986 @ 0.0165 = 29,370U 名义,
37 分钟后平仓亏 -1654U —— 是 `max_notional_usdt * leverage` = 5,000U 上限的 5.9 倍,
而 `records/pending/` 全历史**零个** HEMIUSDT 记录, 管道从未分析过它.

两条判据(任一命中即标记):
  1. `unanalyzed_symbol` —— 该品种在 pending 记录里从未出现过, 即管道从没做过决策;
  2. `oversized_notional` —— 单笔名义超过 `max_notional_usdt * leverage`.

用法
----
    python tools/account_contamination.py --days 30
    python tools/account_contamination.py --days 30 --json logs/contamination.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

SETTINGS_PATH = ROOT / "config" / "settings.json"
PENDING_DIR = ROOT / "records" / "pending"
CONFIG_SECTION = "binance_usdm_testnet"

#: 名义上限的容差, 避免浮点边界误判.
NOTIONAL_TOLERANCE = 1.001


def analyzed_symbols(pending_dir, *, days=None) -> set:
    """pending 记录里出现过的品种(含不下单的轮次).

    品种从文件名取: 记录名是 `YYYY-MM-DD_HH-MM-SS_SYMBOL_TF.json`, 比解析 5797 个
    JSON 快得多, 且符号里不含下划线.
    """
    import datetime

    cutoff = None
    if days:
        tz8 = datetime.timezone(datetime.timedelta(hours=8))
        moment = datetime.datetime.now(tz8) - datetime.timedelta(days=days)
        cutoff = moment.strftime("%Y-%m-%d")
    out = set()
    for path in glob.glob(str(Path(pending_dir) / "*.json")):
        name = os.path.basename(path)
        if cutoff and name[:10] < cutoff:
            continue
        parts = name[:-5].split("_")
        if len(parts) >= 4:
            out.add(parts[2].upper())
    return out


def strategy_bounds(path=SETTINGS_PATH, *, section=CONFIG_SECTION) -> dict:
    """从生产配置读名义上限与杠杆. 缺文件时按 0 处理(等于不启用该判据)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    cfg = doc.get(section) or {}
    max_notional = float(cfg.get("max_notional_usdt", 0.0) or 0.0)
    leverage = float(cfg.get("leverage", 0.0) or 0.0)
    return {
        "max_notional_usdt": max_notional,
        "leverage": leverage,
        "notional_ceiling": max_notional * leverage,
        "risk_per_trade_usdt": float(cfg.get("risk_per_trade_usdt", 0.0) or 0.0),
    }


def leg_notional(leg: dict) -> float:
    """单笔成交腿的名义金额; qty 或 price 缺失时返回 0."""
    try:
        return abs(float(leg.get("qty") or 0.0) * float(leg.get("entry") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def leg_net(leg: dict) -> float:
    return float(leg.get("realized") or 0.0) + float(leg.get("fees") or 0.0)


def flag_reasons(leg: dict, *, analyzed: set, ceiling: float) -> tuple:
    """该腿为什么不可能来自策略(空元组 = 无法排除)."""
    reasons = []
    sym = str(leg.get("sym") or "").upper()
    if analyzed and sym and sym not in analyzed:
        reasons.append("unanalyzed_symbol")
    if ceiling > 0 and leg_notional(leg) > ceiling * NOTIONAL_TOLERANCE:
        reasons.append("oversized_notional")
    return tuple(reasons)


def contamination_report(legs, *, analyzed=None, ceiling=0.0) -> dict:
    """把交易腿分成"策略可能产生"与"不可能产生"两堆, 各自给笔数与净额."""
    analyzed = {str(s).upper() for s in (analyzed or set())}
    flagged = []
    clean = []
    for leg in legs:
        reasons = flag_reasons(leg, analyzed=analyzed, ceiling=ceiling)
        if reasons:
            item = dict(leg)
            item["reasons"] = reasons
            item["notional"] = round(leg_notional(leg), 2)
            item["net"] = round(leg_net(leg), 4)
            flagged.append(item)
        else:
            clean.append(leg)
    by_reason = defaultdict(lambda: {"count": 0, "net": 0.0})
    for item in flagged:
        for reason in item["reasons"]:
            by_reason[reason]["count"] += 1
            by_reason[reason]["net"] += item["net"]
    by_symbol = defaultdict(lambda: {"count": 0, "net": 0.0})
    for item in flagged:
        sym = str(item.get("sym") or "")
        by_symbol[sym]["count"] += 1
        by_symbol[sym]["net"] += item["net"]
    return {
        "legs_total": len(legs),
        "flagged_count": len(flagged),
        "flagged_net": round(sum(leg_net(x) for x in flagged), 4),
        "clean_count": len(clean),
        "clean_net": round(sum(leg_net(x) for x in clean), 4),
        "total_net": round(sum(leg_net(x) for x in legs), 4),
        "by_reason": {k: dict(v) for k, v in by_reason.items()},
        "by_symbol": {
            k: dict(v) for k, v in sorted(by_symbol.items(), key=lambda kv: kv[1]["net"])
        },
        "flagged": sorted(flagged, key=lambda x: x["net"]),
        "analyzed_symbols": sorted(analyzed),
    }


def render(report) -> str:
    lines = []
    total = report["legs_total"]
    lines.append(
        f"交易腿 {total} 条: 策略可能 {report['clean_count']} 条"
        f" {report['clean_net']:+.2f}U | 不可能 {report['flagged_count']} 条"
        f" {report['flagged_net']:+.2f}U"
    )
    lines.append(f"账户合计 {report['total_net']:+.2f}U")
    if report["by_reason"]:
        lines.append("按判据:")
        for reason, stat in sorted(report["by_reason"].items()):
            lines.append(f"  {reason:<22} {stat['count']:>5} 条 {stat['net']:>12.2f}U")
    if report["by_symbol"]:
        lines.append("按品种(不可能部分):")
        for sym, stat in list(report["by_symbol"].items())[:12]:
            lines.append(f"  {sym:<14} {stat['count']:>5} 条 {stat['net']:>12.2f}U")
    worst = report["flagged"][:5]
    if worst:
        lines.append("最大 5 笔:")
        for item in worst:
            lines.append(
                f"  {item.get('sym')} 名义 {item['notional']:>12.2f}U"
                f" 净 {item['net']:>10.2f}U  {','.join(item['reasons'])}"
            )
    return chr(10).join(lines)


def load_account_legs(days: int):
    """拉账户成交并 LIFO 配对. 依赖 testnet 密钥, 无密钥时抛 SystemExit."""
    import tempfile

    from _pa_sim_common import fetch_account_data, make_client, window_bounds

    from pa_agent.trading.replay_pairing import rebuild_trades

    start_ms, _end = window_bounds(days)
    _, client = make_client()
    income = client.income_history(start_ms=start_ms)
    syms = sorted({str(r.get("symbol")) for r in income if r.get("symbol")})
    cache = Path(tempfile.mkdtemp())
    fetch_account_data(client, syms, start_ms, cache)
    with open(cache / "orders.json", encoding="utf-8") as fh:
        orders = json.load(fh)
    with open(cache / "user_trades.json", encoding="utf-8") as fh:
        fills = json.load(fh)
    return rebuild_trades(orders, fills, syms), income


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", default="", help="把完整报告写到该路径")
    args = ap.parse_args()

    legs, income = load_account_legs(args.days)
    analyzed = analyzed_symbols(PENDING_DIR, days=args.days)
    bounds = strategy_bounds()
    report = contamination_report(legs, analyzed=analyzed, ceiling=bounds["notional_ceiling"])
    report["bounds"] = bounds
    report["income_net"] = round(
        sum(
            float(r.get("income") or 0.0)
            for r in income
            if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION")
        ),
        4,
    )
    print(render(report))
    print()
    print("配置口径:", bounds)
    joined = ", ".join(sorted(analyzed))
    print(f"已分析品种 {len(analyzed)} 个: {joined}")
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("明细:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
