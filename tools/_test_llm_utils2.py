"""Test llm_utils_chat with function=solo_agent_lite (from log analysis).

Log line 3122 shows: function=solo_agent_lite, config_name=glm-5.2, model_name=glm-5.2
This is the function value used by create_agent_task. Maybe llm_utils_chat uses the same.
"""
import uuid
import httpx

from pa_agent.ai.trae_connector import _get_trae_cn_info

info = _get_trae_cn_info()
host, token, device_info = info
device_id = device_info.get("device_id", "unknown")
machine_id = device_info.get("machine_id", "unknown")

trace_id = uuid.uuid4().hex
request_id = str(uuid.uuid4())
headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
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

url = host.rstrip("/") + "/api/agent/v3/llm_utils_chat"
simple_messages = [
    {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: hello"}]},
]


def try_payload(name: str, payload: dict) -> None:
    print(f"\n--- {name} ---")
    print(f"  keys: {list(payload.keys())}")
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=30) as resp:
            collected = 0
            for line in resp.iter_lines():
                if line is None:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if line:
                    print(f"  {line[:300]}")
                    collected += 1
                    if collected > 20:
                        print("  ... (truncated)")
                        break
    except Exception as e:
        print(f"  ERROR: {e}")


# Test 1: function=solo_agent_lite (from create_agent_task log)
try_payload(
    "function=solo_agent_lite",
    {"messages": simple_messages, "function": "solo_agent_lite"},
)

# Test 2: function=solo_agent_lite + config_name=glm-5.2
try_payload(
    "function=solo_agent_lite + config_name=glm-5.2",
    {"messages": simple_messages, "function": "solo_agent_lite", "config_name": "glm-5.2"},
)

# Test 3: function=solo_agent_lite + model_name=glm-5.2
try_payload(
    "function=solo_agent_lite + model_name=glm-5.2",
    {"messages": simple_messages, "function": "solo_agent_lite", "model_name": "glm-5.2"},
)

# Test 4: function=solo_agent_lite + model_name=glm-5.2__dev
try_payload(
    "function=solo_agent_lite + model_name=glm-5.2__dev",
    {"messages": simple_messages, "function": "solo_agent_lite", "model_name": "glm-5.2__dev"},
)

# Test 5: function=solo_agent_lite + config_name=glm-5.2 + model_name=glm-5.2
try_payload(
    "function=solo_agent_lite + config + model",
    {
        "messages": simple_messages,
        "function": "solo_agent_lite",
        "config_name": "glm-5.2",
        "model_name": "glm-5.2",
    },
)

print("\nDone.")
