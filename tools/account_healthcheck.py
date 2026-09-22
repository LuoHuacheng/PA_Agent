#!/usr/bin/env python3
"""账户体检: 一条命令给出全账户口径, 防止拿账户级盈亏评价策略.

起因 (2026-09-21)
----------------
90 天窗口账户实现 -2561.02U, 而策略自己只有 **-7.18U / 1749 条腿** —— 99.7% 的"亏损"
来自另一套交易(8 笔超名义上限 + 352 条策略外腿). 只看账户数字会得出完全相反的结论.
所以把四件事串成一次输出:

  1. **污染诊断** —— 哪些腿不可能是本策略产生的(未分析品种 / 超名义上限);
  2. **归因分层** —— matched / manual / missing_decision / ambiguous, 以及 manual 里
     "订单历史过期"与"真外部单"的拆分(交易所订单只回溯约一周);
  3. **归因覆盖率** —— 全部窗口 vs 订单可见窗口(后者才是能持续变好的指标);
  4. **账户真相** —— income 流水(去重后) vs 策略范围 vs 策略外.

用法
----
    python tools/account_healthcheck.py --days 90
    python tools/account_healthcheck.py --days 30 --json logs/healthcheck_30d.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from account_contamination import (  # noqa: E402 - imports follow the sys.path fix
    SETTINGS_PATH,
    analyzed_symbols,
    load_account_bundle,
    strategy_bounds,
)

from pa_agent.feedback.contamination import contamination_summary  # noqa: E402
from pa_agent.feedback.outcome_store import (  # noqa: E402
    account_coverage,
    build_outcome_rows,
    load_csv_fallback,
    load_decision_records,
    prefer_pending_over_csv,
)
from pa_agent.trading.replay_pairing import attach_decisions  # noqa: E402

PENDING_DIR = ROOT / "records" / "pending"
CSV_DIR = ROOT / "trade_records"


def collect(
    days: int,
    *,
    pending_dir=PENDING_DIR,
    csv_dir=CSV_DIR,
    settings_path=SETTINGS_PATH,
    bundle: dict | None = None,
) -> dict:
    """跑完四项口径, 返回一份可直接落盘的体检报告."""
    data = bundle or load_account_bundle(days)
    analyzed = analyzed_symbols(pending_dir, days=days)
    bounds = strategy_bounds(settings_path)
    income = data["income"]
    income_symbols = sorted({str(r.get("symbol") or "") for r in income if r.get("symbol")})
    scope = sorted(set(income_symbols) | analyzed)
    pending_rows = load_decision_records(pending_dir, symbols=scope, days=days)
    csv_rows = load_csv_fallback(csv_dir, symbols=scope, days=days)
    decision_rows = prefer_pending_over_csv(pending_rows, csv_rows)
    attached, attach_audit = attach_decisions(data["legs"], decision_rows)
    rows = build_outcome_rows(attached, decision_rows, data["orders"])
    coverage = account_coverage(
        income,
        attached,
        rows,
        strategy_symbols=sorted(analyzed),
        orders_by_symbol=data["orders"],
    )
    contamination = contamination_summary(
        data["legs"],
        analyzed=analyzed,
        ceiling=bounds["notional_ceiling"],
    )
    return {
        "days": days,
        "bounds": bounds,
        "analyzed_symbols": sorted(analyzed),
        "income_symbols": income_symbols,
        "attach_audit": {k: v for k, v in attach_audit.items() if k != "ambiguous_rows"},
        "coverage": coverage,
        "contamination": contamination,
        "outcome_rows": len(rows),
    }


def verdict(report: dict) -> list:
    """把数字翻成可执行的判定, 而不是只丢一堆表."""
    out = []
    con = report.get("contamination") or {}
    cov = report.get("coverage") or {}
    total = abs(float(con.get("total_net") or 0.0))
    flagged = abs(float(con.get("flagged_net") or 0.0))
    if total > 0 and flagged / total >= 0.5:
        out.append(
            f"账户级盈亏不可用于评价策略: 不可能部分占 {flagged / total:.0%}"
            f" ({con.get('flagged_count')} 条 {con.get('flagged_net'):+.2f}U)"
        )
    clean_net = float(con.get("clean_net") or 0.0)
    clean_count = int(con.get("clean_count") or 0)
    out.append(f"策略可能范围净额 {clean_net:+.2f}U / {clean_count} 条腿 —— 要评价策略只看这一行")
    strat = cov.get("strategy_income_net")
    if strat is not None and abs(float(strat) - clean_net) > max(1.0, abs(clean_net) * 0.1):
        out.append(
            f"注意: 按品种算的'策略范围 income'是 {float(strat):+.2f}U, 与上面不同 ——"
            " 差额来自策略品种上的超标单, 它们不该计入策略"
        )
    win = cov.get("window_trade_coverage")
    if win is not None and win < 0.9:
        out.append(f"订单可见窗口内归因率只有 {win:.0%}, 先查挂单账本再谈归因")
    expired = cov.get("expired_cid_count") or 0
    if expired:
        out.append(f"{expired} 条 manual 是订单历史过期(不是人工单), 别读成手输")
    if cov.get("net_coverage") is None:
        out.append("策略范围 income 为 0, 净额覆盖率不可算")
    return out


def render(report: dict) -> str:
    con = report.get("contamination") or {}
    cov = report.get("coverage") or {}
    audit = report.get("attach_audit") or {}
    lines = [f"=== 账户体检 ({report.get('days')} 天) ==="]
    lines.append(
        f"本金口径: 抓取品种 {len(report.get('income_symbols') or [])} 个,"
        f" 其中被分析过 {len(report.get('analyzed_symbols') or [])} 个"
    )
    lines.append(f"名义上限: {(report.get('bounds') or {}).get('notional_ceiling', 0.0):.0f}U")
    lines.append("")
    lines.append("[1] 污染诊断")
    lines.append(
        f"    策略可能 {con.get('clean_count', 0)} 条 {con.get('clean_net', 0.0):+.2f}U"
        f" | 不可能 {con.get('flagged_count', 0)} 条 {con.get('flagged_net', 0.0):+.2f}U"
    )
    for reason, stat in sorted((con.get("by_reason") or {}).items()):
        lines.append(f"      {reason:<20} {stat['count']:>5} 条 {stat['net']:>12.2f}U")
    lines.append("[2] 归因分层")
    for key in ("matched", "manual", "missing_decision", "ambiguous"):
        lines.append(f"      {key:<20} {audit.get(key, 0):>5} 条")
    lines.append(
        f"      订单历史过期 {cov.get('expired_cid_count', 0):>5} 条 {cov.get('expired_cid_net', 0.0):>12.2f}U"
    )
    lines.append(
        f"      真外部单     {cov.get('external_count', 0):>5} 条 {cov.get('external_net', 0.0):>12.2f}U"
    )
    lines.append("[3] 归因覆盖率")
    lines.append(
        f"      第1层(全部窗口)     {cov.get('trades_matched', 0)}/{cov.get('trades_total', 0)}"
    )
    lines.append(
        f"      第2层(订单可见窗口) {cov.get('window_trades_matched', 0)}/{cov.get('window_trades_total', 0)}"
    )
    lines.append("[4] 账户真相 (income)")
    lines.append(f"      全账户     {cov.get('account_income_net', 0.0):>12.2f}U")
    lines.append(f"      策略范围   {cov.get('strategy_income_net', 0.0):>12.2f}U")
    lines.append(f"      策略外     {cov.get('off_scope_net', 0.0):>12.2f}U")
    worst = sorted((cov.get("off_scope_symbols") or {}).items(), key=lambda kv: kv[1])[:5]
    if worst:
        lines.append("      策略外最大 5 项: " + ", ".join(f"{s}={v:.2f}U" for s, v in worst))
    lines.append("")
    lines.append("=== 判定 ===")
    for item in verdict(report):
        lines.append("  - " + item)
    return chr(10).join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", default="", help="把完整报告写到该路径")
    args = ap.parse_args()
    report = collect(args.days)
    print(render(report))
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        print()
        print("明细:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
