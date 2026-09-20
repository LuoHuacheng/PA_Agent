#!/usr/bin/env python3
"""Phase 0 门控: 「K2 没被标为新增」是否被模型系统性忽略.

背景
----
修复前监控增量提示词把「新增已收盘K线」恒报为 1, 而每轮实际进 2 根 30m 棒
(见 b805207)。要看这次标注修复值不值得花 LLM 调用做 A/B, 先做一个零成本门控:
如果模型本来就会对"有信息量的 K2"作出反应(60 根全表里 K2 始终在场), 那标注
缺失的代价约等于 0。

口径
----
- 样本: 白名单 30m 目标、走增量路径(incremental_delta 存在)、且与前一条记录
  连续(锚点仍在窗口内、新棒数恰为 2)的轮次。
- 自变量(主): K2 实体 / ATR14 >= 0.5; 稳健性: 破前 20 根高低点 / 与 K1 反向 /
  outside bar。
- 因变量(主): 相对上一轮 stage1_diagnosis.direction 是否改变。
- 判定阈值 15pp 与样本下限见 docs/superpowers/specs/2026-09-20-k2-new-bar-attribution-design.md
  (预注册, 跑数前冻结)。

用法
----
    python tools/k2_materiality_gate.py                    # 近 5 天
    python tools/k2_materiality_gate.py --days 7 --out logs/k2_gate.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TZ8 = timezone(timedelta(hours=8))
PENDING_DIR = ROOT / "records" / "pending"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "ZECUSDT")
TRADABLE_ORDER_TYPES = ("限价单", "突破单", "市价单")

#: 预注册阈值(pp)与每层最小样本数, 见设计文档。
GATE_THRESHOLD_PP = 15.0
MIN_STRATUM_N = 20
MATERIAL_BODY_ATR = 0.5
EXIT_SEQ = 2  # 相对上一轮应有的新棒数(60 分钟档 / 30m 棒)


def _f(value: object) -> float | None:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def atr14(bars: list[dict], period: int = 14) -> float | None:
    """平均真实波幅(简单均值, 最近 *period* 根)。

    入参棒按最新在前(记录里的 kline_data 顺序); 内部翻成最旧在前, 与
    tools/shadow_replay.atr_pct_from_bars 完全同口径(TR 用*更旧*一根的收盘价),
    这样门控里的 0.5 ATR 阈值与回放里的 min_stop_atr 是同一个尺子。
    """
    if len(bars) < period + 1:
        return None
    asc = list(reversed(bars))
    trs: list[float] = []
    for i in range(1, len(asc)):
        h = _f(asc[i].get("high"))
        low = _f(asc[i].get("low"))
        pc = _f(asc[i - 1].get("close"))
        if h is None or low is None or pc is None:
            return None
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    window = trs[-period:]
    return sum(window) / len(window) if window else None


def k2_materiality(
    k1: dict, k2: dict, *, atr: float | None, prev_bars: list[dict]
) -> dict[str, object]:
    """K2(次新已收盘棒, 就是当年没被标成新增的那根)的信息量标签。"""
    o = _f(k2.get("open")) or 0.0
    h = _f(k2.get("high")) or 0.0
    low = _f(k2.get("low")) or 0.0
    c = _f(k2.get("close")) or 0.0
    body_atr = (abs(c - o) / atr) if atr else 0.0
    highs = [x for x in (_f(b.get("high")) for b in prev_bars or []) if x is not None]
    lows = [x for x in (_f(b.get("low")) for b in prev_bars or []) if x is not None]
    breaks_prev = bool(highs) and bool(lows) and (h > max(highs) or low < min(lows))
    s1 = (_f(k1.get("close")) or 0.0) - (_f(k1.get("open")) or 0.0)
    s2 = c - o
    opposite = (s1 > 0 > s2) or (s1 < 0 < s2)
    outside = h > (_f(k1.get("high")) or 0.0) and low < (_f(k1.get("low")) or 0.0)
    return {
        "body_atr": round(body_atr, 4),
        "material_body": body_atr >= MATERIAL_BODY_ATR,
        "breaks_prev": breaks_prev,
        "opposite": opposite,
        "outside": outside,
    }


def _plan_signature(inner: dict) -> tuple:
    return (
        str(inner.get("order_type") or ""),
        str(inner.get("order_direction") or ""),
        inner.get("entry_price"),
        inner.get("stop_loss_price"),
        inner.get("take_profit_price"),
    )


def _tradable(inner: dict) -> bool:
    return str(inner.get("order_type") or "") in TRADABLE_ORDER_TYPES


def _confidence(inner: dict) -> int | None:
    try:
        return int(float(str(inner.get("trade_confidence"))))
    except (TypeError, ValueError):
        return None


def reaction(
    prev_diag: dict, prev_dec: dict, diag: dict, dec: dict
) -> dict[str, object]:
    """本轮相对上一轮的模型反应(诊断 + 决策两个层面)。"""
    prev_diag = prev_diag or {}
    diag = diag or {}
    prev_inner = ((prev_dec or {}).get("decision") or {})
    inner = ((dec or {}).get("decision") or {})
    pc, c = _confidence(prev_inner), _confidence(inner)
    plan_now = _tradable(inner)
    if not _tradable(prev_inner) and not plan_now:
        plan_changed: bool | None = None
    else:
        plan_changed = _plan_signature(prev_inner) != _plan_signature(inner)
    return {
        "direction_changed": str(prev_diag.get("direction") or "")
        != str(diag.get("direction") or ""),
        "cycle_changed": str(prev_diag.get("cycle_position") or "")
        != str(diag.get("cycle_position") or ""),
        "conf_delta": (c - pc) if (pc is not None and c is not None) else None,
        "plan_now": plan_now,
        "plan_changed": plan_changed,
    }


def wilson_ci(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson 区间(默认 90%)。"""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom))


