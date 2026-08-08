"""A/B: same kline data, trimmed vs original Stage 1 prompt — record 1 cycle mismatch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_agent.config.paths import PROMPT_DIR, RECORDS_PENDING_DIR
from pa_agent.config.settings import load_settings
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.json_validator import JsonValidator, Ok, ValidationError
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.demo.record_loader import frame_from_record_klines
from pa_agent.util.threading import CancelToken


def build_prompt_with_files(settings, txt_files):
    """Build a PromptAssembler whose Stage-1 system files = *txt_files*.
    We patch module-level tuples before constructing so the assembler picks
    them up, then restore afterward (process cache keyed by tuple).
    """
    import pa_agent.ai.prompt_assembler as pa

    orig_sys = pa.COMMON_SYSTEM_STAGE1_TXT_FILES
    orig_task = pa.STAGE1_TASK_PROMPT_TXT_FILES
    try:
        pa.COMMON_SYSTEM_STAGE1_TXT_FILES = tuple(txt_files["system"])
        pa.STAGE1_TASK_PROMPT_TXT_FILES = tuple(txt_files["task"])
        assembler = PromptAssembler(
            PROMPT_DIR, ExperienceReader(), prompt_settings=settings.prompt
        )
        return assembler
    finally:
        pa.COMMON_SYSTEM_STAGE1_TXT_FILES = orig_sys
        pa.STAGE1_TASK_PROMPT_TXT_FILES = orig_task


def run(assembler, client, validator, frame, settings):
    messages = assembler.build_stage1(frame)
    reply = client.stream_chat(
        messages,
        cancel_token=CancelToken(),
        thinking=bool(settings.provider.thinking),
        reasoning_effort=settings.provider.reasoning_effort,
    )
    res = validator.validate("stage1", reply.content, kline_frame=frame)
    if isinstance(res, ValidationError):
        return None, reply.content, res
    return res.obj, reply.content, None


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "2026-08-07_22-08-19_ETHUSDT_15m.json"
    settings = load_settings()
    client = create_ai_client(settings.provider)
    validator = JsonValidator(settings)
    path = RECORDS_PENDING_DIR / name
    raw = json.loads(path.read_text(encoding="utf-8"))
    frame = frame_from_record_klines(
        raw["kline_data"], symbol=raw["meta"]["symbol"], timeframe=raw["meta"]["timeframe"]
    )
    stored = raw.get("stage1_diagnosis") or {}
    print(f"== record {name}", flush=True)
    print(f"   stored(original): cycle={stored.get('cycle_position')} dir={stored.get('direction')} "
          f"market_phase={stored.get('market_phase')} transition_risk={stored.get('transition_risk')}", flush=True)

    configs = {
        "TRIMMED": {
            "system": ("提示词大纲_人设与思维方式.txt", "二元决策_阶段一闸门.txt"),
            "task": ("市场诊断框架.txt", "文件16-K线信号识别.txt"),
        },
        "ORIGINAL": {
            "system": ("提示词大纲_人设与思维方式.txt", "二元决策.txt"),
            "task": ("市场诊断框架.txt.bak", "文件16-K线信号识别.txt"),
        },
    }
    results = {}
    for label, files in configs.items():
        assembler = build_prompt_with_files(settings, files)
        diag, content, err = run(assembler, client, validator, frame, settings)
        if err is not None:
            print(f"   [{label}] FAILED: {err.category} {err.message}", flush=True)
            print(f"   [{label}] invalid={err.invalid_fields}", flush=True)
            Path(f"/tmp/ab_{label}_{name}").write_text(content or "", encoding="utf-8")
            results[label] = None
            continue
        gt = {g.get("node_id"): g.get("answer") for g in (diag.get("gate_trace") or [])}
        print(f"   [{label}] cycle={diag.get('cycle_position')} dir={diag.get('direction')} "
              f"market_phase={diag.get('market_phase')} transition_risk={diag.get('transition_risk')}", flush=True)
        print(f"   [{label}] gate={gt}", flush=True)
        results[label] = diag
        Path(f"/tmp/ab_{label}_{name}").write_text(content or "", encoding="utf-8")

    if results.get("TRIMMED") and results.get("ORIGINAL"):
        t, o = results["TRIMMED"], results["ORIGINAL"]
        print(f"\n   cycle: TRIMMED={t.get('cycle_position')} ORIGINAL={o.get('cycle_position')} "
              f"agree={t.get('cycle_position') == o.get('cycle_position')}", flush=True)
        print(f"   direction: TRIMMED={t.get('direction')} ORIGINAL={o.get('direction')} "
              f"agree={t.get('direction') == o.get('direction')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
