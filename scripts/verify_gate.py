"""验证线上入口闸门真的生效(没把计划漏进交易所).

判据(与 docs/闸门裁定表.md 第 7 节一致, 但**以线上配置为准, 不写死快照**):
  - block_short_entry=True  -> 不允许出现**提交到交易所**的做空计划
  - block_cycle_allowlist   -> 提交的周期位置必须落在白名单内

四条必须知道的前提:
  1. 模型可以主动观望(terminal.outcome=wait / order_type=不下单 / order_direction=None),
     这类记录没有入场意图, 不参与闸门判定, 否则会把「模型自己不下单」误报成「闸门泄漏」;
  2. 被入口门控拦下的计划**不算泄漏**: 它们没有提交到交易所(execution.status=
     gate_blocked), 判定必须看 execution 标记。2026-09-20 现场: 两条被正确拦下的做空
     spike 单被本脚本报成 VIOLATION 并 exit 1, 属假阳性 —— 把闸门干得好当成闸门坏了;
  3. 开关值从配置文件读, 不硬编码。此前 DEFAULT_ALLOW 写死三个周期值, 而 2026-09-20
     00:51 的放量变更已把 trending_tr 加进白名单, 脚本比配置旧 ⇒ 对 trending_tr 做空
     假报违规。读不到的降级方向是"关掉周期检查并明说", 不是退回硬编码(硬的会再漂);
  4. 被拦计划是否落 records/pending 记录, 早期(2026-09-13 之前)是"不落", 现在是"照落
     (带 execution 标记)"。所以本脚本既能证明「没漏进去」, 也能给出拦截原因计数。

退出码: 0=无违规, 1=发现违规, 2=上线后暂无记录
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.records.execution_marker import ACTIVE_STATUSES
from tools.expectancy_report import GATE_START, parse_cutoff

NO_ORDER_TYPES = ("不下单", "无", "wait", "no_trade")


def plan_is_working(status) -> bool:
    """该计划是否真的提交到了交易所 —— 只有这种状态才可能构成"泄漏".

    与 pa_agent.records.execution_marker.plan_is_working 同口径: 空/未标记 = 旧记录,
    按已提交处理(历史兼容); 否则只有 ACTIVE_STATUSES 里的状态算在场工作。
    """
    text = str(status or "").strip()
    if not text:
        return True
    return text in ACTIVE_STATUSES


def load_live_gates(config_path=None):
    """读线上真实生效的 (周期白名单, 做空门开关, 来源说明).

    读不到配置时返回空白名单(周期检查关闭)并打印原因 —— 宁可少判一项并说清楚,
    也不要退回一个会和配置漂开的硬编码值。
    """
    path = Path(config_path) if config_path else SETTINGS_JSON_PATH
    try:
        from pa_agent.config.settings import load_settings

        cfg = load_settings(path).binance_usdm_testnet
    except Exception as exc:  # 脚本要能降级并明说, 不因读配置失败而崩
        print(f"!! 读配置失败({path}): {exc} —— 周期检查按关闭处理, 可用 --allow 手工指定")
        return (), True, f"读取失败({path})"
    return tuple(cfg.block_cycle_allowlist or ()), bool(cfg.block_short_entry), str(path)


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


def violations_for(direction, cycle, allow, *, status="", short_gate=True):
    """返回该笔计划违反闸门的理由列表(空 = 合规).

    只对**真的提交了**的计划判定(status 走 plan_is_working 口径): 被拦下/被拒/干跑的
    计划交易所上一无所有, 把它们算成违规等于把闸门干得好当成闸门坏了。
    short_gate=False(线上 block_short_entry=false)时做空本就是允许的, 不再判违规。
    """
    if not plan_is_working(status):
        return []
    out = []
    if short_gate and "空" in str(direction or ""):
        out.append("做空单未被拦截")
    allow = tuple(allow or ())
    if allow and cycle and str(cycle) not in allow:
        out.append(f"周期位置 {cycle} 不在白名单({'/'.join(allow)})")
    return out


def gate_reason_counts(infos):
    """按拦截原因前缀(冒号前)统计次数, 便于看哪个闸门在实盘拦得最多."""
    counts = {}
    for info in infos:
        for reason in info.get("gate_reasons") or ():
            key = str(reason).split(":", 1)[0].strip()
            counts[key] = counts.get(key, 0) + 1
    return counts


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
        "gate_reasons": list((dec.get("execution") or {}).get("gate_reasons") or ()),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pattern", default="records/pending/*.json")
    ap.add_argument("--since", default=GATE_START)
    ap.add_argument("--config", default=None,
                    help="配置文件路径(缺省 config/settings.json)")
    ap.add_argument("--allow", default=None,
                    help="周期白名单(逗号分隔); 缺省读配置的 block_cycle_allowlist")
    ap.add_argument("--short-gate", choices=("auto", "on", "off"), default="auto",
                    help="做空门是否开启; auto=读配置(缺省), on/off=强制")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cutoff = parse_cutoff(args.since)
    if args.allow is None:
        allow, short_gate, source = load_live_gates(args.config)
        gate_src = f"{source} block_short_entry={short_gate}"
    else:
        allow = tuple(x.strip() for x in args.allow.split(",") if x.strip())
        short_gate = True
        gate_src = "--allow 命令行(做空门按开启处理)"
    if args.short_gate != "auto":
        short_gate = args.short_gate == "on"
    records = load_records(args.pattern, cutoff)
    print(f"since={args.since} 上线后记录数={len(records)} "
          f"白名单={list(allow)} 开关来源={gate_src}")
    if not records:
        print("结论: 暂无上线后记录 —— 等下一次轮询 (节奏看 monitoring.poll_interval_minutes)")
        return 2

    bad = attempted = submitted = withheld = 0
    infos = []
    for path, doc in records:
        info = describe(path, doc)
        if not is_entry_attempt(info["order_type"], info["direction"]):
            print(f"{info['file']} {info['symbol']} 观望(outcome={info['outcome']}) "
                  f"cycle={info['cycle']}  SKIP: 无入场意图")
            continue
        infos.append(info)
        attempted += 1
        working = plan_is_working(info["exec_status"])
        if working:
            submitted += 1
        else:
            withheld += 1
        viol = violations_for(info["direction"], info["cycle"], allow,
                              status=info["exec_status"], short_gate=short_gate)
        bad += 1 if viol else 0
        if viol:
            tag = "VIOLATION: " + "; ".join(viol)
        elif working:
            tag = "OK(已提交且合规)"
        else:
            tag = "OK(已被拦下, 未提交)"
        if working and not info["exec_status"]:
            # 未标记 = 旧代码路径(execution 标记上线前的记录)。按 execution_marker
            # 的口径保守地当成"已提交", 但必须显式提示这是推断而非事实。
            tag += "  [未标记 execution: 按已提交处理, 需人工确认]"
        blocked = ("  拦截: " + "; ".join(info["gate_reasons"])) if info["gate_reasons"] else ""
        print(f"{info['file']} {info['symbol']} {info['direction']} {info['cycle']} "
              f"exec={info['exec_status']}  {tag}{blocked}")
    counts = gate_reason_counts(infos)
    if counts:
        print("拦截原因统计: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"结论: 上线后 {len(records)} 条记录, 尝试入场 {attempted} 条 "
          f"(已提交 {submitted} / 未提交 {withheld}); "
          + ("闸门有泄漏, 见上" if bad else "无泄漏: 没有未拦截的计划提交到交易所"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
