"""Test different TRAE Work CN API endpoints to find one without rate limiting.

Findings from log analysis:
- /api/ide/v1/llm_raw_chat  -> rate-limited (code 4011)
- /api/agent/v3/llm_utils_chat -> used by client for plugin recommendations
- /api/agent/v3/create_agent_task -> used by client for regular chat (agent orchestration)

The TRAE Work CN desktop client never hits llm_raw_chat directly. It routes
through the local ai-agent process via IPC (lite.send_message), which then
calls create_agent_task on the cloud. The server internally invokes
llm_raw_chat_v2 (note: v2 suffix, different from the v1 path we were using).
"""
import json
import uuid
import httpx

from pa_agent.ai.trae_connector import _get_trae_cn_info

info = _get_trae_cn_info()
if info is None:
    print("ERROR: TRAE Work CN not detected")
    raise SystemExit(1)

host, token, device_info = info
device_id = device_info.get("device_id", "unknown")
machine_id = device_info.get("machine_id", "unknown")

# Headers matching the TRAE Work CN desktop client (from ai-agent log line 3143).
# Key difference from previous test: x-app-id is the real TRAE app id, not "default".
trace_id = uuid.uuid4().hex
request_id = str(uuid.uuid4())
headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",  # real TRAE app id
    "x-app-version": "default",
    "x-app-version-code": "20260806",
    "x-ide-version": "0.1.48",
    "x-ide-version-code": "20260806",
    "x-ide-version-type": "stable",
    "x-custom-trace-id": trace_id,
    "x-flow-traceparent": f"04-{trace_id}-{uuid.uuid4().hex[:16]}-01",
    "x-device-id": device_id,
    "x-machine-id": machine_id,
    "x-device-brand": "MS-7D48",
    "x-device-cpu": "Intel",
    "x-device-type": "windows",
    "x-os-version": "Windows 10 Pro",
    "request-traffic-type": "prod",
    "x-request-id": request_id,
    "x-trae-request-id": request_id,
    "Authorization": f"Cloud-IDE-JWT {token}",
    "x-ide-token": token,
}


def try_endpoint(name: str, url: str, payload: dict) -> None:
    print(f"\n{'='*70}")
    print(f"Test: {name}")
    print(f"URL: {url}")
    print(f"Payload keys: {list(payload.keys())}")
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=30) as resp:
            print(f"HTTP Status: {resp.status_code}")
            collected = 0
            for line in resp.iter_lines():
                if line is None:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if line:
                    print(f"  {line[:200]}")
                    collected += 1
                    if collected > 15:
                        print("  ... (truncated)")
                        break
            if collected == 0:
                body = resp.read().decode("utf-8", errors="replace")[:500]
                print(f"  Body: {body}")
    except Exception as e:
        print(f"ERROR: {e}")


# Simple user message in TRAE block-list format.
simple_messages = [
    {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: hello"}]},
]

# Test 1: llm_raw_chat (known rate-limited) - just to confirm.
try_endpoint(
    "llm_raw_chat (v1, known rate-limited)",
    host.rstrip("/") + "/api/ide/v1/llm_raw_chat",
    {"messages": simple_messages, "model_name": "seed_m8"},
)

# Test 2: llm_utils_chat with same payload as llm_raw_chat.
try_endpoint(
    "llm_utils_chat (agent v3, same payload)",
    host.rstrip("/") + "/api/agent/v3/llm_utils_chat",
    {"messages": simple_messages, "model_name": "seed_m8"},
)

# Test 3: llm_utils_chat with config_name (from log: config_name=glm-5.2).
try_endpoint(
    "llm_utils_chat (with config_name)",
    host.rstrip("/") + "/api/agent/v3/llm_utils_chat",
    {
        "messages": simple_messages,
        "model_name": "glm-5.2",
        "config_name": "glm-5.2",
    },
)

# Test 4: llm_utils_chat with model_config object (mirrors ModelConfig from log).
try_endpoint(
    "llm_utils_chat (with model_config object)",
    host.rstrip("/") + "/api/agent/v3/llm_utils_chat",
    {
        "messages": simple_messages,
        "model_config": {
            "config_name": "glm-5.2",
            "model_name": "glm-5.2",
            "max_tokens": 4096,
        },
    },
)

print("\n" + "="*70)
print("Done.")
