"""验证 T6 灰度闸门在实盘轮询里真的生效.

判据(与 docs/闸门裁定表.md 第 7 节一致):
  - block_short_entry=True  -> 不允许出现做空计划
  - block_cycle_allowlist   -> 周期位置必须落在白名单内

两个必须知道的前提:
  1. 模型可以主动观望(terminal.outcome=wait / order_type=不下单 / order_direction=None),
     这类记录没有入场意图, 不参与闸门判定, 否则会把「模型自己不下单」误报成「闸门泄漏」;
  2. 被入口门控拦下的计划可能不落 records/pending 记录
     (pa_agent/records/execution_marker.py 注释里的 2026-09-13 现场),
     所以本脚本只能证明「没漏进去」, 不能证明「拦了几笔」; 后者看 logs/pa_agent.log。

退出码: 0=无违规, 1=发现违规, 2=上线后暂无记录
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.expectancy_report import GATE_START, parse_cutoff

DEFAULT_ALLOW = ("broad_channel", "normal_channel", "trading_range")
NO_ORDER_TYPES = ("不下单", "无", "wait", "no_trade")


def load_records(pattern, cutoff_ms):
    """读入 timestamp_local_ms >= cutoff_ms 的决策记录."""
    records = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        meta = doc.get("meta") or {}
        try:
            ts = int(meta.get("timestamp_local_ms"))
        except (TypeError, ValueError):
            continue  # 没有可信时间戳的记录无法归属到窗口, 跳过
        if ts < cutoff_ms:
            continue
        records.append((path, doc))
    return records


def is_entry_attempt(order_type, direction):
    """是否真的打算入场.

    模型可以主动观望(terminal.outcome=wait / order_type=不下单 / order_direction=None),
    这类记录没有入场意图, 不该拿去做闸门判定。
    (2026-09-18 16:09 SOLUSDT 那条 order_type=不下单、cycle=tight_channel, 就误报过一次。)
    """
    if str(order_type or "").strip() in NO_ORDER_TYPES:
        return False
    return bool(str(direction or "").strip())


def violations_for(direction, cycle, allow):
    """返回该笔计划违反闸门的理由列表(空 = 合规)."""
    out = []
    if "空" in str(direction or ""):
        out.append("做空单未被拦截")
    allow = tuple(allow or ())
    if allow and cycle and str(cycle) not in allow:
        out.append(f"周期位置 {cycle} 不在白名单({'/'.join(allow)})")
    return out


def describe(path, doc):
    """抽出一笔计划的可读字段."""
    dec = doc.get("stage2_decision") or {}
    inner = dec.get("decision") or {}
    diag = dec.get("diagnosis_summary") or {}
    meta = doc.get("meta") or {}
    terminal = dec.get("terminal") or {}
    return {
        "file": Path(path).name,
        "symbol": meta.get("symbol"),
        "direction": inner.get("order_direction"),
        "order_type": inner.get("order_type"),
        "cycle": diag.get("cycle_position"),
        "outcome": terminal.get("outcome"),
        "exec_status": (dec.get("execution") or {}).get("status"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pattern", default="records/pending/*.json")
    ap.add_argument("--since", default=GATE_START)
    ap.add_argument("--allow", default=",".join(DEFAULT_ALLOW))
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cutoff = parse_cutoff(args.since)
    allow = tuple(x.strip() for x in args.allow.split(",") if x.strip())
    records = load_records(args.pattern, cutoff)
    print(f"since={args.since} 上线后记录数={len(records)} 白名单={list(allow)}")
    if not records:
        print("结论: 暂无上线后记录 —— 等下一次整点轮询(每 30 分钟 +5s)")
        return 2

    bad = attempted = 0
    for path, doc in records:
        info = describe(path, doc)
        if not is_entry_attempt(info["order_type"], info["direction"]):
            print(f"{info['file']} {info['symbol']} 观望(outcome={info['outcome']}) "
                  f"cycle={info['cycle']}  SKIP: 无入场意图")
            continue
        attempted += 1
        viol = violations_for(info["direction"], info["cycle"], allow)
        bad += 1 if viol else 0
        tag = "VIOLATION: " + "; ".join(viol) if viol else "OK"
        print(f"{info['file']} {info['symbol']} {info['direction']} {info['cycle']} "
              f"exec={info['exec_status']}  {tag}")
    print(f"结论: 上线后 {len(records)} 条记录, 尝试入场 {attempted} 条; "
          + ("闸门有泄漏, 见上" if bad else "闸门生效, 无违规"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
