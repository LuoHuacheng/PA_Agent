#!/usr/bin/env python3
"""账户污染诊断: 找出策略不可能产生的交易.

纯判据在 `pa_agent.feedback.contamination`; 这里只负责读配置、拉成交、打印.

背景 (2026-09-21)
----------------
30 天窗口账户实现 -2065.88U, 策略范围只有 -172.30U —— **90% 的亏损来自两笔违反名义
上限的持仓**, 外加一批从未被策略分析过的品种:

  HEMIUSDT  名义  29,369.77U  净 -1683.91U  未分析 + 超标 5.9x  (2026-08-31 16:58)
  BTCUSDT   名义  18,127.29U  净  -166.94U  超标 3.6x            (2026-08-31 19:28)

配置上限是 `max_notional_usdt * leverage` = 250 * 20 = 5,000U. 两笔的品种/名义都超出
管道可能产生的范围, 而 `records/pending/` 全历史**零个** HEMIUSDT 记录.

用法
----
    python tools/account_contamination.py --days 30
    python tools/account_contamination.py --days 90 --json logs/contamination_90d.json
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from pa_agent.feedback.contamination import (  # noqa: E402 - imports follow the sys.path fix
    contamination_summary,
)

SETTINGS_PATH = ROOT / "config" / "settings.json"
PENDING_DIR = ROOT / "records" / "pending"
CONFIG_SECTION = "binance_usdm_testnet"
TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def analyzed_symbols(pending_dir, *, days=None) -> set:
    """pending 记录里出现过的品种(含不下单的轮次).

    品种从文件名取: 记录名是 `YYYY-MM-DD_HH-MM-SS_SYMBOL_TF.json`, 比解析几千个 JSON
    快得多, 且合约符号里不含下划线.
    """
    cutoff = None
    if days:
        moment = datetime.datetime.now(TZ8) - datetime.timedelta(days=days)
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
    """从生产配置读名义上限与杠杆; 缺文件时按 0 处理(等于不启用该判据)."""
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


def render(report) -> str:
    lines = [
        f"交易腿 {report['legs_total']} 条: 策略可能 {report['clean_count']} 条"
        f" {report['clean_net']:+.2f}U | 不可能 {report['flagged_count']} 条"
        f" {report['flagged_net']:+.2f}U",
        f"账户合计 {report['total_net']:+.2f}U",
    ]
    if report["by_reason"]:
        lines.append("按判据:")
        for reason, stat in sorted(report["by_reason"].items()):
            lines.append(f"  {reason:<22} {stat['count']:>5} 条 {stat['net']:>12.2f}U")
    if report["by_symbol"]:
        lines.append("按品种(不可能部分):")
        for sym, stat in list(report["by_symbol"].items())[:12]:
            lines.append(f"  {sym:<14} {stat['count']:>5} 条 {stat['net']:>12.2f}U")
    worst = (report.get("flagged") or [])[:5]
    if worst:
        lines.append("最大 5 笔:")
        for item in worst:
            reasons = ",".join(item["reasons"])
            lines.append(
                f"  {item.get('sym')} 名义 {item['notional']:>12.2f}U"
                f" 净 {item['net']:>10.2f}U  {reasons}"
            )
    return chr(10).join(lines)


def load_account_bundle(days: int) -> dict:
    """拉账户 payload 并 LIFO 配对, 返回体检需要的全部原料.

    比只要 legs 多给 orders/fills/symbols/窗口边界 —— 归因窗口(cid_visible_from)
    要用 orders, 覆盖率要用 income, 分开拉会重复请求. """
    from _pa_sim_common import fetch_account_data, make_client, window_bounds

    from pa_agent.trading.replay_pairing import rebuild_trades

    start_ms, end_ms = window_bounds(days)
    _, client = make_client()
    income = client.income_history(start_ms=start_ms)
    syms = sorted({str(r.get("symbol")) for r in income if r.get("symbol")})
    cache = Path(tempfile.mkdtemp())
    fetch_account_data(client, syms, start_ms, cache)
    with open(cache / "orders.json", encoding="utf-8") as fh:
        orders = json.load(fh)
    with open(cache / "user_trades.json", encoding="utf-8") as fh:
        fills = json.load(fh)
    return {
        "legs": rebuild_trades(orders, fills, syms),
        "income": income,
        "orders": orders,
        "fills": fills,
        "symbols": syms,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def load_account_legs(days: int):
    """向后兼容: 只要 (legs, income)."""
    bundle = load_account_bundle(days)
    return bundle["legs"], bundle["income"]


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
    report = contamination_summary(
        legs,
        analyzed=analyzed,
        ceiling=bounds["notional_ceiling"],
        include_flagged=True,
    )
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
