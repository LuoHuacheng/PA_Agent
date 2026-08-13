"""Call batch_get_detail_param to list available models for solo_agent function.

This endpoint was called by the TRAE Work CN client before create_agent_task
(see ai-agent log line 685). It returns model configurations for each function.
"""
import json
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
    "Accept": "application/json",
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

url = host.rstrip("/") + "/api/ide/v1/batch_get_detail_param"

# Payload from log line 685
payload = {
    "functions": [
        "ui_builder_v2", "solo_coder", "chat_v3", "solo_builder", "builder_v3",
        "builder", "chat", "inline_chat", "git_ai", "custom_agent_generation",
        "utils", "code_reviewer", "code_review_summary", "solo_agent",
        "solo_agent_remote", "solo_work_remote", "solo_agent_lite",
        "solo_work_lite", "solo_design_lite", "solo_design_remote",
        "multimodal", "system_diagnosis"
    ],
    "agent_type": "",
    "current_config_info": {
        "config_name": "",
        "is_custom_model": False
    },
    "mode_type": "Manual",
    "access_type": "Default",
    "ab_force_vids": "",
    "ab_autotest_advanced_mode": 0
}

print(f"URL: {url}")
print(f"Payload size: {len(json.dumps(payload))} bytes")
print()

try:
    resp = httpx.post(url, headers=headers, json=payload, timeout=30)
    print(f"HTTP Status: {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")
    print()
    try:
        data = resp.json()
        # Pretty print, but limit output
        formatted = json.dumps(data, indent=2, ensure_ascii=False)
        # Print first 8000 chars
        if len(formatted) > 8000:
            print(formatted[:8000])
            print(f"\n... (truncated, total {len(formatted)} chars)")
        else:
            print(formatted)
    except Exception:
        text = resp.text[:3000]
        print(f"Non-JSON response ({len(resp.text)} chars):")
        print(text)
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
