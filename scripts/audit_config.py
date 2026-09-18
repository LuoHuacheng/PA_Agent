"""审计 config/settings*.json 与代码模型是否一致.

查三类问题:
  1. JSON 里有、pydantic 模型没有的键 -> 会被静默忽略(僵尸键/写错名);
  2. 模型有、JSON 没写的字段 -> 静默走默认值(把"隐式默认"列出来, 便于决定是否显式化);
  3. 两个 env 块(binance_usdm_live / binance_usdm_testnet)的键集是否一致(计划 DoD 要求)。

用法(仓库根):
    .venv/bin/python scripts/audit_config.py                    # 审实盘配置
    .venv/bin/python scripts/audit_config.py --config config/settings.example.json
退出码: 0=无僵尸键, 1=发现僵尸键(或两 env 块键集不一致)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pa_agent.config.settings import Settings  # noqa: E402

ENV_BLOCKS = ("binance_usdm_live", "binance_usdm_testnet")


def audit(raw, model_cls=Settings):
    """-> {extra: [(section, key)], missing: {section: [field]}, env_parity: bool|None}."""
    known = set(model_cls.model_fields)
    extra = sorted((k, None) for k in set(raw) - known)
    missing = {}
    for key, value in raw.items():
        if not isinstance(value, dict) or key not in known:
            continue
        model = getattr(model_cls(**{}), key, None)  # 仅取类型信息, 不依赖实例
        fields = set(type(model).model_fields) if model is not None else set()
        if fields:
            extra += [(key, k) for k in sorted(set(value) - fields)]
            absent = sorted(fields - set(value))
            if absent:
                missing[key] = absent
    present = [b for b in ENV_BLOCKS if b in raw]
    parity = None
    if len(present) == 2:
        parity = set(raw[present[0]]) == set(raw[present[1]])
    return {"extra": sorted(extra), "missing": missing, "env_parity": parity}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/settings.json")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    path = Path(args.config)
    raw = json.loads(path.read_text(encoding="utf-8"))
    res = audit(raw)

    print(f"审计 {path}")
    bad = False
    if res["extra"]:
        bad = True
        print("  ❌ 僵尸键(模型没有, 会被静默忽略):")
        for section, key in res["extra"]:
            print(f"       {section}.{key}" if key else f"       {section} (整个 section)")
    else:
        print("  ✅ 无僵尸键")
    if res["env_parity"] is not None:
        print(f"  {'✅' if res['env_parity'] else '❌'} 两 env 块键集一致: {res['env_parity']}")
        bad = bad or not res["env_parity"]
    if res["missing"]:
        print("  ⓘ 未写->走默认值的字段:")
        for section, fields in res["missing"].items():
            print(f"       [{section}] {'/'.join(fields)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
