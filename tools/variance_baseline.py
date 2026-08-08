"""Model variance baseline: run the SAME prompt N times on one record,
measuring cycle_position/direction/gate_trace agreement across draws.

This quantifies the model's intrinsic run-to-run consistency, which is the
noise floor that any "trimmed vs original prompt" test must exceed.

Usage:
    python tools/variance_baseline.py <record.json> <n_runs> <trimmed|original> [out.json]
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_agent.config.paths import PROMPT_DIR, RECORDS_PENDING_DIR
from pa_agent.config.settings import load_settings
from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.json_validator import JsonValidator, ValidationError
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.demo.record_loader import frame_from_record_klines
from pa_agent.util.threading import CancelToken

GATE_NODES = ["1.1", "1.2", "1.3", "2.1", "2.2", "2.3", "2.4", "2.5"]
MAX_ATTEMPTS = 3


def _select_variant(variant: str) -> None:
    """Monkeypatch the Stage 1 file lists before any prompt is built."""
    import pa_agent.ai.prompt_assembler as pa

    if variant == "original":
        pa.COMMON_SYSTEM_STAGE1_TXT_FILES = (
            "提示词大纲_人设与思维方式.txt",
            "二元决策.txt",
        )
        pa.STAGE1_TASK_PROMPT_TXT_FILES = (
            "市场诊断框架.txt.bak",
            "文件16-K线信号识别.txt",
        )
    else:
        pa.COMMON_SYSTEM_STAGE1_TXT_FILES = (
            "提示词大纲_人设与思维方式.txt",
            "二元决策_阶段一闸门.txt",
        )
        pa.STAGE1_TASK_PROMPT_TXT_FILES = (
            "市场诊断框架.txt",
            "文件16-K线信号识别.txt",
        )


def _one_draw(assembler, client, validator, frame, settings):
    """One Stage 1 draw with retry on transient stream errors.

    Returns (diagnosis_dict, error_string).
    """
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            messages = assembler.build_stage1(frame)
            reply = client.stream_chat(
                messages,
                cancel_token=CancelToken(),
                thinking=bool(settings.provider.thinking),
                reasoning_effort=settings.provider.reasoning_effort,
            )
        except Exception as exc:  # noqa: BLE001 — transient provider stream drops
            last_err = f"API {type(exc).__name__}: {exc}"
            print(f"      attempt {attempt}/{MAX_ATTEMPTS} {last_err}", flush=True)
            time.sleep(5 * attempt)
            continue
        res = validator.validate("stage1", reply.content, kline_frame=frame)
        if isinstance(res, ValidationError):
            last_err = f"validation {res.category}: {res.message}"
            print(f"      attempt {attempt}/{MAX_ATTEMPTS} {last_err}", flush=True)
            continue
        return res.obj, ""
    return None, last_err


def _agreement(items: list) -> tuple[int, int, object]:
    """Return (modal_count, total, modal_value)."""
    if not items:
        return 0, 0, None
    counter = Counter(items)
    value, count = counter.most_common(1)[0]
    return count, len(items), value


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "2026-08-07_22-08-19_ETHUSDT_15m.json"
    n_runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    variant = sys.argv[3] if len(sys.argv) > 3 else "trimmed"
    out_path = Path(sys.argv[4]) if len(sys.argv) > 4 else None

    _select_variant(variant)

    settings = load_settings()
    client = create_ai_client(settings.provider)
    validator = JsonValidator(settings)
    assembler = PromptAssembler(
        PROMPT_DIR, ExperienceReader(), prompt_settings=settings.prompt
    )

    raw = json.loads((RECORDS_PENDING_DIR / name).read_text(encoding="utf-8"))
    frame = frame_from_record_klines(
        raw["kline_data"],
        symbol=raw["meta"]["symbol"],
        timeframe=raw["meta"]["timeframe"],
    )

    print(f"== {name} variant={variant} runs={n_runs}", flush=True)
    print(
        f"   model={settings.provider.model} thinking={settings.provider.thinking} "
        f"effort={settings.provider.reasoning_effort}",
        flush=True,
    )

    draws: list[dict] = []
    failures: list[str] = []
    for i in range(1, n_runs + 1):
        print(f"  -- run {i}/{n_runs}", flush=True)
        diag, err = _one_draw(assembler, client, validator, frame, settings)
        if diag is None:
            failures.append(err)
            print(f"  run{i}: GAVE UP ({err})", flush=True)
            continue
        gate = {
            g.get("node_id"): g.get("answer")
            for g in (diag.get("gate_trace") or [])
        }
        draws.append(
            {
                "cycle_position": diag.get("cycle_position"),
                "direction": diag.get("direction"),
                "market_phase": diag.get("market_phase"),
                "transition_risk": diag.get("transition_risk"),
                "gate": gate,
            }
        )
        print(
            f"  run{i}: cycle={diag.get('cycle_position')} "
            f"dir={diag.get('direction')} mp={diag.get('market_phase')} "
            f"tr={diag.get('transition_risk')}",
            flush=True,
        )
        print(f"         gate={gate}", flush=True)

    print("\n  === variance summary ===", flush=True)
    if not draws:
        print(f"  no successful draws ({len(failures)} failures)", flush=True)
        return 1

    cyc_n, cyc_t, cyc_v = _agreement([d["cycle_position"] for d in draws])
    dir_n, dir_t, dir_v = _agreement([d["direction"] for d in draws])
    print(
        f"  cycle_position: {cyc_n}/{cyc_t} = {100*cyc_n/cyc_t:.1f}% (mode={cyc_v})",
        flush=True,
    )
    print(
        f"  direction:      {dir_n}/{dir_t} = {100*dir_n/dir_t:.1f}% (mode={dir_v})",
        flush=True,
    )

    gate_agree = 0
    gate_total = 0
    per_node: dict[str, str] = {}
    for node in GATE_NODES:
        answers = [d["gate"].get(node) for d in draws if node in d["gate"]]
        if not answers:
            continue
        n_mode, n_tot, v_mode = _agreement(answers)
        gate_agree += n_mode
        gate_total += n_tot
        per_node[node] = f"{n_mode}/{n_tot} (mode={v_mode})"
        flag = "" if n_mode == n_tot else "  <-- varies"
        print(f"    node {node}: {per_node[node]}{flag}", flush=True)
    if gate_total:
        print(
            f"  gate_trace overall: {gate_agree}/{gate_total} = "
            f"{100*gate_agree/gate_total:.1f}%",
            flush=True,
        )

    total_agree = cyc_n + dir_n + gate_agree
    total_all = cyc_t + dir_t + gate_total
    combined = 100 * total_agree / total_all if total_all else 0.0
    print(
        f"  COMBINED baseline: {total_agree}/{total_all} = {combined:.1f}%",
        flush=True,
    )
    print(f"  draws ok={len(draws)} failed={len(failures)}", flush=True)

    if out_path is not None:
        out_path.write_text(
            json.dumps(
                {
                    "record": name,
                    "variant": variant,
                    "n_requested": n_runs,
                    "n_ok": len(draws),
                    "failures": failures,
                    "draws": draws,
                    "cycle_agreement": [cyc_n, cyc_t, cyc_v],
                    "direction_agreement": [dir_n, dir_t, dir_v],
                    "gate_agreement": [gate_agree, gate_total],
                    "gate_per_node": per_node,
                    "combined_pct": combined,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  saved -> {out_path}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
