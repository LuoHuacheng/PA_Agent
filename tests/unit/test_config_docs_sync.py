"""配置资产(example/README)与 settings.py 模型的一致性守卫.

防的是这一类漂移: 代码里新加了设置项, 但 config/settings.example.json 没补键、
config/README.md 没写说明, 使用者照着模板配却发现字段不存在。

生成/修复:
    .venv/bin/python scripts/gen_config_readme.py
    .venv/bin/python scripts/sync_config_json.py --write-example
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa_agent.config.settings import Settings
from scripts.gen_config_readme import check_fields
from scripts.sync_config_json import EXAMPLE, render_example

ROOT = Path(__file__).resolve().parents[2]


def _leaf_paths(model, prefix: str = "") -> set:
    out: set = set()
    for name, field in model.model_fields.items():
        key = f"{prefix}.{name}" if prefix else name
        ann = field.annotation
        if hasattr(ann, "model_fields"):
            out |= _leaf_paths(ann, key)
        else:
            out.add(key)
    return out


def _flat(data: dict, prefix: str = "") -> set:
    out: set = set()
    for k, v in (data or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= _flat(v, key)
        else:
            out.add(key)
    return out


def test_example_covers_every_model_field():
    doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    code = _leaf_paths(Settings)
    have = _flat(doc)
    assert not (code - have), f"example 缺字段: {sorted(code - have)}"
    assert not (have - code), f"example 有模型不认识的键: {sorted(have - code)}"


def test_example_is_in_sync_with_renderer():
    """example 必须与 scripts/sync_config_json.py 的渲染结果一致(顺序 + 键集合)."""
    assert EXAMPLE.read_text(encoding="utf-8").strip() == render_example().strip()


def test_readme_documents_every_field():
    assert check_fields() == 0


def test_example_loads_through_settings_loader(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    loaded = Settings.model_validate(json.loads(target.read_text(encoding="utf-8")))
    assert loaded.binance_usdm_environment == "testnet"
    # 模板必须是"安全"的: 不会一拷就自动下单
    assert loaded.binance_usdm_testnet.dry_run is True
    assert loaded.binance_usdm_testnet.emergency_stop is True


@pytest.mark.skipif(
    not (ROOT / "config" / "settings.json").exists(),
    reason="本机 settings.json 不存在(CI/新克隆), 跳过本地值比对",
)
def test_local_readme_in_sync_with_local_settings():
    """本机 README 的"本机当前值"列必须与 config/settings.json 对齐."""
    from scripts.gen_config_readme import README, render

    assert README.read_text(encoding="utf-8").strip() == render().strip()
