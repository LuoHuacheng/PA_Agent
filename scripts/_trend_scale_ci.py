#!/usr/bin/env python3
"""趋势尺度冲突: 日线标签 vs 决策周期结构 (2026-09-20).

动机: 2026-09-20 16:00/17:00 两轮 BTCUSDT 限价做空被 short_block 拦, 理由是
"下跌宽通道顺势做空" —— 但日线 7 天 +5.76% 偏多。两个尺度给出相反结论。
本脚本用历史样本回答: 按哪种尺度看, 这类空单才是对的?

数据源: records/pending 的 stage1_diagnosis(program_features.swing_structure /
bar_analysis.always_in / trend_context.trading_direction) 与回放 CSV 按 record_file 关联。

用法(仓库根)
    PYTHONPATH=. .venv/bin/python scripts/_trend_scale_ci.py
    PYTHONPATH=. .venv/bin/python scripts/_trend_scale_ci.py --csv logs/shadow_rows_20260920_fix_endpoint.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.shadow_replay import PENDING_DIR, _is_counter_trend, bootstrap_mean_ci  # noqa: E402

DEFAULT_CSV = "logs/shadow_rows_20260920_fix_endpoint.csv"
NEUTRAL = 1.0


def load_decision_context():
    """pending 文件名 -> (swing_structure, always_in, trading_direction)."""
    out = {}
    for path in glob.glob(str(PENDING_DIR / "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            continue
        s1 = doc.get("stage1_diagnosis") or {}
        pf = s1.get("program_features") or {}
        ba = s1.get("bar_analysis") or {}
        tc = s1.get("trend_context") or {}
        out[os.path.basename(path)] = (
            str(pf.get("swing_structure") or "?"),
            str(ba.get("always_in") or "?"),
            str(tc.get("trading_direction") or "?"),
        )
    return out


def show(label, sub, risk=4.0):
    if not sub:
        print("  %-34s %4d" % (label, 0))
        return
    nets = [r["net"] for r in sub]
    if len(nets) >= 2:
        lo, hi = bootstrap_mean_ci(nets)
        ci = "[%+.2f,%+.2f]%s" % (lo, hi, "  全负" if hi < 0 else ("  全正" if lo > 0 else ""))
    else:
        ci = "(n<2, 不给区间)"
    wins = sum(1 for x in nets if x > 0)
    print("  %-34s %4d  胜%2d/%2d  净%+8.2f  均%+6.2f  %s" % (
        label, len(sub), wins, len(sub), sum(nets), sum(nets) / len(sub), ci))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    args = ap.parse_args()

    ctx = load_decision_context()
    with open(args.csv, encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r["filled"] == "True"]
    for r in rows:
        r["net"] = float(r["net"])
        r["trend_pct"] = float(r["trend_pct"]) if r["trend_pct"] else None
    tagged = [r for r in rows if r["record_file"] in ctx]
    print(f"样本: {args.csv} | 成交 {len(rows)} 笔, 其中 {len(tagged)} 笔有阶段一上下文")
    print()

    short = [r for r in tagged if r["direction"] == "做空"]
    long_ = [r for r in tagged if r["direction"] == "做多"]

    print("=== 做空: 日线标签 x 30m 交易方向 (核心交叉) ===")
    for ct in (True, False):
        for td in ("bearish", "neutral", "bullish"):
            sub = [r for r in short
                   if _is_counter_trend(r, NEUTRAL) == ct and ctx[r["record_file"]][2] == td]
            show("空 %s x 30m=%s" % ("逆日线" if ct else "顺日线", td), sub)
    print()

    print("=== 做空: 按 30m 结构 (swing_structure) ===")
    for ss in ("LL+LH", "HH+HL", "mixed", "?"):
        show("空 x 结构=%s" % ss, [r for r in short if ctx[r["record_file"]][0] == ss])
    print()

    print("=== 做空: 按 Always In ===")
    for ai in ("short", "long", "?"):
        show("空 x AIS=%s" % ai, [r for r in short if ctx[r["record_file"]][1] == ai])
    print()

    print("=== 对照: 做多 (同上维度) ===")
    for td in ("bullish", "neutral", "bearish"):
        show("多 x 30m=%s" % td, [r for r in long_ if ctx[r["record_file"]][2] == td])
    for ss in ("HH+HL", "LL+LH", "mixed"):
        show("多 x 结构=%s" % ss, [r for r in long_ if ctx[r["record_file"]][0] == ss])


if __name__ == "__main__":
    main()
