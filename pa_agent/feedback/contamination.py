"""账户污染判据: 策略不可能产生的交易腿. 纯函数, 不碰 IO.

背景与实测见 `tools/account_contamination.py` 和
`docs/账户污染诊断-2026-09-21.md`. 30 天窗口里账户实现 -2065.88U, 其中 90% 来自两笔
明显不是管道下的单. 判据两条, 任一命中即标记:

  1. `unanalyzed_symbol` —— 该品种在 pending 记录里从未出现过, 管道没做过决策;
  2. `oversized_notional` —— 单笔名义超过 `max_notional_usdt * leverage`.

只判"品种是否在白名单"不够: 实测还有一笔 BTCUSDT 名义 18,127U(上限 5,000U 的 3.6 倍),
品种完全在策略范围内, 只有名义判据抓得到.
"""

from __future__ import annotations

from collections import defaultdict

#: 名义上限的容差, 避免浮点边界误判.
NOTIONAL_TOLERANCE = 1.001


def leg_notional(leg: dict) -> float:
    """单笔成交腿的名义金额; qty 或 price 缺失时返回 0."""
    try:
        return abs(float(leg.get("qty") or 0.0) * float(leg.get("entry") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def leg_net(leg: dict) -> float:
    """单笔成交腿的已实现盈亏加手续费."""
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


def contamination_summary(legs, *, analyzed=None, ceiling=0.0, include_flagged=False) -> dict:
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
    by_reason: dict = defaultdict(lambda: {"count": 0, "net": 0.0})
    by_symbol: dict = defaultdict(lambda: {"count": 0, "net": 0.0})
    for item in flagged:
        for reason in item["reasons"]:
            by_reason[reason]["count"] += 1
            by_reason[reason]["net"] += item["net"]
        sym = str(item.get("sym") or "")
        by_symbol[sym]["count"] += 1
        by_symbol[sym]["net"] += item["net"]
    summary = {
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
        "analyzed_symbols": sorted(analyzed),
    }
    if include_flagged:
        summary["flagged"] = sorted(flagged, key=lambda x: x["net"])
    return summary


#: 兼容旧名 (tools 里的 CLI 与测试先用了 contamination_report).
contamination_report = contamination_summary
