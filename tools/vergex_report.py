"""VergeX 还原度矩阵报告: 平台实盘策略 vs 机械还原规则回放统计对照.

读取 research/vergex/oos_binance.json(Task 5 产出), 对每个平台参考策略取其
还原规则的 full_best(全样本最优组合)统计, 按胜率/笔数容差给出
"达标|超差|样本不足" 判定, 并附滚动样本外(OOS)口径表与不可还原边界说明.

CLI:
    uv run python tools/vergex_report.py --oos research/vergex/oos_binance.json \
        --out research/vergex/还原度矩阵_2026-09-14.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# make repo-root importable when run as a script from anywhere
HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 平台参考数字: 策略 slug -> 名称/还原规则/胜率%/笔数/收益率%/最大回撤%
PLATFORM_REFERENCE = {
    "eth_mid":        {"strategy": "ETH中线策略-稳中求进", "rule": "trend_pullback",
                       "wr": 50.45, "n": 111, "ret": 281.87, "dd": 46.89},
    "btc_zec_rotation": {"strategy": "BTC_ZEC_MultiTF_Rotation", "rule": "dual_supertrend",
                       "wr": 45.79, "n": 653, "ret": 399.32, "dd": 58.91},
    "zec_mom1_5x":    {"strategy": "ZEC_MOM1_5x_50pct", "rule": "momo_break",
                       "wr": 28.37, "n": 282, "ret": 125.40, "dd": 38.63},
    "zec_macd":       {"strategy": "ZEC_MACD", "rule": "momo_break",
                       "wr": 32.65, "n": 49, "ret": 116.68, "dd": 42.22},
    "zec_high_freq":  {"strategy": "ZEC高频趋势追踪大王", "rule": "momo_break",
                       "wr": 41.76, "n": 91, "ret": 689.16, "dd": 87.07},
}

# 不可还原边界(逐字, 固定附于报告尾部)
NON_REPRODUCIBLE_BOUNDARIES = [
    "1. 平台收益含复利/杠杆/资金费,`simulate_plan` 是固定风险 R 口径,只可比胜率与均 R 形态,"
    "收益率仅要求同数量级且单位不同(R 倍数 vs %);",
    "2. 美股代币策略(MRVL/AMD/Silver/GOOGL/PLTR/TELA)无 Binance 数据,排除;",
    "3. TickStrategy 的 tick 级细节在 K 线粒度只能近似;",
    "4. 平台头部数字有幸存者偏差,还原目标是\"同规则同数据段统计形态一致\",不是复刻 +17302%。",
]

TZ8 = timezone(timedelta(hours=8))

WR_TOL = 8.0    # 胜率容差(百分点)
N_TOL = 0.25    # 笔数相对容差
MIN_N = 30      # 回放最小样本数


def verdict(ref_wr, ref_n, rep_wr, rep_n, *, wr_tol=WR_TOL, n_tol=N_TOL) -> str:
    """判据顺序固定: rep_n<30 -> 样本不足; 胜率差与笔数差均在容差内 -> 达标; 否则超差."""
    if rep_n < MIN_N:
        return "样本不足"
    if abs(rep_wr - ref_wr) <= wr_tol and abs(rep_n - ref_n) / ref_n <= n_tol:
        return "达标"
    return "超差"


def _agg_oos_by_rule(oos_rows: list[dict]) -> dict[str, dict]:
    """按 rule 聚合: n 求和, wr 按 n 加权, r_sum 求和."""
    agg: dict[str, dict] = {}
    for row in oos_rows:
        rule = row.get("rule", "")
        a = agg.setdefault(rule, {"n": 0, "wr_weighted": 0.0, "r_sum": 0.0})
        a["n"] += row.get("n", 0)
        a["wr_weighted"] += row.get("wr", 0.0) * row.get("n", 0)
        a["r_sum"] += row.get("r_sum", 0.0)
    for a in agg.values():
        a["wr"] = a["wr_weighted"] / a["n"] if a["n"] else None
    return agg


def _first_ref_by_rule() -> dict[str, str]:
    """每个 rule 取 PLATFORM_REFERENCE 顺序中首个对应 slug(OOS 表判定基准)."""
    out: dict[str, str] = {}
    for slug, ref in PLATFORM_REFERENCE.items():
        out.setdefault(ref["rule"], slug)
    return out


def _fmt2(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def build_matrix(oos_doc: dict) -> tuple[str, list[dict]]:
    """输入 oos_binance.json 的 dict, 返回 (markdown 全文, 明细行 list)."""
    full_best = oos_doc.get("full_best", {}) or {}

    rows: list[dict] = []
    for slug, ref in PLATFORM_REFERENCE.items():
        fb = full_best.get(ref["rule"])
        if fb is None:
            rep_wr = rep_n = rep_ret = None
            judge = "样本不足"
        else:
            rep_wr = fb["wr"] * 100      # 0-1 小数 -> 百分点
            rep_n = fb["n"]
            rep_ret = fb["r_sum"]        # R 倍数口径
            judge = verdict(ref["wr"], ref["n"], rep_wr, rep_n)
        rows.append({
            "slug": slug,
            "策略": ref["strategy"],
            "还原规则": ref["rule"],
            "平台wr%": ref["wr"],
            "回放wr%": rep_wr,
            "平台n": ref["n"],
            "回放n": rep_n,
            "平台ret%": ref["ret"],
            "回放retR": rep_ret,
            "平台dd%": ref["dd"],
            "判定": judge,
        })

    md = _markdown(oos_doc, rows)
    return md, rows


def _main_table(rows: list[dict]) -> list[str]:
    lines = [
        "| 策略 | 还原规则 | 平台wr% | 回放wr% | 平台n | 回放n | 平台ret% | 回放ret(R) | 平台dd% | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['策略']} | {r['还原规则']} | {r['平台wr%']:.2f} | {_fmt2(r['回放wr%'])} "
            f"| {r['平台n']} | {r['回放n'] if r['回放n'] is not None else '—'} "
            f"| {r['平台ret%']:.2f} | {_fmt2(r['回放retR'])} | {r['平台dd%']:.2f} | {r['判定']} |"
        )
    lines += [
        "",
        "注: zec_mom1_5x / zec_macd / zec_high_freq 三个平台策略同用 `momo_break` 一条还原规则"
        "(同一还原规则, 回放统计为该规则全样本最优组合口径)。",
    ]
    return lines


def _oos_table(oos_doc: dict) -> list[str]:
    agg = _agg_oos_by_rule(oos_doc.get("oos_rows", []) or [])
    first_ref = _first_ref_by_rule()
    lines = [
        "## 滚动样本外(OOS)口径",
        "",
        "按 rule 聚合全部 OOS 段(n 求和、wr 按 n 加权、r_sum 求和);"
        "判定以该 rule 首个平台参考为基准, 容差与主表一致。",
        "",
        "| 还原规则 | 对应策略 | OOS n | OOS wr% | OOS r_sum | 判定 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for rule in sorted(agg):
        a = agg[rule]
        base_slug = first_ref.get(rule)
        if base_slug is None or a["wr"] is None:
            judge = "—"
        else:
            ref = PLATFORM_REFERENCE[base_slug]
            judge = verdict(ref["wr"], ref["n"], a["wr"] * 100, a["n"])
        covered = "、".join(
            r["strategy"] for r in PLATFORM_REFERENCE.values() if r["rule"] == rule
        ) or "—"
        lines.append(
            f"| {rule} | {covered} | {a['n']} | {_fmt2(a['wr'] * 100 if a['wr'] is not None else None)} "
            f"| {a['r_sum']:.2f} | {judge} |"
        )
    return lines


def _commentary(rows: list[dict], oos_doc: dict) -> list[str]:
    ok = [r for r in rows if r["判定"] == "达标"]
    bad = [r for r in rows if r["判定"] == "超差"]
    poor = [r for r in rows if r["判定"] == "样本不足"]

    def names(items):
        return "、".join(r["策略"] for r in items) if items else "无"

    agg = _agg_oos_by_rule(oos_doc.get("oos_rows", []) or [])
    all_neg = bool(agg) and all(a["r_sum"] < 0 for a in agg.values())
    return [
        "## 判读",
        "",
        f"- 达标: {names(ok)}。超差: {names(bad)}。样本不足: {names(poor)}。",
        f"- 回放窗口为 {oos_doc.get('days', '?')} 天({oos_doc.get('segments', '?')} 段滚动),"
        "远短于平台策略全历史; 120 天截断使多数规则的回放笔数明显低于平台累计笔数,"
        "即使胜率形态一致, 笔数相对差也容易超出 25% 容差而判超差——笔数差异主要是窗口长度所致,"
        "不必然指向规则失真。",
        "- OOS 口径各规则 r_sum "
        + ("全为负: 机械还原规则在样本外不盈利。这与\"平台头部数字有幸存者偏差、"
           "平台收益为复利/杠杆口径\"的免责一致——全样本最优组合本身已含选择偏差,"
           "滚动样本外仍全面亏损说明这些简单规则形态不足以直接实盘,"
           "还原对照只用于确认\"同规则同数据段统计形态\", 不构成对平台收益的可复刻性背书。"
           if all_neg else
           "并非全负, 仍有规则在样本外为正。") ,
        "",
    ]


def _markdown(oos_doc: dict, rows: list[dict]) -> str:
    gen_at = oos_doc.get("generated_at", datetime.now(TZ8).isoformat())
    lines = [
        "# VergeX 还原度矩阵",
        "",
        f"- 生成时间: {datetime.now(TZ8).strftime('%Y-%m-%d')}",
        f"- 回放数据: source={oos_doc.get('source', '?')}, days={oos_doc.get('days', '?')}, "
        f"segments={oos_doc.get('segments', '?')}, oos 生成于 {gen_at}",
        "- 判定容差: 胜率差 <= 8.0 百分点且笔数相对差 <= 25%, 回放 n < 30 判样本不足。",
        "",
        "## 主口径: 全样本最优组合 (full_best)",
        "",
        *_main_table(rows),
        "",
        *_oos_table(oos_doc),
        "",
        *_commentary(rows, oos_doc),
        "## 不可还原边界",
        "",
        *NON_REPRODUCIBLE_BOUNDARIES,
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oos", required=True, help="path to oos_binance.json")
    ap.add_argument("--out", required=True, help="path to output markdown")
    args = ap.parse_args()

    doc = json.loads(Path(args.oos).read_text(encoding="utf-8"))
    md, rows = build_matrix(doc)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    for r in rows:
        wr = _fmt2(r["回放wr%"]) if r["回放wr%"] is not None else "—"
        n = r["回放n"] if r["回放n"] is not None else "—"
        print(f"{r['策略']}  {r['判定']}  回放wr={wr}%  回放n={n}")
    print(f"report written: {out}")


if __name__ == "__main__":
    main()