def gate_verdict(
    *, material_rate: float, immaterial_rate: float, material_n: int, immaterial_n: int
) -> dict[str, object]:
    """预注册判定: 材料层与非材料层的反应率差 >= 15pp 视为"模型已在用 K2"。"""
    gap = round((material_rate - immaterial_rate) * 100, 1)
    if min(material_n, immaterial_n) < MIN_STRATUM_N:
        verdict = "insufficient_sample"
    elif gap >= GATE_THRESHOLD_PP:
        verdict = "already_used_k2"
    else:
        verdict = "worth_ab"
    return {"reaction_gap_pp": gap, "verdict": verdict}


def _record_symbol(path: Path) -> str:
    return path.name.rsplit("_", 2)[-2]


def load_rounds(
    *, days: float, symbols: tuple[str, ...], pending_dir: Path | None = None
) -> list[dict[str, object]]:
    """收集配对轮次: 本轮 + 同品种上一条记录(不作时间窗过滤)。"""
    root = pending_dir or PENDING_DIR
    cutoff = datetime.now(tz=TZ8) - timedelta(days=days)
    paths: dict[str, list[Path]] = defaultdict(list)
    for path in root.glob("*_30m.json"):
        sym = _record_symbol(path)
        if sym in symbols:
            paths[sym].append(path)
    rounds: list[dict[str, object]] = []
    for sym, items in paths.items():
        items.sort(key=lambda p: p.name)
        prev: dict | None = None
        prev_path: Path | None = None
        for path in items:
            try:
                cur = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if prev is not None and prev_path is not None:
                rounds.append({
                    "symbol": sym, "path": path, "prev_path": prev_path,
                    "cur": cur, "prev": prev,
                })
            prev, prev_path = cur, path
    out: list[dict[str, object]] = []
    for item in rounds:
        cur = item["cur"]
        prev = item["prev"]
        meta = cur.get("meta") or {}
        stamp = meta.get("timestamp_local_ms")
        if not stamp or datetime.fromtimestamp(stamp / 1000, TZ8) < cutoff:
            continue
        diag = cur.get("stage1_diagnosis") or {}
        if not diag.get("incremental_delta"):
            continue  # 非增量轮次
        bars = cur.get("kline_data") or []
        prev_bars = (prev.get("kline_data") or [])
        if len(bars) < 22 or not prev_bars:
            continue
        anchor = prev_bars[0].get("ts_open")
        ts = [b.get("ts_open") for b in bars]
        if anchor not in ts:
            continue
        if len([t for t in ts if t is not None and anchor is not None and t > anchor]) != EXIT_SEQ:
            continue  # 只取稳态节拍(每轮 2 根)以保持处理一致
        atr = atr14(bars)
        mat = k2_materiality(bars[0], bars[1], atr=atr, prev_bars=bars[2:22])
        react = reaction(
            prev.get("stage1_diagnosis") or {},
            prev.get("stage2_decision") or {},
            diag,
            cur.get("stage2_decision") or {},
        )
        out.append({"meta": meta, "atr": atr, **mat, **react,
                    "path": str(item["path"]), "prev_path": str(item["prev_path"]),
                    "cur": cur, "prev": prev, "anchor": anchor})
    return out


