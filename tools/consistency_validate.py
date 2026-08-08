"""一致性验证：用精简后（gate）Stage 1 prompt 对历史分析记录做实时 Stage 1，
与库中记录的 stage1_diagnosis（=精简前 full prompt 产物）逐项对比。

对比字段：cycle_position / direction / gate_trace 各节点 answer。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Project root, not tools/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_agent.config.paths import PROMPT_DIR, RECORDS_PENDING_DIR
from pa_agent.config.settings import load_settings
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.demo.record_loader import frame_from_record_klines
from pa_agent.util.threading import CancelToken


GATE_NODES = ["1.1", "1.2", "1.3", "2.1", "2.2", "2.3", "2.4", "2.5"]


def build_components():
    settings = load_settings()
    client = create_ai_client(settings.provider)
    exp_reader = ExperienceReader()
    assembler = PromptAssembler(
        prompt_dir=PROMPT_DIR,
        experience_reader=exp_reader,
        prompt_settings=settings.prompt,
    )
    validator = JsonValidator(settings)
    return settings, client, assembler, validator


def run_stage1(
    assembler,
    client,
    validator,
    frame,
    *,
    settings,
    thinking: bool,
    reasoning_effort: str,
):
    """Run Stage 1 with the same validate+retry path as production."""
    from pa_agent.ai.json_validator import ValidationError, coalesce_model_json_text
    from pa_agent.orchestrator.validation_retry import validate_with_retry

    messages = assembler.build_stage1(frame)

    def call_api(msgs):
        return client.stream_chat(
            msgs,
            cancel_token=CancelToken(),
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )

    reply = call_api(messages)
    retry = validate_with_retry(
        stage="stage1",
        messages=messages,
        reply=reply,
        validator=validator,
        validation_settings=settings.validation,
        validate_kwargs={"kline_frame": frame},
        call_api=call_api,
        provider_settings=settings.provider,
    )
    content = coalesce_model_json_text(
        getattr(retry.reply, "content", None) or "",
        getattr(retry.reply, "reasoning_content", None) or "",
    )
    if isinstance(retry.result, ValidationError):
        return None, content, retry.result
    return retry.result.obj, content, None


def extract_diag(diag: dict) -> dict:
    """Extract comparison-relevant fields from a stage1 diagnosis."""
    gt = {g.get("node_id"): g.get("answer") for g in (diag.get("gate_trace") or [])}
    return {
        "cycle_position": diag.get("cycle_position"),
        "direction": diag.get("direction"),
        "gate_trace": gt,
    }


def compare(ref: dict, new: dict) -> dict:
    """Return per-field agreement summary."""
    out = {}
    out["cycle_position"] = 1 if ref.get("cycle_position") == new.get("cycle_position") else 0
    out["direction"] = 1 if ref.get("direction") == new.get("direction") else 0
    ref_gt = ref.get("gate_trace") or {}
    new_gt = new.get("gate_trace") or {}
    gt_agree = 0
    gt_total = 0
    for node in GATE_NODES:
        if node in ref_gt or node in new_gt:
            gt_total += 1
            if ref_gt.get(node) == new_gt.get(node):
                gt_agree += 1
    out["gate_trace"] = (gt_agree, gt_total)
    return out


def main() -> int:
    settings, client, assembler, validator = build_components()
    thinking = bool(settings.provider.thinking)
    effort = settings.provider.reasoning_effort
    print(
        f"provider: model={settings.provider.model} base_url={settings.provider.base_url} "
        f"thinking={thinking} effort={effort}",
        flush=True,
    )
    print(flush=True)

    # Prefer diverse cycle_position samples first, then remaining usable records.
    candidates: list[tuple[Path, dict, dict]] = []
    for path in sorted(RECORDS_PENDING_DIR.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stored = raw.get("stage1_diagnosis")
        if not isinstance(stored, dict) or not stored.get("cycle_position"):
            continue
        candidates.append((path, raw, stored))

    seen_cp: set[str] = set()
    ordered: list[tuple[Path, dict, dict]] = []
    for item in candidates:
        cp = item[2].get("cycle_position")
        if cp not in seen_cp:
            ordered.append(item)
            seen_cp.add(str(cp))
    for item in candidates:
        if item not in ordered:
            ordered.append(item)

    print(f"usable records with stage1_diagnosis: {len(ordered)}", flush=True)
    results: list[tuple] = []
    for path, raw, stored in ordered:
        name = path.name
        print(f"\n--- running Stage1 on {name} (ref={stored.get('cycle_position')}/{stored.get('direction')}) ---", flush=True)
        frame = frame_from_record_klines(
            raw["kline_data"],
            symbol=raw["meta"]["symbol"],
            timeframe=raw["meta"]["timeframe"],
        )
        try:
            new_diag, content, err = run_stage1(
                assembler,
                client,
                validator,
                frame,
                settings=settings,
                thinking=thinking,
                reasoning_effort=effort,
            )
        except Exception as exc:  # noqa: BLE001 — surface live API failures
            print(f"[{name}] Stage1 API ERROR: {exc}", flush=True)
            results.append((name, stored, None, exc))
            continue
        if err is not None:
            print(f"[{name}] Stage1 validation FAILED: {err.category} {err.message}", flush=True)
            results.append((name, stored, None, err))
            continue
        cmp = compare(extract_diag(stored), extract_diag(new_diag))
        results.append((name, stored, new_diag, cmp))
        print(
            f"[{name}] cycle_ref={stored.get('cycle_position')} new={new_diag.get('cycle_position')} agree={cmp['cycle_position']}",
            flush=True,
        )
        print(
            f"          direction_ref={stored.get('direction')} new={new_diag.get('direction')} agree={cmp['direction']}",
            flush=True,
        )
        print(f"          gate_trace={cmp['gate_trace'][0]}/{cmp['gate_trace'][1]}", flush=True)
        # Per-node mismatch detail for debugging
        ref_gt = extract_diag(stored)["gate_trace"]
        new_gt = extract_diag(new_diag)["gate_trace"]
        for node in GATE_NODES:
            if ref_gt.get(node) != new_gt.get(node):
                print(
                    f"          MISMATCH node {node}: ref={ref_gt.get(node)!r} new={new_gt.get(node)!r}",
                    flush=True,
                )

    # Aggregate
    print("\n=== 汇总 ===", flush=True)
    total_cycle = 0
    total_dir = 0
    total_gt_a = 0
    total_gt_t = 0
    n = 0
    failed = 0
    for name, stored, new_diag, cmp in results:
        if new_diag is None:
            failed += 1
            continue
        n += 1
        total_cycle += cmp["cycle_position"]
        total_dir += cmp["direction"]
        total_gt_a += cmp["gate_trace"][0]
        total_gt_t += cmp["gate_trace"][1]
    if n:
        cycle_pct = 100 * total_cycle / n
        dir_pct = 100 * total_dir / n
        gt_pct = 100 * total_gt_a / total_gt_t if total_gt_t else 0.0
        # Combined field-level agreement (cycle + direction + each gate node)
        field_agree = total_cycle + total_dir + total_gt_a
        field_total = n + n + total_gt_t
        field_pct = 100 * field_agree / field_total if field_total else 0.0
        print(f"有效对比记录数: {n} (失败/校验错误: {failed})", flush=True)
        print(f"cycle_position 一致率: {total_cycle}/{n} = {cycle_pct:.1f}%", flush=True)
        print(f"direction 一致率: {total_dir}/{n} = {dir_pct:.1f}%", flush=True)
        print(f"gate_trace 节点一致率: {total_gt_a}/{total_gt_t} = {gt_pct:.1f}%", flush=True)
        print(f"综合字段一致率: {field_agree}/{field_total} = {field_pct:.1f}%", flush=True)
        target = 95.0
        ok = cycle_pct >= target and dir_pct >= target and gt_pct >= target
        print(f"目标 >={target}%: {'PASS' if ok else 'FAIL'}", flush=True)
        return 0 if ok else 2
    print(f"无可对比记录 (失败/校验错误: {failed})", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())