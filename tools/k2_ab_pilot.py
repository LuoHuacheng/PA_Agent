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
import re
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
from pa_agent.ai.json_validator import JsonValidator  # noqa: E402
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


#: 增量消息里两张"新增棒"表的章节前缀(顺序即插入顺序)。
NEW_BAR_SECTIONS = ("## 新增 K线数据", "## 新增 K线几何特征")


def section_block(text: str, header_prefix: str) -> str:
    """取以 *header_prefix* 开头、到下一个 '## ' 或文末为止的文本块。"""
    lines = text.split(chr(10))
    start = None
    for i, line in enumerate(lines):
        if line.startswith(header_prefix):
            start = i
            break
    if start is None:
        raise ValueError(f"section not found: {header_prefix}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return chr(10).join(lines[start:end])


def data_rows(block: str) -> list[str]:
    """块内以序号开头的表格数据行(形如 '1    | ...')。"""
    return [ln for ln in block.split(chr(10)) if re.match(r"^\s*\d+\s*\|", ln)]


def _row_identity(row: str, *, fields: int = 2) -> tuple[str, ...]:
    """表格行的身份字段(序号 + 时间/类型)。

    渲染臂的 EMA20/ATR14 及派生列存在预热重算漂移(实测 16.2011 vs 16.3145),
    所以只能按"同一根棒"校验, 不能整行比。
    """
    return tuple(part.strip() for part in row.split("|")[:fields])


def splice_arm_b(
    stored: str, rendered: str, *, sections: tuple[str, ...] = NEW_BAR_SECTIONS
) -> str:
    """把 rendered(2 根新增) 里 K2 的行拼进 stored(1 根新增)。

    stored 是生产实际发出的增量用户消息(逐字节), 因此拼接后 arm B 与 arm A 的
    差异被限制在: 两处 (共N根) + 一处 新增已收盘K线:N + 两张表各多一行。
    K1 行必须两边一致, 否则拒绝拼接(说明渲染口径已漂移)。
    """
    out = stored
    for header in sections:
        s_rows = data_rows(section_block(out, header))
        r_rows = data_rows(section_block(rendered, header))
        if len(s_rows) != 1 or len(r_rows) != 2:
            raise ValueError(
                f"{header}: 期望 stored 1 行 / rendered 2 行, 实际 {len(s_rows)}/{len(r_rows)}"
            )
        if _row_identity(s_rows[0]) != _row_identity(r_rows[0]):
            raise ValueError(
                f"{header}: K1 不是同一根棒 "
                f"({_row_identity(s_rows[0])} vs {_row_identity(r_rows[0])}), 拒绝拼接"
            )
        out = out.replace(s_rows[0] + chr(10), s_rows[0] + chr(10) + r_rows[1] + chr(10), 1)
    out = out.replace("新增已收盘K线:1", "新增已收盘K线:2")
    out = out.replace("(共1根", "(共2根")
    return out


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


def _usage_dict(reply: Any) -> Any:
    usage = getattr(reply, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    for attr in ("model_dump", "to_dict", "__dict__"):
        value = getattr(usage, attr, None)
        if callable(value):
            try:
                return value()
            except Exception:  # noqa: BLE001
                continue
        if isinstance(value, dict):
            return value
    return str(usage)


def call_stage(
    client: Any, settings: Any, messages: list[dict], *, label: str
) -> dict[str, Any]:
    """一次模型调用 + 宽松 JSON 解析(不做重试, 失败也落盘)。"""
    import time as _time

    started = _time.time()
    try:
        reply = client.stream_chat(
            messages,
            cancel_token=CancelToken(),
            thinking=bool(settings.provider.thinking),
            reasoning_effort=settings.provider.reasoning_effort,
        )
    except Exception as exc:  # noqa: BLE001 - 单次失败不该中断整批
        return {"label": label, "error": str(exc), "elapsed_s": round(_time.time() - started, 1)}
    out: dict[str, Any] = {
        "label": label,
        "content": reply.content or "",
        "usage": _usage_dict(reply),
        "elapsed_s": round(_time.time() - started, 1),
        "content_chars": len(reply.content or ""),
    }
    try:
        out["parsed"] = json.loads(json_body(out["content"]))
    except Exception as exc:  # noqa: BLE001
        out["parse_error"] = str(exc)
    return out


def run_one_round(
    row: dict,
    *,
    settings: Any,
    assembler: PromptAssembler,
    client: Any,
    validator: Any,
    noise: bool,
) -> dict[str, Any]:
    """跑一轮: B 臂(2 根) + 可选噪声地板臂(A 臂原样重跑)。"""
    meta = row["meta"]
    prev_record = _record_for(row)
    frame = _frame_for(row)
    sent = list(row["cur"].get("stage1_messages") or [])[:-1]
    if len(sent) < 4 or sent[3].get("role") != "user":
        raise ValueError(f"stored 增量链结构异常: {[m.get('role') for m in sent]}")
    stored_inc = str(sent[3].get("content") or "")
    rendered_b = assembler.build_incremental_stage1(frame, prev_record, 2)
    arm_b_user = splice_arm_b(stored_inc, str(rendered_b[-1].get("content") or ""))
    record: dict[str, Any] = {
        "symbol": meta.get("symbol"),
        "ts": meta.get("timestamp_local_ms"),
        "path": str(row["path"]),
        "prev_path": str(row["prev_path"]),
        "stored_inc_chars": len(stored_inc),
        "arm_b_inc_chars": len(arm_b_user),
        "runs": {},
    }
    for arm, user_text, reps in (("B", arm_b_user, 1), ("A_noise", stored_inc, 2 if noise else 0)):
        for rep in range(reps):
            messages = sent[:3] + [{"role": "user", "content": user_text}]
            key = arm if reps == 1 else f"{arm}#{rep + 1}"
            s1 = call_stage(client, settings, messages, label=f"{key}:stage1")
            entry: dict[str, Any] = {"stage1": s1}
            if s1.get("parsed"):
                try:
                    valid = validator.validate(
                        "stage1", s1["content"], kline_frame=frame,
                        incremental_new_bar_count=2 if arm == "B" else 1,
                    )
                    entry["stage1_valid"] = not str(type(valid).__name__).endswith("ValidationError")
                except Exception as exc:  # noqa: BLE001
                    entry["stage1_valid"] = False
                    entry["stage1_valid_error"] = str(exc)[:200]
                try:
                    messages_s2 = assembler.build_stage2_continuation(
                        frame=frame,
                        stage1_messages=messages,
                        stage1_reply_content=s1["content"],
                        stage1_json=s1["parsed"],
                        strategy_files=list(row["cur"].get("strategy_files_used") or []),
                        experience_entries=list(row["cur"].get("experience_loaded") or []),
                        decision_stance=str(meta.get("decision_stance") or ""),
                        previous_record=prev_record,
                        provider_settings=settings.provider,
                        structure_flip_cooldown_bars=int(
                            getattr(settings.general, "structure_flip_cooldown_bars", 3) or 3
                        ),
                    )
                    entry["stage2"] = call_stage(
                        client, settings, messages_s2, label=f"{key}:stage2"
                    )
                except Exception as exc:  # noqa: BLE001
                    entry["stage2_build_error"] = str(exc)[:300]
            record["runs"][key] = entry
    return record


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
        row["fidelity_ok"] = same
        print(f"  {label}: 保真={'OK' if same else 'MISMATCH'} {why}")
        results.append({"symbol": meta["symbol"], "ts": meta.get("timestamp_local_ms"),
                        "path": str(row["path"]), "prev_path": str(row["prev_path"]),
                        "fidelity_ok": same, "fidelity_why": why})
    rate = ok / len(results) if results else 0.0
    print(f"\n保真通过率: {ok}/{len(results)} = {rate * 100:.1f}%")
    print("判定:", "可以直接跑 B 臂" if rate >= 0.8 else
          "通过率 <80% —— 应退化为直接编辑 stored messages 的新增块方案")
    if not args.run:
        return 0
    # 渲染保真只是参考: arm A 用 stored 逐字节, arm B 靠 splice 只补 K2 两行,
    # 因此 [1] 的指标漂移不影响实验。真正的前置条件是"splice 能不能拼出来"。
    precheck: list[dict] = []
    for row in rows:
        try:
            sent = list(row["cur"].get("stage1_messages") or [])[:-1]
            stored_inc = str(sent[3].get("content") or "")
            rendered = assembler.build_incremental_stage1(
                _frame_for(row), _record_for(row), 2
            )
            splice_arm_b(stored_inc, str(rendered[-1].get("content") or ""))
        except Exception as exc:  # noqa: BLE001
            print(f"  拼接预检失败 {row['meta'].get('symbol')}: {exc}")
            continue
        precheck.append(row)
    print(f"\n--run: 拼接预检通过 {len(precheck)}/{len(rows)} 轮")
    if not precheck:
        print("没有可跑的轮次")
        return 1
    runnable = precheck
    client = create_ai_client(settings.provider)
    validator = JsonValidator(settings)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    noise_every = max(1, int(round(1.0 / args.noise_frac))) if args.noise_frac > 0 else 0
    done = 0
    with open(args.out, "a", encoding="utf-8") as fh:
        for i, row in enumerate(runnable):
            meta = row["meta"]
            label = f"{meta.get('symbol')} {str(meta.get('timestamp_local_iso'))[11:19]}"
            noise = bool(noise_every) and (i % noise_every == 0)
            try:
                rec = run_one_round(
                    row, settings=settings, assembler=assembler,
                    client=client, validator=validator, noise=noise,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[{i + 1}/{len(runnable)}] {label}: 失败 {exc}", flush=True)
                continue
            rec["noise"] = noise
            fh.write(json.dumps(rec, ensure_ascii=False) + chr(10))
            fh.flush()
            done += 1
            b = rec["runs"].get("B", {})
            s1, s2 = b.get("stage1", {}), b.get("stage2", {})
            print(
                f"[{i + 1}/{len(runnable)}] {label}: "
                f"s1 {s1.get('elapsed_s')}s {'ok' if s1.get('parsed') else 'FAIL'} | "
                f"s2 {s2.get('elapsed_s')}s {'ok' if s2.get('parsed') else 'FAIL'}"
                + ("  [noise]" if noise else ""),
                flush=True,
            )
    print(f"\n完成 {done} 轮 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
