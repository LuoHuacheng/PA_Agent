"""按硬门槛判定实盘期望是否显著为正.

用法:
    .venv/bin/python tools/expectancy_report.py                    # 全部历史样本
    .venv/bin/python tools/expectancy_report.py --gate-since       # 只用 T6 上线后的样本
    .venv/bin/python tools/expectancy_report.py --since 2026-09-18T15:11:00+08:00
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.shadow_replay import bootstrap_mean_ci

MIN_N = {"R2": 30, "R3": 100}
MAX_DD = {"R2": 0.25, "R3": 0.30}

#: T6 灰度上线时刻(2026-09-18 15:11 UTC+8), 见 docs/闸门裁定表.md 第 5 节。
#: R2/R3 的判定必须只用此时刻之后平仓的样本——否则会把改动前的亏损样本混进来
#: (计划第 0 节禁令 2 的同源问题: 不能用旧样本报告新配方的收益)。
GATE_START = "2026-09-18T15:11:00+08:00"


def parse_cutoff(text):
    """ISO8601 或纯数字 epoch 毫秒 -> epoch 毫秒; 不带时区的 ISO 按本地时区解释."""
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def filter_since(rows, cutoff_ms):
    """只保留 ts_close >= cutoff_ms 且时间戳可解析的行."""
    kept = []
    for row in rows:
        try:
            ts = float(row.get("ts_close") or "")
        except (TypeError, ValueError):
            continue
        if ts >= cutoff_ms:
            kept.append(row)
    return kept


def max_drawdown(nets):
    """按时间顺序累计净额的峰值回撤(相对峰值, 计划 T3 原口径).

    ⚠️ 这个比值**不是账户回撤**: 分母是"累计净额的峰值", 不是账户权益。
    亏损曲线下它可以超过 100%(实盘 73 笔给 758.7%), 因为峰值只有 +24.29U
    而后续回吐是 184.26U。门槛 "回撤<25%" 按此口径读作"回吐峰值利润的 25%"。
    绝对口径见 max_drawdown_usdt()。详见 docs/正期望验证规程.md §2.7。
    """
    peak = equity = worst = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def max_drawdown_usdt(nets):
    """峰值到谷底的最大绝对回撤(USDT) —— 与账户口径可比的量."""
    peak = equity = worst = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def evaluate(nets):
    """由逐笔净额算出 n/均/区间/胜率/回撤/阶段/是否通过."""
    if len(nets) >= MIN_N["R3"]:
        stage = "R3"
    elif len(nets) >= MIN_N["R2"]:
        stage = "R2"
    else:
        stage = "样本不足"
    mean = (sum(nets) / len(nets)) if nets else 0.0
    lo, hi = bootstrap_mean_ci(nets)
    wr = (sum(1 for n in nets if n > 0) / len(nets)) if nets else 0.0
    dd = max_drawdown(nets)
    dd_usdt = max_drawdown_usdt(nets)
    ok = bool(nets) and stage != "样本不足" and mean > 0 and lo > 0 and dd < MAX_DD[stage]
    return {"n": len(nets), "mean": mean, "lo": lo, "hi": hi,
            "wr": wr, "dd": dd, "dd_usdt": dd_usdt, "stage": stage, "ok": ok}


def _load_rows(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default="trade_records/outcomes.csv")
    ap.add_argument("--since", default="", help="只统计该时刻(ISO8601)之后平仓的样本")
    ap.add_argument("--gate-since", action="store_true",
                    help=f"只统计 T6 灰度上线({GATE_START})之后的样本")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    rows = _load_rows(args.path)
    cutoff_label = ""
    cutoff = None
    if args.gate_since:
        cutoff, cutoff_label = parse_cutoff(GATE_START), GATE_START
    if args.since:
        cutoff, cutoff_label = parse_cutoff(args.since), args.since
    if cutoff is not None:
        rows = filter_since(rows, cutoff)

    pairs = sorted((str(r.get("ts_close") or ""), _num(r.get("net_usdt"))) for r in rows)
    nets = [n for _, n in pairs if n is not None]
    if not nets:
        print("FAIL: 无成交记录" + (f"(since={cutoff_label})" if cutoff_label else ""))
        return 1

    res = evaluate(nets)
    prefix = f"since={cutoff_label} " if cutoff_label else ""
    if res["stage"] == "样本不足":
        print(f"{prefix}n={res['n']} 均{res['mean']:+.2f}U 胜率{res['wr']:.0%} "
              f"90%CI[{res['lo']:+.2f}, {res['hi']:+.2f}] "
              f"回撤{res['dd_usdt']:.2f}U(峰值口径{res['dd']:.1%}) "
              f"-> FAIL(样本不足, 需 n>={MIN_N['R2']})")
        return 1
    print(f"{prefix}n={res['n']} 均{res['mean']:+.2f}U 胜率{res['wr']:.0%} "
          f"90%CI[{res['lo']:+.2f}, {res['hi']:+.2f}] "
          f"回撤{res['dd_usdt']:.2f}U(峰值口径{res['dd']:.1%}) "
          f"阶段={res['stage']} -> {'PASS' if res['ok'] else 'FAIL'}")
    print(f"门槛({res['stage']}): n>={MIN_N[res['stage']]}, 均>0, CI下界>0, 回撤<{MAX_DD[res['stage']]:.0%}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
