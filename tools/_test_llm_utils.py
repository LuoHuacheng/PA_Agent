"""Test llm_utils_chat with different 'function'/'usage' field values.

Error from previous test:
  [LLMUtilsChat.resolveByUsage] function is empty, cannot resolve model by usage=

This suggests the endpoint needs a 'function' field to resolve which model to use.
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
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=20) as resp:
            for line in resp.iter_lines():
                if line is None:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if line:
                    print(f"  {line[:250]}")
    except Exception as e:
        print(f"  ERROR: {e}")


# Try various function values (the error says "function is empty").
for fn in ("chat", "recommend_plugin", "default", "llm_chat", "raw_chat", "qa"):
    try_payload(
        f"function={fn}",
        {"messages": simple_messages, "function": fn},
    )

# Try usage field.
for u in ("chat", "recommend_plugin", "default", "llm_chat"):
    try_payload(
        f"usage={u}",
        {"messages": simple_messages, "usage": u},
    )

# Try function + usage.
try_payload(
    "function=chat + usage=chat",
    {"messages": simple_messages, "function": "chat", "usage": "chat"},
)

# Try with model_name + function (no config_name to avoid rate limit path).
try_payload(
    "model_name=glm-5.2 + function=chat",
    {"messages": simple_messages, "model_name": "glm-5.2", "function": "chat"},
)

print("\nDone.")