def _rate(rows: list[dict], key: str) -> tuple[int, int]:
    vals = [bool(r[key]) for r in rows if r.get(key) is not None]
    return sum(1 for v in vals if v), len(vals)


def _stratum_table(rows: list[dict], flag: str) -> list[dict[str, object]]:
    table = []
    for label, subset in (
        ("material", [r for r in rows if r.get(flag)]),
        ("immaterial", [r for r in rows if not r.get(flag)]),
    ):
        hits, n = _rate(subset, "direction_changed")
        lo, hi = wilson_ci(hits, n)
        table.append({"stratum": label, "n": n, "direction_changed": hits,
                      "rate": (hits / n if n else 0.0),
                      "ci90": (round(lo, 3), round(hi, 3))})
    return table


def _print_table(rows: list[dict], flag: str, title: str) -> dict[str, object]:
    table = _stratum_table(rows, flag)
    print(f"\n=== {title} ===")
    print("  层           n    方向改变   反应率   90%CI")
    for row in table:
        print("  %-10s %4d %6d %8.1f%%   [%.2f, %.2f]" % (
            row["stratum"], row["n"], row["direction_changed"], row["rate"] * 100,
            row["ci90"][0], row["ci90"][1]))
    return gate_verdict(
        material_rate=table[0]["rate"], immaterial_rate=table[1]["rate"],
        material_n=table[0]["n"], immaterial_n=table[1]["n"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=5.0)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--out", type=Path, default=ROOT / "logs" / "k2_materiality_gate.csv")
    args = ap.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    rows = load_rounds(days=args.days, symbols=symbols)
    print(f"配对轮次: {len(rows)}  (近 {args.days:g} 天, 品种 {', '.join(symbols)})")
    if not rows:
        print("没有可用轮次: 检查记录是否走了增量路径且节拍为每轮 2 根")
        return 1
    per_symbol = Counter(str(r["meta"].get("symbol")) for r in rows)
    print("按品种:", dict(sorted(per_symbol.items())))
    verdict = _print_table(rows, "material_body", "主指标: K2 实体 >= 0.5 ATR")
    for flag, title in (("breaks_prev", "稳健性: K2 破前 20 根高低点"),
                        ("opposite", "稳健性: K2 与 K1 反向"),
                        ("outside", "稳健性: K2 outside bar")):
        _print_table(rows, flag, title)
    plan_hits, plan_n = _rate(rows, "plan_changed")
    conf = [r["conf_delta"] for r in rows if r.get("conf_delta") is not None]
    print("\n=== 其他因变量 ===")
    print("  计划实质变化率: %d/%d = %.1f%%" % (plan_hits, plan_n,
                                               (plan_hits / plan_n * 100 if plan_n else 0)))
    if conf:
        print("  trade_confidence 相对上轮变化均值: %+.2f" % (sum(conf) / len(conf)))
    print("\n=== 预注册判定 ===")
    print("  反应率差: %+.1fpp (阈值 >= %.0fpp)" % (verdict["reaction_gap_pp"],
                                                 GATE_THRESHOLD_PP))
    print("  判定:", verdict["verdict"])
    print("  含义:", {
        "already_used_k2": "模型本就在用 K2 —— 只跑 L1 确认, 不扩到 L2",
        "worth_ab": "存在 K2 被忽略的迹象 —— 值得跑 L1",
        "insufficient_sample": "样本不足 —— 加大 --days 再判",
    }[str(verdict["verdict"])])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "record_ms", "atr", "body_atr", "material_body", "breaks_prev",
              "opposite", "outside", "direction_changed", "cycle_changed", "conf_delta",
              "plan_now", "plan_changed"]
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({"symbol": row["meta"].get("symbol"),
                             "record_ms": row["meta"].get("timestamp_local_ms"), **row})
    print("明细:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
