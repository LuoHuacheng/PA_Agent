"""Batch A/B: restored Stage-1 prompt vs stored production diagnosis.

Compares TRIMMED (current working-tree prompt: slim gate system + restored
role instructions + output contract in user turn) against the stored
stage1_diagnosis captured by production at record time (pre-trim baseline).

Usage:
    python tools/ab_test_batch.py [record.json ...] [--workers N]
    (no args -> picks the 10 oldest records in records/pending)
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_agent.config.paths import PROMPT_DIR, RECORDS_PENDING_DIR
from pa_agent.config.settings import load_settings
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.json_validator import JsonValidator, ValidationError
from pa_agent.demo.record_loader import frame_from_record_klines
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.util.threading import CancelToken

FIELDS = ["cycle_position", "direction", "market_phase", "transition_risk", "spike_stage"]


def build_prompt_with_files(settings, txt_files):
    """Build a PromptAssembler whose Stage-1 system files = *txt_files*."""
    import pa_agent.ai.prompt_assembler as pa

    orig_sys = pa.COMMON_SYSTEM_STAGE1_TXT_FILES
    orig_task = pa.STAGE1_TASK_PROMPT_TXT_FILES
    try:
        pa.COMMON_SYSTEM_STAGE1_TXT_FILES = tuple(txt_files["system"])
        pa.STAGE1_TASK_PROMPT_TXT_FILES = tuple(txt_files["task"])
        return PromptAssembler(PROMPT_DIR, ExperienceReader(), prompt_settings=settings.prompt)
    finally:
        pa.COMMON_SYSTEM_STAGE1_TXT_FILES = orig_sys
        pa.STAGE1_TASK_PROMPT_TXT_FILES = orig_task


def run_once(assembler, client, validator, frame, settings):
    messages = assembler.build_stage1(frame)
    reply = client.stream_chat(messages, cancel_token=CancelToken())
    res = validator.validate("stage1", reply.content, kline_frame=frame)
    if isinstance(res, ValidationError):
        return None, reply.content, res
    return res.obj, reply.content, None


def process_record(name: str, settings, client, validator, sem: threading.Semaphore) -> dict:
    with sem:
        raw = json.loads((RECORDS_PENDING_DIR / name).read_text(encoding="utf-8"))
    frame = frame_from_record_klines(
        raw["kline_data"],
        symbol=raw["meta"]["symbol"],
        timeframe=raw["meta"]["timeframe"],
    )
    stored = raw.get("stage1_diagnosis") or {}
    trimmed, content, err = run_once(
        build_prompt_with_files(
            settings,
            {
                "system": ("提示词大纲_人设与思维方式.txt", "二元决策_阶段一闸门.txt"),
                "task": ("市场诊断框架.txt", "文件16-K线信号识别.txt"),
            },
        ),
        client,
        validator,
        frame,
        settings,
    )
    result = {"name": name, "stored": stored, "trimmed": trimmed, "error": None}
    if err is not None:
        result["error"] = f"{err.category}: {err.message}"
        Path(f"/tmp/ab_batch_{name}").write_text(content or "", encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    names = args.names
    if not names:
        all_records = sorted(p.name for p in RECORDS_PENDING_DIR.glob("*.json"))
        names = all_records[:10]

    settings = load_settings()
    client = create_ai_client(settings.provider)
    validator = JsonValidator(settings)
    sem = threading.Semaphore(args.workers)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_record, n, settings, client, validator, sem): n for n in names}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"[done] {r['name']} error={r['error']}", flush=True)

    out = Path("/tmp/ab_batch_results.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # Aggregate agreement (skip records without a stored baseline diagnosis)
    ok, total = {}, {}
    valid_n = 0
    for r in results:
        if not r.get("stored"):
            print(f"== {r['name']}: SKIPPED (no stored baseline diagnosis)", flush=True)
            continue
        if r["error"] or not r["trimmed"]:
            print(f"== {r['name']}: FAILED {r['error']}", flush=True)
            continue
        valid_n += 1
        print(f"== {r['name']}", flush=True)
        for fld in FIELDS:
            s, t = r["stored"].get(fld), r["trimmed"].get(fld)
            agree = s == t
            total[fld] = total.get(fld, 0) + 1
            ok[fld] = ok.get(fld, 0) + (1 if agree else 0)
            if not agree:
                print(f"   {fld:16s} stored={s!r:35} trimmed={t!r}", flush=True)
    print(f"\n== 一致率（有效基线 {valid_n} 条）==", flush=True)
    for fld in FIELDS:
        if total.get(fld):
            print(
                f"   {fld:16s} {ok[fld]}/{total[fld]} = {ok[fld] / total[fld] * 100:.0f}%",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
