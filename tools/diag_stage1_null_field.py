"""Diagnose which Stage1 field is null (schema 'None is not of type string')."""
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
from pa_agent.ai.prompts.schemas import STAGE1_SCHEMA
import jsonschema


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "2026-08-07_23-08-09_BTCUSDT_15m.json"
    settings = load_settings()
    client = create_ai_client(settings.provider)
    assembler = PromptAssembler(
        PROMPT_DIR, ExperienceReader(), prompt_settings=settings.prompt
    )
    path = RECORDS_PENDING_DIR / name
    raw = json.loads(path.read_text(encoding="utf-8"))
    frame = frame_from_record_klines(
        raw["kline_data"], symbol=raw["meta"]["symbol"], timeframe=raw["meta"]["timeframe"]
    )
    messages = assembler.build_stage1(frame)
    reply = client.stream_chat(
        messages,
        cancel_token=CancelToken(),
        thinking=bool(settings.provider.thinking),
        reasoning_effort=settings.provider.reasoning_effort,
    )
    content = reply.content or ""
    Path("/tmp/stage1_raw_" + name).write_text(content, encoding="utf-8")
    print(f"raw saved /tmp/stage1_raw_{name} len={len(content)}", flush=True)

    # Parse and run raw jsonschema
    try:
        obj = json.loads(content)
    except Exception as exc:
        print("parse fail", exc, flush=True)
        return 1
    norm = JsonValidator(settings).normalize_parsed("stage1", obj, kline_frame=frame)
    print("normalized keys:", sorted(norm.keys()), flush=True)
    errs = list(jsonschema.Draft7Validator(STAGE1_SCHEMA).iter_errors(norm))
    print(f"schema errors: {len(errs)}", flush=True)
    for e in errs:
        print("  path:", ".".join(str(p) for p in e.absolute_path) or "(root)",
              "| validator:", e.validator, "| msg:", e.message[:120], flush=True)
        if e.validator == "required":
            missing = [m for m in e.message.split("'")[1::2]]
            print("    missing fields:", missing, flush=True)
    # Also show null-valued fields
    for k, v in norm.items():
        if v is None and k in STAGE1_SCHEMA.get("properties", {}):
            print("NULL top-level field:", k, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
