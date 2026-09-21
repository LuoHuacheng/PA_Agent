# ruff: noqa: RUF001, RUF002 - Chinese product copy
"""多轮前滚对照：检验"上一轮自述拒单理由被回灌"是否自我强化（`docs/意图归零归因实验.md` §5）。

问题：单轮 A/B（`tools/ab_intent.py`）只能测"这一轮删掉该文案会不会改变这一轮的决定"，
答案是不会。但回路是**跨轮累积**的：第 N 轮写下的"会被执行层拒单"被注入第 N+1 轮的增量上下文，
再被写一次。要判定它，必须让模型**自己滚**若干轮，比较两臂。

两臂（同一条记录链、同一个起点、同一路由）：
  raw        上一轮的 stage2_decision 原样回灌（= 生产现状）
  sanitized  回灌前把"预言式拒单"话术替换成中性表述（见 ab_intent.SCRUB_RULES）

指标（逐轮）：是否下单、模型自己产出的预言式文案条数（复发）、注入上下文的该类文案条数（刺激）。

用法：
    .venv/bin/python tools/ab_loop.py --dry-run --rounds 6            # 只建链与组装，不调 API
    .venv/bin/python tools/ab_loop.py --symbol ZECUSDT --rounds 6 --out logs/ab_loop_run1.jsonl
"""

from __future__ import annotations

import argparse
import collections
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.config.settings import load_settings
from pa_agent.demo.record_loader import frame_from_record_klines
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.records.schema import AnalysisRecord
from pa_agent.util.threading import CancelToken
from tools.ab_intent import PREDICTIVE_RE, SCRUB_RULES

TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
INTENT_TYPES = {"限价单", "突破单", "市价单"}


def sanitize_text(text: str) -> str:
    out = text
    for new_wording, replacement in SCRUB_RULES:
        if new_wording in out:
            out = out.replace(new_wording, replacement)
    return out


def sanitize_obj(obj):
    """深度替换 dict/list 里所有字符串中的预言式拒单话术。"""
    if isinstance(obj, dict):
        return {k: sanitize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_obj(v) for v in obj]
    if isinstance(obj, str):
        return sanitize_text(obj)
    return obj


def count_pred(obj) -> int:
    if isinstance(obj, dict):
        return sum(count_pred(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_pred(v) for v in obj)
    if isinstance(obj, str):
        return len(PREDICTIVE_RE.findall(obj))
    return 0


def record_ts(raw: dict) -> int:
    return int((raw.get("meta") or {}).get("timestamp_local_ms") or 0)


def pick_chain(symbol: str, timeframe: str, start: str, rounds: int,
               end: str = "") -> tuple[dict, list[Path]]:
    """返回 (seed_raw, [链上每轮路径])。seed = 链首之前最近的一条同品种记录。"""
    cands: list[tuple[int, Path]] = []
    for p in sorted(RECORDS_PENDING_DIR.glob("*.json")):
        name = p.name
        if end and name[:16] >= end:
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = raw.get("meta") or {}
        if meta.get("symbol") != symbol or meta.get("timeframe") != timeframe:
            continue
        if not (raw.get("stage2_messages") or []):
            continue
        cands.append((record_ts(raw), p))
    cands.sort()
    idx = next((i for i, (_, p) in enumerate(cands) if p.name[:16] >= start), None)
    if idx is None or idx == 0:
        raise SystemExit(f"找不到 {symbol} {timeframe} 在 {start} 之后且带前序的链")
    seed = json.loads(cands[idx - 1][1].read_text(encoding="utf-8"))
    chain = [p for _, p in cands[idx:idx + rounds]]
    if not chain:
        raise SystemExit("链为空")
    return seed, chain


def call_stage(client, settings, messages: list[dict], label: str) -> dict:
    t0 = time.time()
    try:
        reply = client.stream_chat(
            messages,
            cancel_token=CancelToken(),
            thinking=bool(settings.provider.thinking),
            reasoning_effort=settings.provider.reasoning_effort,
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300], "elapsed_s": round(time.time() - t0, 1)}
    return {
        "content": reply.content or "",
        "elapsed_s": round(time.time() - t0, 1),
        "chars": len(reply.content or ""),
        "label": label,
    }


