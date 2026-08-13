"""Quick test: call llm_raw_chat directly to check rate limit status."""
import json
import httpx
from pa_agent.ai.trae_connector import _get_trae_cn_info

info = _get_trae_cn_info()
host, token, device_info = info
device_id = device_info.get("device_id", "unknown")
machine_id = device_info.get("machine_id", "unknown")

headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "Authorization": f"Cloud-IDE-JWT {token}",
    "x-ide-token": token,
    "x-device-id": device_id,
    "x-machine-id": machine_id,
    "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
    "x-app-version": "default",
    "x-app-version-code": "20260806",
    "x-ide-version": "0.1.48",
    "x-ide-version-code": "20260806",
    "x-ide-version-type": "stable",
    "request-traffic-type": "prod",
}

payload = {
    "model_name": "glm-5.2",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "Reply with exactly: hello"}]}],
}

url = host.rstrip("/") + "/api/ide/v1/llm_raw_chat"
print(f"URL: {url}")

try:
    with httpx.stream("POST", url, headers=headers, json=payload, timeout=30) as resp:
        print(f"HTTP Status: {resp.status_code}")
        count = 0
        for line in resp.iter_lines():
            if line:
                print(f"  {line[:300]}")
                count += 1
                if count > 15:
                    print("  ... (truncated)")
                    break
        if count == 0:
            body = resp.read().decode("utf-8", errors="replace")[:500]
            print(f"  Body: {body}")
except Exception as e:
    print(f"ERROR: {e}")
