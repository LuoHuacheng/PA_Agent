#!/usr/bin/env python3
"""K2 A/B 试点(L1): 冻结输入的配对重跑编排.

设计见 docs/superpowers/specs/2026-09-20-k2-new-bar-attribution-design.md

两个阶段:
  --dry-run   (默认, 0 次调用) 选择轮次 + 渲染两臂提示词 + 与记录里的
              stage1_messages 做字节对比(保真闸门)。通过率 <80% 就该退化为
              "直接编辑 stored messages 的新增块"方案, 而不是硬跑。
  --run       对每轮跑 B 臂(new_bar_count=2) 与噪声地板臂(A 臂 count=1 重跑),
              原始回复全部落 JSONL, 之后离线算指标(不重复调用)。

用法:
    python tools/k2_ab_pilot.py --rounds 24 --days 5            # 保真闸门
    python tools/k2_ab_pilot.py --run --rounds 24 --days 5      # 花调用跑 B 臂
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from k2_materiality_gate import DEFAULT_SYMBOLS, load_rounds  # noqa: E402

from pa_agent.ai.client_factory import create_ai_client  # noqa: E402
from pa_agent.ai.prompt_assembler import PromptAssembler  # noqa: E402
from pa_agent.config.paths import EXPERIENCE_DIR, PROMPT_DIR  # noqa: E402
from pa_agent.config.settings import load_settings  # noqa: E402
from pa_agent.demo.record_loader import frame_from_record_klines  # noqa: E402
from pa_agent.records.analysis_history import load_record  # noqa: E402
from pa_agent.records.experience_reader import ExperienceReader  # noqa: E402
from pa_agent.util.threading import CancelToken  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
DEFAULT_OUT = ROOT / "logs" / "k2_ab_pilot.jsonl"


def build_assembler(settings: object) -> PromptAssembler:
    """与 app_context 同参构造(见 pa_agent/app_context.py:112)。"""
    from pa_agent.trading.binance_env import active_cfg

    reader = ExperienceReader(experience_dir=EXPERIENCE_DIR)
    return PromptAssembler(
        prompt_dir=PROMPT_DIR,
        experience_reader=reader,
        prompt_settings=settings.prompt,
        feedback_settings=settings.feedback,
        binance_settings=active_cfg(settings),
    )


def select_rounds(rows: list[dict], limit: int) -> list[dict]:
    """按品种轮转取最近 *limit* 轮, 避免品种偏斜。"""
    rows = sorted(rows, key=lambda r: str(r["path"]), reverse=True)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[str(row["meta"].get("symbol"))].append(row)
    picked: list[dict] = []
    while len(picked) < limit and any(buckets.values()):
        for sym in sorted(buckets):
            if not buckets[sym]:
                continue
            picked.append(buckets[sym].pop(0))
            if len(picked) >= limit:
                break
    return picked


def _norm(messages: list[dict]) -> list[tuple[str, str]]:
    return [(str(m.get("role")), str(m.get("content") or "")) for m in messages]


def compare_prompts(rendered: list[dict], stored: list[dict]) -> tuple[bool, str]:
    """渲染臂与生产记录的 stage1_messages 逐字节对比。"""
    a, b = _norm(rendered), _norm(stored)
    if len(a) != len(b):
        return False, f"长度不同: rendered={len(a)} stored={len(b)}"
    for i, ((ra, ca), (rb, cb)) in enumerate(zip(a, b)):
        if ra != rb:
            return False, f"[{i}] role 不同: {ra} vs {rb}"
        if ca != cb:
            head = 0
            while head < min(len(ca), len(cb)) and ca[head] == cb[head]:
                head += 1
            return False, (
                f"[{i}] 内容不同(len {len(ca)} vs {len(cb)}) 首个差异位于 {head}: "
                f"rendered={ca[head:head + 60]!r} stored={cb[head:head + 60]!r}"
            )
    return True, ""


def _record_for(row: dict) -> object:
    record = load_record(Path(row["prev_path"]))
    if record is None:
        raise ValueError(f"上一条记录无法解析: {row['prev_path']}")
    return record


def _frame_for(row: dict):
    cur = row["cur"]
    meta = cur["meta"]
    return frame_from_record_klines(
        cur["kline_data"],
        symbol=meta["symbol"],
        timeframe=meta["timeframe"],
        snapshot_ts_local_ms=meta.get("timestamp_local_ms"),
    )


_FENCE = chr(96) * 3


def json_body(text: str) -> str:
    """从回复里抠出裸 JSON(容忍代码围栏)。"""
    body = (text or "").strip()
    if body.startswith(_FENCE):
        body = body.split(chr(10), 1)[-1].strip()
    if body.endswith(_FENCE):
        body = body[: -len(_FENCE)].strip()
    start, end = body.find("{"), body.rfind("}")
    return body[start:end + 1] if start >= 0 and end > start else body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="真的调用模型跑 B 臂(默认只做保真闸门)")
    ap.add_argument("--rounds", type=int, default=24)
    ap.add_argument("--days", type=float, default=5.0)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--noise-frac", type=float, default=0.1, help="噪声地板臂抽样比例")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    settings = load_settings()
    assembler = build_assembler(settings)
    rows = select_rounds(load_rounds(days=args.days, symbols=symbols), args.rounds)
    print(f"选中轮次: {len(rows)}  (--rounds {args.rounds}, 近 {args.days:g} 天)")
    if not rows:
        print("没有可用轮次")
        return 1
    ok = 0
    results: list[dict] = []
    for row in rows:
        meta = row["meta"]
        label = f"{meta['symbol']} {meta.get('timestamp_local_iso', '')[11:19]}"
        try:
            prev_record = _record_for(row)
            frame = _frame_for(row)
        except Exception as exc:  # noqa: BLE001 - 单轮失败不该中断
            print(f"  {label}: 准备失败: {exc}")
            continue
        if not assembler.can_build_incremental_stage1(prev_record):
            print(f"  {label}: 上一条记录不可增量(缺 stage1 response), 跳过")
            continue
        rendered_a = assembler.build_incremental_stage1(frame, prev_record, 1)
        stored = list(row["cur"].get("stage1_messages") or [])
        # 记录里最后一条 assistant 是"本轮回复"被回写进来的(实测 == stage1_response.content),
        # 真正发出去的只有 stored[:-1]。
        reply = str((row["cur"].get("stage1_response") or {}).get("content") or "")
        if stored and stored[-1].get("role") == "assistant" and str(
            stored[-1].get("content") or ""
        ) == reply:
            stored = stored[:-1]
        same, why = compare_prompts(rendered_a, stored)
        ok += 1 if same else 0
        print(f"  {label}: 保真={'OK' if same else 'MISMATCH'} {why}")
        results.append({"symbol": meta["symbol"], "ts": meta.get("timestamp_local_ms"),
                        "path": str(row["path"]), "prev_path": str(row["prev_path"]),
                        "fidelity_ok": same, "fidelity_why": why})
    rate = ok / len(results) if results else 0.0
    print(f"\n保真通过率: {ok}/{len(results)} = {rate * 100:.1f}%")
    print("判定:", "可以直接跑 B 臂" if rate >= 0.8 else
          "通过率 <80% —— 应退化为直接编辑 stored messages 的新增块方案")
    if args.run:
        print("\n--run 尚未实现: 先确认保真通过率与调用预算, 再补 B 臂执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
