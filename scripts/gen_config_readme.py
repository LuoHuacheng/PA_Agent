#!/usr/bin/env python3
# ruff: noqa: RUF001 - 中文文档文案, 全角标点是有意的
"""从 pa_agent/config/settings.py 的 Pydantic 模型 + 本机 config/settings.json
生成 config/README.md 的字段表, 保证文档与代码、本机配置三方一致.

- 字段与默认值来源: settings.py 的模型定义(权威)
- 说明来源: settings.py 中字段上方的注释块(不另写一份, 避免再次漂移)
- 本机当前值: config/settings.json(缺失 = 该节本机未配置)
- 密钥类字段一律打码, 不进 README

用法(仓库根):
    .venv/bin/python scripts/gen_config_readme.py            # 写 config/README.md
    .venv/bin/python scripts/gen_config_readme.py --check    # 只校验是否已同步(CI 用)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pa_agent.config.settings import (  # noqa: E402
    AIProviderSettings,
    AutoDiscoverSettings,
    BinanceUSDMTestnetSettings,
    FeedbackSettings,
    FeishuSettings,
    GeneralSettings,
    MonitoringSettings,
    MonitorTarget,
    PromptSettings,
    PushPlusSettings,
    TelegramSettings,
    ValidationSettings,
)

SETTINGS_PY = ROOT / "pa_agent" / "config" / "settings.py"
LOCAL_JSON = ROOT / "config" / "settings.json"
README = ROOT / "config" / "README.md"

BT = chr(96)          # 反引号, 避免本文件里出现裸反引号
PIPE = "\\|"

#: 精确字段名(叶子名)匹配 —— 不要用子串, 否则 max_output_tokens 也会被打码
SECRET_FIELDS = {
    "api_key", "api_secret", "secret", "app_secret", "token", "bot_token",
    "password", "tradingview_password", "tradingview_username",
    "webhook_url", "chat_id", "user_data_stream_ws_url",
}

TYPE_ALIASES = {
    "DataSourceKind": "mt5 / tradingview",
    "DecisionStance": "conservative / balanced / aggressive / extreme_aggressive",
    "NormalizationMode": "strict / lenient",
}


def cell(text) -> str:
    """Markdown 表格单元: 竖线必须转义, 否则列会错位."""
    return str(text).replace("|", PIPE)

FIELD_RE = re.compile(r"^    ([a-z_][a-z0-9_]*)\s*:\s*([^=]+?)\s*=\s*(.+)$")
CLASS_RE = re.compile(r"^class (\w+)\(")

MONITORING_NOTE = f"""不提供 GUI 管理。直接编辑 {BT}settings.json{BT} 后重启程序生效。每个目标在自身 K 线自然收盘、再等待
{BT}poll_lead_seconds{BT} 的数据延迟后拉取一次行情，不按秒轮询。目标共用当前
{BT}general.last_data_source{BT} 与 {BT}general.last_tradingview_exchange{BT}，但使用独立订阅，互不覆盖。
{BT}targets{BT} 每项为 {BT}{{"symbol": "BTCUSDT", "timeframe": "30m", "enabled": true}}{BT}。
状态存于本地忽略文件 {BT}config/monitoring_state.json{BT}。
"""

AUTO_DISCOVER_NOTE = f"""开启后 {BT}targets{BT} 静态列表被替换为按 24h 行情动态排名的 USDⓈ-M 合约，运行中周期性刷新。
"""

BINANCE_NOTE = f"""
不提供 GUI 管理，直接编辑 {BT}settings.json{BT} 后重启生效。{BT}binance_usdm_testnet{BT} 与
{BT}binance_usdm_live{BT} **字段同构**（同一份 Pydantic 模型），由根级
{BT}binance_usdm_environment{BT}（{BT}"testnet"{BT} / {BT}"live"{BT}，默认 {BT}testnet{BT}）选择活动环境；
两节同时 {BT}enabled{BT}、或切到 {BT}live{BT} 而 live 节缺密钥时**直接拒绝启动**。
下表"本机当前值"一列取自本机 {BT}settings.json{BT} 的 **testnet 节**。
"""


def parse_sections() -> dict:
    """从 settings.py 源码抓每个类的字段顺序 + 上方注释块."""
    out: dict = {}
    current = None
    pending: list = []
    for raw in SETTINGS_PY.read_text(encoding="utf-8").splitlines():
        m = CLASS_RE.match(raw)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            pending = []
            continue
        stripped = raw.strip()
        if stripped.startswith("#"):
            text = stripped.lstrip("#").lstrip(": ").strip()
            pending.append(text)
            continue
        fm = FIELD_RE.match(raw)
        if fm and current:
            out[current].append({
                "name": fm.group(1),
                "annotation": fm.group(2).strip(),
                "comment": " ".join(x for x in pending if x).strip(),
            })
            pending = []
            continue
        if stripped:
            pending = []
    return out


def model_field_meta(model, name):
    f = model.model_fields.get(name)
    if f is None:
        return None, []
    try:
        default = f.get_default(call_default_factory=True)
    except Exception:
        default = f.default
    bits = []
    for meta in f.metadata:
        for attr, label in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            v = getattr(meta, attr, None)
            if v is not None:
                bits.append(f"{label}{v}")
    return default, bits


def fmt_type(annotation: str) -> str:
    a = annotation.strip()
    if a in TYPE_ALIASES:
        return TYPE_ALIASES[a]
    if a.startswith("Literal["):
        a = a[len("Literal["):-1].replace('"', BT)
    return a


def fmt_default(value, annotation: str) -> str:
    if value is None or str(value) == "PydanticUndefined":
        if str(value) == "PydanticUndefined":
            return "**必填**"
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False) if value else "[]"
    if isinstance(value, str):
        return BT + value + BT if value else BT + '""' + BT
    return BT + str(value) + BT


def fmt_local(val) -> str:
    if val == "—":
        return "—（未配置）"
    if isinstance(val, list):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, bool):
        return "true" if val else "false"
    return BT + str(val) + BT


def is_secret(name: str) -> bool:
    return name in SECRET_FIELDS


def flat(obj, prefix=""):
    out = {}
    for k, v in (obj or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flat(v, key))
        else:
            out[key] = v
    return out


SECTIONS = [
    ("provider", "provider — AI 提供商", AIProviderSettings, "provider", ""),
    ("general", "general — 通用设置", GeneralSettings, "general", ""),
    ("prompt", "prompt — Prompt 组装调优", PromptSettings, "prompt", ""),
    ("validation", "validation — 校验与重试", ValidationSettings, "validation", ""),
    ("monitoring", "monitoring — 多品种周期预警", MonitoringSettings, "monitoring", MONITORING_NOTE),
    ("monitoring.auto_discover", "monitoring.auto_discover — 监控标的自动发现",
     AutoDiscoverSettings, "monitoring.auto_discover", AUTO_DISCOVER_NOTE),
    ("feedback", "feedback — 结果回灌闭环", FeedbackSettings, "feedback", ""),
    ("feishu", "feishu — 飞书通知", FeishuSettings, "feishu", ""),
    ("pushplus", "pushplus — PushPlus 通知", PushPlusSettings, "pushplus", ""),
    ("telegram", "telegram — Telegram Bot 通知", TelegramSettings, "telegram", ""),
]


def table(parsed, model, prefix, local, value_prefix=None) -> list:
    lines = ["| 字段 | 类型 | 代码默认值 | 本机当前值 | 说明 |",
             "|------|------|-----------|-----------|------|"]
    for f in parsed.get(model.__name__) or []:
        name = f["name"]
        default, bits = model_field_meta(model, name)
        dtype = fmt_type(f["annotation"])
        if bits:
            dtype += "（" + ", ".join(bits) + "）"
        path = f"{prefix}.{name}"
        val = local.get(value_prefix + "." + name if value_prefix else path, "—")
        if is_secret(name):
            local_txt = "已打码" if val not in ("", None, [], {}, "—") else "空"
        else:
            local_txt = fmt_local(val)
        desc = cell(f["comment"]) or "—"
        lines.append(f"| {BT}{name}{BT} | {cell(dtype)} | {fmt_default(default, f['annotation'])} | "
                     f"{cell(local_txt)} | {desc} |")
    lines.append("")
    return lines


def render() -> str:
    parsed = parse_sections()
    local = {}
    if LOCAL_JSON.exists():
        local = flat(json.loads(LOCAL_JSON.read_text(encoding="utf-8")))

    head_path = ROOT / "config" / "_readme_head.md.txt"
    tail_path = ROOT / "config" / "_readme_tail.md.txt"
    head = head_path.read_text(encoding="utf-8").rstrip() if head_path.exists() else "# 本地配置说明"
    tail = tail_path.read_text(encoding="utf-8").rstrip() if tail_path.exists() else ""

    out = [head, "", "## settings.json 字段说明", ""]
    out.append(
        "字段、默认值与取值范围来自 " + BT + "pa_agent/config/settings.py" + BT
        + " 的 Pydantic 模型（权威来源）；说明取自代码中字段上方的注释。"
        + "**本机当前值**一列读自本机 " + BT + "config/settings.json" + BT + "。"
        + "改完代码或配置后重跑 " + BT + "python scripts/gen_config_readme.py" + BT + " 即可同步本文档。"
    )
    out.append("")
    out.append("根级 " + BT + 'binance_usdm_environment' + BT + "：" + BT + '"testnet"' + BT
               + "（默认）/ " + BT + '"live"' + BT + "。")
    out.append("")

    for _key, title, model, prefix, note in SECTIONS:
        out.append("### " + title)
        out.append("")
        if note:
            out.append(note.strip())
            out.append("")
        out.extend(table(parsed, model, prefix, local))
        if prefix == "monitoring":
            out.append("### monitoring.targets 条目")
            out.append("")
            out.append("| 字段 | 类型 | 代码默认值 | 说明 |")
            out.append("|------|------|-----------|------|")
            for f in parsed.get("MonitorTarget") or []:
                default, bits = model_field_meta(MonitorTarget, f["name"])
                dtype = fmt_type(f["annotation"])
                if bits:
                    dtype += "（" + ", ".join(bits) + "）"
                out.append(f"| {BT}{f['name']}{BT} | {cell(dtype)} | "
                           f"{fmt_default(default, f['annotation'])} | {cell(f['comment']) or '—'} |")

            out.append("")

    out.append("### binance_usdm_testnet / binance_usdm_live — 自动执行")
    out.append("")
    out.append(BINANCE_NOTE.strip())
    out.append("")
    out.extend(table(parsed, BinanceUSDMTestnetSettings, "binance_usdm_testnet", local,
                     value_prefix="binance_usdm_testnet"))

    if tail:
        out.append(tail)
        out.append("")
    return "\n".join(out)


def check_fields() -> int:
    """不依赖本机 settings.json 的覆盖率校验: 每个模型叶子字段都必须出现在 README 里."""
    readme = README.read_text(encoding="utf-8") if README.exists() else ""
    missing: list = []
    models = [
        (AIProviderSettings, "provider"),
        (GeneralSettings, "general"),
        (PromptSettings, "prompt"),
        (ValidationSettings, "validation"),
        (MonitoringSettings, "monitoring"),
        (AutoDiscoverSettings, "monitoring.auto_discover"),
        (MonitorTarget, "monitoring.targets"),
        (FeedbackSettings, "feedback"),
        (FeishuSettings, "feishu"),
        (PushPlusSettings, "pushplus"),
        (TelegramSettings, "telegram"),
        (BinanceUSDMTestnetSettings, "binance_usdm_*"),
    ]
    for model, prefix in models:
        for name in model.model_fields:
            if f"| {BT}{name}{BT} |" not in readme:
                missing.append(f"{prefix}.{name}")
    if "binance_usdm_environment" not in readme:
        missing.append("binance_usdm_environment")
    if missing:
        print("config/README.md 缺以下字段, 重跑 scripts/gen_config_readme.py:")
        for m in missing:
            print("  - " + m)
        return 1
    print("config/README.md 字段覆盖完整")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--check-fields", action="store_true",
                    help="只校验字段覆盖(不需要本机 settings.json), CI 用")
    args = ap.parse_args()
    if args.check_fields:
        sys.exit(check_fields())
    text = render()
    if args.check:
        cur = README.read_text(encoding="utf-8") if README.exists() else ""
        if cur.strip() != text.strip():
            print("config/README.md 与代码/本机配置不同步, 请重跑 scripts/gen_config_readme.py")
            sys.exit(1)
        print("config/README.md 已同步")
        sys.exit(0)
    README.write_text(text, encoding="utf-8")
    print(f"已写入 {README.relative_to(ROOT)} ({len(text.splitlines())} 行)")
