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

#: **最近一次配置变更**时刻, R2/R3 判定只统计它之后的样本。
#:
#: 变更史(每次改线上的闸门/参数都必须更新这里, 否则会把两种配置的样本混在一起 ——
#: 计划第 0 节禁令 2 的同源问题: 不能用旧配置的样本报告新配置的收益):
#:   1. 2026-09-18 15:11 UTC+8  灰度上线: block_short_entry=true +
#:      block_cycle_allowlist=[broad_channel,normal_channel,trading_range]  (docs/闸门裁定表.md §7)
#:   2. 2026-09-18 16:53 UTC+8  limit_fill_timeout_minutes: 30 -> 120        (docs/闸门裁定表.md §7.2)
GATE_START = "2026-09-18T16:53:01+08:00"


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


def account_drawdown_pct(nets, capital):
    """账户口径回撤 = 绝对峰值回撤 / 参照本金.

    计划 T3 的 max_drawdown() 分母是"累计净额峰值", 不是账户 —— 见其 docstring 与
    docs/正期望验证规程.md §2.7。要按账户口径判 R2/R3, 必须给出参照本金。
    """
    if not capital or capital <= 0:
        return 0.0
    return max_drawdown_usdt(nets) / float(capital)


def evaluate(nets, capital=None):
    """由逐笔净额算出 n/均/区间/胜率/回撤/阶段/是否通过.

    capital 给定时额外算出账户口径回撤 dd_account(比例), 否则它为 None。
    """
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
    dd_account = account_drawdown_pct(nets, capital) if capital else None
    ok = bool(nets) and stage != "样本不足" and mean > 0 and lo > 0 and dd < MAX_DD[stage]
    return {"n": len(nets), "mean": mean, "lo": lo, "hi": hi, "wr": wr,
            "dd": dd, "dd_usdt": dd_usdt, "dd_account": dd_account,
            "stage": stage, "ok": ok}


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
                    help=f"只统计最近一次配置变更({GATE_START})之后的样本")
    ap.add_argument("--capital", type=float, default=None,
                    help="参照本金(USDT); 给了就额外输出账户口径回撤")
    ap.add_argument("--account-dd-gate", action="store_true",
                    help="用账户口径回撤(需 --capital)代替计划口径判定 R2/R3")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.account_dd_gate and not args.capital:
        print("--account-dd-gate 需要同时给 --capital", file=sys.stderr)
        return 2

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

    res = evaluate(nets, capital=args.capital)
    prefix = f"since={cutoff_label} " if cutoff_label else ""
    acct = ""
    if res["dd_account"] is not None:
        acct = f" 账户回撤{res['dd_account']:.1%}(本金{args.capital:.0f}U)"
    if res["stage"] == "样本不足":
        print(f"{prefix}n={res['n']} 均{res['mean']:+.2f}U 胜率{res['wr']:.0%} "
              f"90%CI[{res['lo']:+.2f}, {res['hi']:+.2f}] "
              f"回撤{res['dd_usdt']:.2f}U(峰值口径{res['dd']:.1%}){acct} "
              f"-> FAIL(样本不足, 需 n>={MIN_N['R2']})")
        return 1
    dd_for_gate = res["dd_account"] if args.account_dd_gate else res["dd"]
    ok = res["mean"] > 0 and res["lo"] > 0 and dd_for_gate < MAX_DD[res["stage"]]
    dd_label = "账户回撤" if args.account_dd_gate else "峰值口径回撤"
    print(f"{prefix}n={res['n']} 均{res['mean']:+.2f}U 胜率{res['wr']:.0%} "
          f"90%CI[{res['lo']:+.2f}, {res['hi']:+.2f}] "
          f"回撤{res['dd_usdt']:.2f}U(峰值口径{res['dd']:.1%}){acct} "
          f"阶段={res['stage']} -> {'PASS' if ok else 'FAIL'}")
    print(f"门槛({res['stage']}): n>={MIN_N[res['stage']]}, 均>0, CI下界>0, "
          f"{dd_label}<{MAX_DD[res['stage']]:.0%}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
