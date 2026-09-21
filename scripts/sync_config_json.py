#!/usr/bin/env python3
"""让 config/ 下的 JSON 与 pa_agent/config/settings.py 的模型保持同步.

做三件事(默认全部 dry-run, 需显式开关才落盘):
  1. --write-example  把 config/settings.example.json 缺的键补齐(值取代码默认),
                      并把各节键顺序对齐模型声明顺序; 已有键的值一律不动.
  2. --sync-local     把本机 config/settings.json 缺的键补齐(只增不改不删),
                      写入前自动备份到 config/settings.json.bak_<时间戳>_pre_sync.
  3. --check          校验 example 是否已与代码同步(CI 用); 不同步 exit 1.

绝不修改任何已有键的值 —— 本机密钥/线上参数不会被动到.

用法(仓库根):
    .venv/bin/python scripts/sync_config_json.py --check
    .venv/bin/python scripts/sync_config_json.py --write-example
    .venv/bin/python scripts/sync_config_json.py --sync-local
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel  # noqa: E402

from pa_agent.config.settings import Settings  # noqa: E402

EXAMPLE = ROOT / "config" / "settings.example.json"
LOCAL = ROOT / "config" / "settings.json"


def code_default(field, model) -> object:
    """字段的代码默认值(dict/list 都会实参化), 必填字段返回 None."""
    try:
        value = model.model_fields[field].get_default(call_default_factory=True)
    except Exception:
        value = model.model_fields[field].default
    if isinstance(value, BaseModel):
        return value.model_dump()
    if str(value) == "PydanticUndefined":
        return None
    return value


def model_order(model) -> list:
    return list(model.model_fields)


def fill(prefix: str, model, data: dict, added: list) -> dict:
    """按模型声明顺序重建 dict: 已有值保留, 缺的键补代码默认."""
    out = {}
    for name in model_order(model):
        f = model.model_fields[name]
        ann = f.annotation
        nested = hasattr(ann, "model_fields")
        if name in data:
            value = data[name]
            if nested and isinstance(value, dict):
                value = fill(prefix + name + ".", ann, value, added)
        else:
            value = code_default(name, model)
            added.append(prefix + name)
            if nested and isinstance(value, dict):
                for k in value:
                    added.append(prefix + name + "." + k)
        out[name] = value
    # 模型不认识的遗留键保留在末尾(load_settings 用 extra=ignore, 不会报错)
    for name, value in data.items():
        if name not in out:
            out[name] = value
    return out


def render_example() -> str:
    current = json.loads(EXAMPLE.read_text(encoding="utf-8")) if EXAMPLE.exists() else {}
    added: list = []
    rebuilt = fill("", Settings, current, added)
    return json.dumps(rebuilt, ensure_ascii=False, indent=2) + "\n"


def sync_local() -> int:
    if not LOCAL.exists():
        print("本机 config/settings.json 不存在, 跳过")
        return 0
    current = json.loads(LOCAL.read_text(encoding="utf-8"))
    added: list = []
    rebuilt = fill("", Settings, current, added)
    if not added:
        print("本机 config/settings.json 无缺失键")
        return 0
    backup = LOCAL.with_name(f"settings.json.bak_{time.strftime('%Y%m%d_%H%M%S')}_pre_sync")
    shutil.copy2(LOCAL, backup)
    LOCAL.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"本机 config/settings.json 补入 {len(added)} 个键, 备份 -> {backup.name}")
    for key in added:
        print("  + " + key)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-example", action="store_true")
    ap.add_argument("--sync-local", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    rendered = render_example()
    if args.check:
        cur = EXAMPLE.read_text(encoding="utf-8") if EXAMPLE.exists() else ""
        if cur.strip() != rendered.strip():
            print("config/settings.example.json 与 settings.py 不同步; "
                  "重跑 scripts/sync_config_json.py --write-example")
            return 1
        print("config/settings.example.json 已同步")
        return 0

    did = False
    if args.write_example:
        EXAMPLE.write_text(rendered, encoding="utf-8")
        print("已写入 config/settings.example.json")
        did = True
    if args.sync_local:
        sync_local()
        did = True
    if not did:
        print("未指定动作; 可选 --write-example / --sync-local / --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
