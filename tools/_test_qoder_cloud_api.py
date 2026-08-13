"""Try to find Qoder CN's direct cloud LLM API."""
import json
import requests

TOKEN_PATH = r"C:\Users\Administrator\AppData\Roaming\QoderCN\SharedClientCache\cache\machine_token.json"
with open(TOKEN_PATH, "r", encoding="utf-8") as f:
    MACHINE_TOKEN = json.load(f)["token"]

# Try various endpoints
HOSTS = [
    "https://gateway.qoder.com.cn",
    "https://openapi.qoder.com.cn",
    "https://qts.qoder.com.cn",
]

PATHS = [
    "/api/v1/chat/completions",
    "/api/v1/llm/chat",
    "/api/v1/llm/raw_chat",
    "/api/v1/model/chat",
    "/api/v1/inference/chat",
    "/api/v2/chat/completions",
    "/api/v2/llm/chat",
    "/algo/api/v1/chat/completions",
    "/algo/api/v1/llm/raw_chat",
    "/api/v1/qcs/chat/completions",
]

HEADERS = {
    "Content-Type": "application/json",
    "Cosy-MachineToken": MACHINE_TOKEN,
    "Authorization": f"Bearer {MACHINE_TOKEN}",
}

BODY = {
    "model": "mmodel",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
    "max_tokens": 100,
}

for host in HOSTS:
    for path in PATHS:
        url = host + path
        try:
            r = requests.post(url, headers=HEADERS, json=BODY, timeout=5, verify=True)
            status = r.status_code
            body_preview = r.text[:200] if r.text else "(empty)"
            if status != 404:
                print(f"[{status}] {url}")
                print(f"  body: {body_preview}")
        except requests.exceptions.ConnectionError:
            pass
        except requests.exceptions.Timeout:
            print(f"[TIMEOUT] {url}")
        except Exception as e:
            print(f"[ERR] {url}: {e}")
