"""Test create_agent_task endpoint — try multiple model configs to find what works.

The "model config is empty" error might be due to:
1. Wrong model name (glm-5.2 might have been removed from server registry)
2. Wrong config_source serialization (integer vs string)
3. model_info field not expected in request (server does its own lookup)
4. Missing extra_config fields

This script tries multiple combinations to isolate the issue.
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

url = host.rstrip("/") + "/api/agent/v3/create_agent_task"

COMMON_PARAMS = {
    "icube_uid": "2719552726382864",
    "user_id": "2719552726382864",
    "biz_user_id": "2719552726382864",
    "user_unique_id": "3951005750868043",
    "device_id": "3951005750868043",
    "local_device_id": "aha-4fd65aae10daaee6a0b74e5d2e8c59e9",
    "is_special_uuid": False,
    "machine_id": machine_id,
    "arch": "x64",
    "system": "win32",
    "scope": "marscode",
    "organization": "",
    "build_version": "2.3.68989",
    "vscode_version": "1.107.1",
    "tenant": "marscode",
    "region": "CN",
    "aiRegion": "CN",
    "quality": "stable",
    "build_time": "2026-08-10T10:34:45.538Z",
    "icube_main_uid": "206932ac-c4bc-4129-9f71-b83e2d9c4b38",
    "window_id": 1,
    "workspace_id": "a660eed132304605295884936a23de3d",
    "app_version": "0.1.48",
    "os_name": "windows",
    "os_version": "Windows 10 Pro",
    "os_release": "10.0.19045",
    "platform": "electron",
    "device_model": "MS-7D48",
    "device_manufacturer": "Micro-Star International Co., Ltd.",
    "cpu": "Intel",
    "cpu_brand": "12th Gen Intel(R) Core(TM) i5-12400",
    "cpu_speed": 2.496,
    "memory": 34118328320,
    "is_ssh": False,
    "language": "zh-cn",
    "app_language": "zh-cn",
    "chat_mode": 1,
    "identity": "1",
    "identity_str": "Pro",
    "is_freshman": "0",
    "channel_name": "common",
    "process_type": 2,
    "privacy_mode": "on",
    "aha_version": "39.2.7-release.1.46.1",
    "store_country_code": "",
    "store_country_code_src": "",
    "store_region": "CN",
    "workspace_status": "unsaved_multi_root",
    "workspace_root_count": 1,
    "product_code": "SOLO_Lite",
    "ai_chat_version": "v1",
    "ai_chat_version_source": "default",
    "app_is_solo_mode": "1",
    "icube_ab": '{"onboarding":"A"}',
    "solo_chat_mode": "code",
    "app_window_count": 1,
    "message_source": "manual",
}


def make_model_config(name: str, config_source=1, with_extra_config: bool = False):
    mc = {
        "provider": "",
        "is_preset": True,
        "config_name": name,
        "config_source": config_source,
        "model_name": name,
        "display_model_name": name.upper().replace("GLM-", "GLM-"),
        "ak": "",
        "base_url": "",
        "use_remote_service": True,
        "multimodal": False,
        "prompt_max_tokens": 168000,
        "toolcall_history_max_tokens": None,
        "encrypted_model_params": "",
        "extra_config": None,
        "function_extra_config": None,
        "ab_versions": None,
        "persist_meta": None,
        "raw_chat_function": None,
        "prompt_set": None,
        "context_window_sizes": None,
        "max_turn": 200,
        "display_options": None,
        "max_tokens": 32000,
        "application_config": None,
        "sk": "",
        "auth_type": 0,
        "region": None,
        "session_token": None,
        "user_custom_hyper_params": None,
        "custom_model_type": None,
        "reasoning_effort": None,
    }
    if with_extra_config:
        mc["extra_config"] = {
            "apply_file_path": True,
            "enable_invalid_json_hint": True,
            "is_new_pe": True,
            "native_function_call": True,
            "use_v2_process": True,
        }
    return mc


def make_payload(model_name: str, *, include_model_info: bool = True,
                 config_source=1, with_extra_config: bool = False,
                 common_params_as_str: bool = True):
    session_id = uuid.uuid4().hex[:24]
    message_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    payload = {
        "conversation_id": session_id,
        "session_id": session_id,
        "user_id": "2719552726382864",
        "device_id": device_id,
        "machine_id": machine_id,
        "agent_id": "solo_agent",
        "agent_type": "solo_agent",
        "cloud_agent_type": "solo_agent",
        "function": "solo_agent",
        "message_id": message_id,
        "task_id": task_id,
        "user_message_id": message_id,
        "reply_to_message_id": message_id,
        "turn_id": str(uuid.uuid4()),
        "query": "Reply with exactly: hello",
        "user_input": {
            "id": message_id,
            "query": f'[{{"type":"text","data":{{"content":"Reply with exactly: hello"}}}}]',
            "parsed_query": ["Reply with exactly: hello"],
            "turn_type": "default",
            "is_in_plan_mode": False,
            "is_in_spec_mode": False,
            "is_in_code_mode": False,
            "is_ralph_loop": False,
            "command_type": None,
            "asr_times": None,
        },
        "parsed_query": ["Reply with exactly: hello"],
        "model_name": model_name,
        "config_name": model_name,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply concisely."},
            {"role": "user", "content": "Reply with exactly: hello"},
        ],
        "agent_run_info": {
            "subagent_type": None,
            "task_id": None,
            "is_async": None,
            "agent_call_id": None,
            "parent_agent_run_id": None,
            "agent_run_id": None,
        },
        "enable_chat_memory_user_config": True,
        "enable_core_memory": False,
        "agent_process_support": "v3",
        "chat_process_version": "v3",
        "version_code": "20260806",
        "ide_version": "0.1.48",
        "ide_version_code": "20260806",
        "app_id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "app_version_code": "20260806",
        "request_client": "AhaNet",
        "shallow_memento_type": "disabled_by_remote",
        "agent_task_service_strategy": "cloud_agent",
        "model_smart_selection_meta": {"config_name": model_name, "mode": "manual"},
        "cluster_type": "k8s",
        "is_worktree": False,
        "mode_type": 1,
        "tools": [],
        "common_params": json.dumps(COMMON_PARAMS) if common_params_as_str else COMMON_PARAMS,
    }
    if include_model_info:
        payload["model_info"] = make_model_config(
            model_name, config_source=config_source, with_extra_config=with_extra_config
        )
    return payload


def try_request(label: str, payload: dict):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"Payload size: {len(json.dumps(payload))} bytes")
    try:
        with httpx.stream("POST", url, headers=headers, json=payload, timeout=30) as resp:
            print(f"HTTP Status: {resp.status_code}")
            lines_collected = []
            for line in resp.iter_lines():
                if line is None:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if line:
                    lines_collected.append(line[:300])
                    if len(lines_collected) > 8:
                        break
            if not lines_collected:
                body = resp.read().decode("utf-8", errors="replace")[:500]
                lines_collected.append(body)
            for l in lines_collected:
                print(f"  {l}")
    except Exception as e:
        print(f"  ERROR: {e}")


# Test 1: glm-5.2 with model_info, config_source=1
try_request("glm-5.2, model_info, config_source=1",
            make_payload("glm-5.2", include_model_info=True, config_source=1))

# Test 2: glm-5.2 with model_info, config_source="Trae"
try_request('glm-5.2, model_info, config_source="Trae"',
            make_payload("glm-5.2", include_model_info=True, config_source="Trae"))

# Test 3: glm-5.2 without model_info
try_request("glm-5.2, no model_info",
            make_payload("glm-5.2", include_model_info=False))

# Test 4: glm-5.1 with model_info
try_request("glm-5.1, model_info, config_source=1",
            make_payload("glm-5.1", include_model_info=True, config_source=1))

# Test 5: glm-5.1 without model_info
try_request("glm-5.1, no model_info",
            make_payload("glm-5.1", include_model_info=False))

# Test 6: glm-5.2 with extra_config filled
try_request("glm-5.2, model_info with extra_config",
            make_payload("glm-5.2", include_model_info=True, with_extra_config=True))

# Test 7: common_params as object (not string)
try_request("glm-5.2, common_params as object",
            make_payload("glm-5.2", include_model_info=True, common_params_as_str=False))
