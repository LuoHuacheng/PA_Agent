#!/usr/bin/env python3
"""Phase 2: K2 A/B 配对分析(0 次调用).

读 logs/k2_ab_pilot.jsonl(试点原始回复) + 原始记录, 对三条胳膊做:
  决策差量(计划是否实质变化) / 影子结局(同 timeline 跑 simulate_plan) /
  gross-fees 拆解 / 操纵检查, 并按设计文档的预注册规则给判定。

用法:
    python tools/k2_ab_analyse.py
    python tools/k2_ab_analyse.py --jsonl logs/k2_ab_pilot.jsonl --days 5
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from k2_materiality_gate import wilson_ci  # noqa: E402
from shadow_replay import (  # noqa: E402
    KLINE_EPOCH_OFFSET_MS,
    Plan,
    _num,
    bootstrap_mean_ci,
    load_plans,
    merge_timelines,
    simulate_plan,
)

PENDING_DIR = ROOT / "records" / "pending"
DEFAULT_JSONL = ROOT / "logs" / "k2_ab_pilot.jsonl"

TRADABLE = ("限价单", "突破单", "市价单")

#: 与生产一致的模拟参数(设计文档 §4 Phase 2)。
PROD_PARAMS = dict(
    risk_usdt=4.0, fee_rate=0.0006, leverage=20, margin_usdt=250.0,
    partial_pct=50.0, timeout_min=120.0, time_stop_min=720.0,
    min_stop_pct=0.2, min_stop_atr=1.5,
)

_DECISION_FIELDS = (
    "order_type", "order_direction", "entry_price", "stop_loss_price", "take_profit_price",
)


def confidence_bucket(value: object) -> int | None:
    """置信度按 10 分一档(跨档才算实质变化)。"""
    try:
        return int(float(str(value))) // 10
    except (TypeError, ValueError):
        return None


def decision_delta(a: dict, b: dict) -> dict[str, bool]:
    """两条决策的实质差异: 五个可交易字段 + 置信度跨档。"""
    out = {field: a.get(field) != b.get(field) for field in _DECISION_FIELDS}
    out["confidence"] = confidence_bucket(a.get("trade_confidence")) != confidence_bucket(
        b.get("trade_confidence")
    )
    out["changed"] = any(out.values())
    return out


def _plan_from(doc: dict, decision: dict, *, path: str) -> Plan | None:
    """按 shadow_replay.load_plans 的口径, 用指定决策构造计划单。"""
    meta = doc.get("meta") or {}
    order_type = str(decision.get("order_type") or "")
    direction = str(decision.get("order_direction") or "")
    entry = _num(decision.get("entry_price"))
    stop = _num(decision.get("stop_loss_price"))
    target = _num(decision.get("take_profit_price"))
    if order_type not in ("限价单", "突破单") or direction not in ("做多", "做空"):
        return None
    if entry is None or stop is None or target is None or entry <= 0:
        return None
    diag = doc.get("stage1_diagnosis") or {}
    bars = []
    for bar in doc.get("kline_data") or []:
        ts = bar.get("ts_open")
        o, h, low, c = (_num(bar.get(k)) for k in ("open", "high", "low", "close"))
        if not ts or None in (o, h, low, c):
            continue
        bars.append((int(ts) - KLINE_EPOCH_OFFSET_MS, o, h, low, c))
    bars.sort()
    return Plan(
        path=path, symbol=str(meta.get("symbol") or ""),
        timeframe=str(meta.get("timeframe") or ""),
        record_ms=int(meta.get("timestamp_local_ms") or 0),
        order_type=order_type, direction=direction,
        entry=entry, stop=stop, target=target,
        target2=_num(decision.get("take_profit_price_2")),
        cycle_position=str(diag.get("cycle_position") or "unknown"),
        diag_direction=str(diag.get("direction") or "unknown"),
        trade_conf=_num(decision.get("trade_confidence")),
        diag_conf=_num(decision.get("diagnosis_confidence")),
        est_win_rate=_num(decision.get("estimated_win_rate")),
        bars=bars,
    )


def _arm_decision(entry: dict, arm: str) -> dict | None:
    if arm == "A":
        return None  # 由调用方填 stored
    run = entry.get("runs", {}).get(arm) or {}
    stage2 = run.get("stage2") or {}
    parsed = stage2.get("parsed")
    if not isinstance(parsed, dict):
        return None
    decision = parsed.get("decision")
    return decision if isinstance(decision, dict) else None


def _net(outcome: object) -> float:
    return float(getattr(outcome, "net", 0.0) or 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--days", type=float, default=5.0)
    args = ap.parse_args()
    if not args.jsonl.is_file():
        print("找不到试点结果:", args.jsonl)
        return 1
    entries = [json.loads(line) for line in args.jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    print("试点轮次:", len(entries))
    symbols = {str(e.get("symbol")) for e in entries}
    timelines = merge_timelines(load_plans(PENDING_DIR, days=args.days, symbols=symbols))
    print("时间线:", {k[0] + "/" + k[1]: len(v) for k, v in sorted(timelines.items())})

    rounds: list[dict] = []
    for entry in entries:
        path = Path(str(entry.get("path")))
        if not path.is_file():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        stored = ((doc.get("stage2_decision") or {}).get("decision")) or {}
        arms: dict[str, dict] = {"A": stored}
        for arm in ("B", "A_noise#1", "A_noise#2"):
            decision = _arm_decision(entry, arm)
            if decision is not None:
                arms[arm] = decision
        tl = timelines.get((str(doc["meta"].get("symbol")), str(doc["meta"].get("timeframe"))), [])
        outcomes: dict[str, object | None] = {}
        for arm, decision in arms.items():
            plan = _plan_from(doc, decision, path=str(path))
            outcomes[arm] = simulate_plan(plan, tl, **PROD_PARAMS) if plan is not None else None
        b_stage1 = ((entry.get("runs", {}).get("B") or {}).get("stage1") or {}).get("parsed") or {}
        bars_listed = (b_stage1.get("incremental_delta") or {}).get("new_closed_bars")
        rounds.append({
            "symbol": doc["meta"].get("symbol"), "arms": arms, "outcomes": outcomes,
            "b_new_bars": len(bars_listed) if isinstance(bars_listed, list) else None,
        })

    usable = [r for r in rounds if "B" in r["arms"]]
    print(f"可用配对轮次: {len(usable)}/{len(rounds)} (B 臂 stage2 解析成功)")

    print("\n=== 1) 决策差量(主指标) ===")
    changed = [decision_delta(r["arms"]["A"], r["arms"]["B"])["changed"] for r in usable]
    rate = sum(changed) / len(changed) if changed else 0.0
    print(f"  A(stored,1根) vs B(2根): 计划实质变化 {sum(changed)}/{len(changed)} = {rate * 100:.1f}%")
    detail = Counter()
    for r in usable:
        for k, v in decision_delta(r["arms"]["A"], r["arms"]["B"]).items():
            if k != "changed" and v:
                detail[k] += 1
    print("  变化构成:", dict(detail))
    noise_pairs = [(r["arms"]["A"], r["arms"][k]) for r in usable
                   for k in ("A_noise#1", "A_noise#2") if k in r["arms"]]
    if noise_pairs:
        noise_changed = sum(decision_delta(a, b)["changed"] for a, b in noise_pairs)
        nrate = noise_changed / len(noise_pairs)
        lo, hi = wilson_ci(noise_changed, len(noise_pairs))
        print(f"  噪声地板 A vs A'(同输入重跑): {noise_changed}/{len(noise_pairs)} = {nrate * 100:.1f}%  Wilson90 上界 {hi * 100:.1f}%")
    else:
        nrate, hi = 0.0, 0.0
        print("  噪声地板: 无样本(未抽到 noise 轮次)")

    print("\n=== 2) 影子结局(配对) ===")
    def bucket(arm):
        outs = [r["outcomes"].get(arm) for r in usable]
        fill = [o for o in outs if o is not None and getattr(o, "filled", False)]
        return outs, fill
    for arm in ("A", "B"):
        outs, fill = bucket(arm)
        nets = [_net(o) for o in fill]
        gross = sum(float(getattr(o, "gross", 0.0) or 0.0) for o in fill)
        fees = sum(float(getattr(o, "fees", 0.0) or 0.0) for o in fill)
        wins = sum(1 for n in nets if n > 0)
        print(f"  {arm}: 有计划 {sum(1 for o in outs if o is not None)}/{len(usable)}"
              f"  成交 {len(fill)}  净 {sum(nets):+.2f}U  gross {gross:+.2f}U  fees {fees:+.2f}U"
              f"  胜率 {(wins / len(nets) * 100 if nets else 0):.1f}%")
    diffs = [_net(r["outcomes"].get("B") or r["outcomes"].get("A")) - _net(r["outcomes"].get("A"))
             for r in usable if r["outcomes"].get("A") is not None or r["outcomes"].get("B") is not None]
    if diffs:
        lo, hi2 = bootstrap_mean_ci(diffs)
        print(f"  配对净U差(B-A): 均 {sum(diffs) / len(diffs):+.3f}U  90%CI [{lo:+.2f}, {hi2:+.2f}]  合计 {sum(diffs):+.2f}U")
    else:
        lo = hi2 = 0.0

    print("\n=== 3) 操纵检查 ===")
    listed = [r["b_new_bars"] for r in usable if r["b_new_bars"] is not None]
    ok2 = sum(1 for n in listed if n == 2)
    print(f"  B 臂 stage1 写出 new_closed_bars 长度=2 的比例: {ok2}/{len(listed)}"
          f" ({ok2 / len(listed) * 100 if listed else 0:.1f}%)")

    print("\n=== 4) 预注册判定 ===")
    noise_ub = hi * 100
    sig = not (lo <= 0 <= hi2)
    print(f"  条件①计划变更率 {rate * 100:.1f}% > 噪声上界 {noise_ub:.1f}%: {rate * 100 > noise_ub}")
    print(f"  条件②配对净U差 90%CI [{lo:+.2f}, {hi2:+.2f}] 不跨 0: {sig}")
    verdict = "material_effect" if (rate * 100 > noise_ub and sig) else "null"
    print("  判定:", verdict)
    print("  可探测的最小效应(MDE): 约 %.2fU/轮(CI 半宽)" % (abs(hi2 - lo) / 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