def json_body(text: str) -> str:
    fence = chr(96) * 3
    body = (text or "").strip()
    if body.startswith(fence):
        body = body.split(chr(10), 1)[-1].strip()
    if body.endswith(fence):
        body = body[: -len(fence)].strip()
    start, end = body.find("{"), body.rfind("}")
    return body[start:end + 1] if start >= 0 and end > start else body


def build_prev(raw: dict, *, stage1_messages, stage1_content, stage1_json,
               stage2_messages, stage2_content, stage2_json, sanitized: bool) -> AnalysisRecord:
    """把本轮的输入/输出装成下一轮要注入的 previous_record（sanitized 臂先洗一遍）。"""
    doc = copy.deepcopy(raw)
    doc.pop("_partial_reason", None)
    doc["stage1_messages"] = stage1_messages
    doc["stage1_response"] = {"content": stage1_content}
    doc["stage1_diagnosis"] = stage1_json or {}
    doc["stage2_messages"] = stage2_messages
    doc["stage2_response"] = {"content": stage2_content}
    doc["stage2_decision"] = (sanitize_obj(stage2_json) if sanitized else stage2_json) or {}
    return AnalysisRecord.model_validate(doc)


def run_arm(arm: str, seed: dict, chain: list[Path], *, settings, assembler, client,
            validator, dry_run: bool) -> list[dict]:
    sanitized = arm == "sanitized"
    prev = AnalysisRecord.model_validate(copy.deepcopy(seed))
    if sanitized and prev.stage2_decision:
        prev = prev.model_copy(update={"stage2_decision": sanitize_obj(prev.stage2_decision)})
    out: list[dict] = []
    tf_min = TF_MINUTES.get(str(prev.meta.timeframe), 30)
    for i, path in enumerate(chain, 1):
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta = raw.get("meta") or {}
        frame = frame_from_record_klines(
            raw["kline_data"], symbol=meta["symbol"], timeframe=meta["timeframe"],
            snapshot_ts_local_ms=meta.get("timestamp_local_ms"),
        )
        gap_bars = max(1, round((record_ts(raw) - int(prev.meta.timestamp_local_ms or 0)) / (tf_min * 60000)))
        injected_pred = count_pred(prev.stage2_decision)
        s1_msgs = assembler.build_incremental_stage1(
            frame, prev, gap_bars, provider_settings=settings.provider,
        )
        if dry_run:
            out.append({"arm": arm, "round": i, "record": path.name, "gap_bars": gap_bars,
                        "injected_pred": injected_pred, "dry": True,
                        "s1_chars": sum(len(m.get("content") or "") for m in s1_msgs)})
            print(f"  [{arm:9s}] r{i} {path.name[:26]} gap={gap_bars} 注入文案={injected_pred} "
                  f"s1={out[-1]['s1_chars']}字符 (dry)")
            continue
        s1 = call_stage(client, settings, s1_msgs, f"{arm}:r{i}:stage1")
        s1_json = {}
        if s1.get("content"):
            try:
                s1_json = json.loads(json_body(s1["content"]))
            except Exception:
                s1_json = {}
        if not s1_json:
            out.append({"arm": arm, "round": i, "record": path.name, "ok": False,
                        "error": "stage1 解析失败", "injected_pred": injected_pred})
            print(f"  [{arm:9s}] r{i} {path.name[:26]} stage1 失败")
            continue
        valid = validator.validate("stage1", s1["content"], kline_frame=frame,
                                   incremental_new_bar_count=gap_bars)
        s1_valid = not str(type(valid).__name__).endswith("ValidationError")
        s1_json = getattr(valid, "obj", None) or s1_json
        s2_msgs = assembler.build_stage2_continuation(
            frame=frame,
            stage1_messages=s1_msgs,
            stage1_reply_content=s1["content"],
            stage1_json=s1_json,
            strategy_files=list(raw.get("strategy_files_used") or []),
            experience_entries=list(raw.get("experience_loaded") or []),
            decision_stance=str(meta.get("decision_stance") or "conservative"),
            previous_record=prev,
            provider_settings=settings.provider,
            structure_flip_cooldown_bars=int(
                getattr(settings.general, "structure_flip_cooldown_bars", 3) or 3
            ),
        )
        s2 = call_stage(client, settings, s2_msgs, f"{arm}:r{i}:stage2")
        s2_json = {}
        if s2.get("content"):
            try:
                s2_json = json.loads(json_body(s2["content"]))
            except Exception:
                s2_json = {}
        order_type = ((s2_json.get("decision") or {}) if isinstance(s2_json, dict) else {}).get("order_type")
        produced_pred = count_pred(s2_json)
        row = {
            "arm": arm, "round": i, "record": path.name, "ok": bool(s2_json),
            "injected_pred": injected_pred, "produced_pred": produced_pred,
            "order_type": order_type, "intent": order_type in INTENT_TYPES,
            "s1_valid": s1_valid, "s1_elapsed": s1.get("elapsed_s"), "s2_elapsed": s2.get("elapsed_s"),
            "stage1_chars": s1.get("chars"), "stage2_chars": s2.get("chars"),
            "reasoning": str((s2_json.get("decision") or {}).get("reasoning") or "")[:200],
        }
        out.append(row)
        flag = "下单" if row["intent"] else ("失败" if not row["ok"] else "观望")
        print(f"  [{arm:9s}] r{i} {path.name[:26]} {flag} {order_type or ''} "
              f"注入={injected_pred} 产出={produced_pred} {row['s2_elapsed']}s")
        prev = build_prev(
            raw,
            stage1_messages=s1_msgs, stage1_content=s1["content"], stage1_json=s1_json,
            stage2_messages=s2_msgs, stage2_content=s2.get("content") or "", stage2_json=s2_json,
            sanitized=sanitized,
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="ZECUSDT")
    ap.add_argument("--timeframe", default="30m")
    ap.add_argument("--start", default="2026-09-19_13", help="链起点（文件名前缀）")
    ap.add_argument("--end", default="")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--arms", default="raw,sanitized")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="logs/ab_loop.jsonl")
    args = ap.parse_args()

    settings = load_settings()
    assembler = PromptAssembler(
        Path("prompt_engineering"), ExperienceReader(), prompt_settings=settings.prompt,
        binance_settings=settings.binance_usdm_testnet,
    )
    validator = JsonValidator(settings)
    client = create_ai_client(settings.provider)
    seed, chain = pick_chain(args.symbol, args.timeframe, args.start, args.rounds, args.end)
    print(f"链: {args.symbol} {args.timeframe} {len(chain)} 轮 | 起点 seed="
          f"{(seed.get('meta') or {}).get('timestamp_local_iso')} | 臂={args.arms}")

    rows: list[dict] = []
    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        rows += run_arm(arm, seed, chain, settings=settings, assembler=assembler,
                        client=client, validator=validator, dry_run=args.dry_run)

    if args.dry_run:
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n=== 汇总 ===")
    for arm, rs in sorted(collections.defaultdict(list, {
        a: [r for r in rows if r["arm"] == a] for a in {r["arm"] for r in rows}
    }).items()):
        ok = [r for r in rs if r.get("ok")]
        intent = [r for r in ok if r.get("intent")]
        rec = sum(1 for r in ok if r.get("produced_pred"))
        inj = [r.get("injected_pred", 0) for r in rs]
        print(f"  {arm:10s} 轮={len(rs)} 成功={len(ok)} 下单={len(intent)} "
              f"复发轮={rec}/{len(ok)} 平均注入文案={sum(inj) / max(1, len(inj)):.1f}")
    print(f"\n结果: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
