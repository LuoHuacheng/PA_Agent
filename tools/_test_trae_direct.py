"""Minimal test: call TRAE Work CN llm_raw_chat directly."""
import json
import uuid
import httpx

# Read token from TRAE connector
from pa_agent.ai.trae_connector import _get_trae_cn_info, _TRAE_API_CHAT_PATH

info = _get_trae_cn_info()
if info is None:
    print("ERROR: TRAE Work CN not detected")
    raise SystemExit(1)

host, token, device_info = info
print(f"Host: {host}")
print(f"Token (first 30): {token[:30]}...")
print(f"Device info: {json.dumps(device_info, indent=2, ensure_ascii=False)}")

device_id = device_info.get("device_id", "unknown")
machine_id = device_info.get("machine_id", "unknown")

# Build request
trace_id = uuid.uuid4().hex
request_id = str(uuid.uuid4())
headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-app-id": "default",
    "x-app-version": "default",
    "x-app-version-code": "20260630",
    "x-ide-version": "3.3.76",
    "x-ide-version-code": "20260630",
    "x-ide-version-type": "stable",
    "x-custom-trace-id": trace_id,
    "x-flow-traceparent": f"04-{trace_id}-{uuid.uuid4().hex[:16]}-01",
    "x-device-id": device_id,
    "x-machine-id": machine_id,
    "x-device-brand": "PA-Agent",
    "x-device-cpu": "Intel",
    "x-device-type": "windows",
    "x-os-version": "Windows",
    "request-traffic-type": "prod",
    "x-request-id": request_id,
    "x-trae-request-id": request_id,
    "Authorization": f"Cloud-IDE-JWT {token}",
    "x-ide-token": token,
}

payload = {
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: hello"}]},
    ],
    "model_name": "seed_m8",
}

url = host.rstrip("/") + _TRAE_API_CHAT_PATH
print(f"\nURL: {url}")
print(f"Sending request...")

events = []
try:
    with httpx.stream("POST", url, headers=headers, json=payload, timeout=60) as resp:
        print(f"HTTP Status: {resp.status_code}")
        print(f"Response Headers: {dict(resp.headers)}")
        sse_buffer = ""
        for line in resp.iter_lines():
            if line is None:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8", errors="replace")
            if line:
                sse_buffer += line + "\n"
                continue
            if sse_buffer.strip():
                # Parse SSE event
                event = ""
                data_parts = []
                for l in sse_buffer.splitlines():
                    if l.startswith("event:"):
                        event = l[6:].strip()
                    elif l.startswith("data:"):
                        data_parts.append(l[5:].lstrip())
                data_str = "\n".join(data_parts)
                events.append((event, data_str[:200]))
                print(f"  [event={event}] data={data_str[:300]}")
                sse_buffer = ""
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()

print(f"\nTotal events: {len(events)}")
