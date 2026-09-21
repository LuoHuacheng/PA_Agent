# ruff: noqa: RUF001, RUF002, RUF003 - Chinese product copy
"""AI provider 前置自检：确认「网关活着 + 凭据有效 + 模型名在列」。

2026-09-21 那次静默停摆（网关 11:27 换了 proxyKey，客户端还是 09-18 的占位 key，
401 连打 4 轮、零信号、无人知晓）暴露的是：**坏 key 不会自己喊**。这里用一次
零 token 的 GET /v1/models 做黑盒探活；401 时再尽力读网关配置给出根因。

设计约束：
- 只做**读**探活，不改任何配置（自动同步 key 是另一个显式动作，不在这里）。
- 任何失败都不抛异常 —— 自检本身绝不能成为新的故障源。
- 探活不依赖网关实现：默认只打标准 /v1/models，读不到网关配置就降级只报状态码。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 默认探活超时（一次 /models，秒）。
DEFAULT_PROBE_TIMEOUT = 15.0

#: 网关配置文件候选路径：环境变量优先，其次本机已知的 inline-pass 网关。
GATEWAY_CONFIG_ENV = "PA_GATEWAY_CONFIG"
GATEWAY_CONFIG_CANDIDATES = (
    "~/Documents/ai/cline-pass-switcher/config.json",
    "~/.cline-pass-switcher/config.json",
)

#: http_get 注入点签名: (url, headers, timeout) -> (status, payload)。
HttpGet = Callable[[str, dict[str, str], float], "tuple[int, Any]"]


@dataclass(frozen=True)
class PreflightResult:
    """一次探活的结论。ok=False 时 kind 说明卡在哪一类。"""

    ok: bool
    kind: str
    message: str
    status: int | None = None
    diagnosis: str = ""
    models_count: int = 0
    latency_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        """一行给日志/CLI 的结论。"""
        head = "✅ 探活通过" if self.ok else "🚨 探活失败"
        parts = [head, self.message]
        if self.latency_ms:
            parts.append(f"耗时 {self.latency_ms}ms")
        line = " | ".join(parts)
        return f"{line}\n→ {self.diagnosis}" if self.diagnosis else line


def _maybe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _default_http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, Any]:
    """标准库探活；HTTP 错误码当正常结果返回，只有传输层错误才抛。"""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), _maybe_json(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = _maybe_json(exc.read())
        except Exception:
            body = None
        return int(exc.code), body


def _truncate(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _existing(path: Path) -> Path | None:
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def gateway_config_path(explicit: str | Path | None = None) -> Path | None:
    """定位网关配置文件：显式路径 > 环境变量 > 已知候选。

    显式路径与环境变量是**权威**的：点名了一个不存在的文件就返回 None，而不是
    悄悄退回别的候选文件 —— 否则诊断会把结论挂在用户没指定的配置上（测试也会
    读到开发机上的真实网关配置而失去隔离）。只有两者都没给时才走候选列表。
    """
    if explicit:
        return _existing(Path(explicit).expanduser())
    env_path = os.environ.get(GATEWAY_CONFIG_ENV, "").strip()
    if env_path:
        return _existing(Path(env_path).expanduser())
    for candidate in GATEWAY_CONFIG_CANDIDATES:
        found = _existing(Path(candidate).expanduser())
        if found is not None:
            return found
    return None


def diagnose_gateway_key(api_key: str, explicit: str | Path | None = None) -> str:
    """尽力读网关配置比对 proxyKey；读不到就返回空串（降级，不报错）。"""
    path = gateway_config_path(explicit)
    if path is None:
        return ""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("网关配置读取失败: %s", path, exc_info=True)
        return ""
    if not isinstance(config, dict):
        return ""
    proxy_key = str(config.get("proxyKey") or "").strip()
    ours = (api_key or "").strip()
    if not proxy_key:
        return (
            "网关未开鉴权（proxyKey 为空），任意非空 key 都应通过；仍 401 请核对 "
            "provider.base_url 是否指向该网关。"
        )
    if proxy_key == ours:
        return (
            f"网关 proxyKey 与本机 provider.api_key 一致（均 {len(ours)} 字符），401 另有原因："
            "查网关上游账号是否失效/欠费。"
        )
    return (
        f"网关 proxyKey 与本机不一致（网关 {len(proxy_key)} 字符 / 本机 {len(ours)} 字符，"
        f"配置 {path}）：把 config/settings.json 的 provider.api_key 同步为网关 config.json "
        "里的 proxyKey，然后重启 pa-monitor。"
    )


def probe_provider(
    settings: Any = None,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    http_get: HttpGet | None = None,
    gateway_config: str | Path | None = None,
) -> PreflightResult:
    """打一次 /v1/models，判定「凭据是否有效 + 模型名是否在列」。永不抛异常。"""
    provider = getattr(settings, "provider", None)
    base_url = str(getattr(provider, "base_url", "") or "").strip()
    api_key = str(getattr(provider, "api_key", "") or "").strip()
    model = str(getattr(provider, "model", "") or "").strip()
    if not base_url:
        return PreflightResult(False, "config", "provider.base_url 未配置")
    if not api_key:
        return PreflightResult(False, "config", "provider.api_key 为空（SDK 构造客户端即会失败）")

    url = base_url.rstrip("/") + "/models"
    getter = http_get or _default_http_get
    started = time.monotonic()
    try:
        status, payload = getter(url, {"Authorization": f"Bearer {api_key}"}, timeout)
    except Exception as exc:  # 传输层：DNS/连接/超时
        elapsed = int((time.monotonic() - started) * 1000)
        return PreflightResult(
            False,
            "network",
            f"连不上 {url}：{_truncate(exc)}",
            latency_ms=elapsed,
            diagnosis="网关进程是否在跑？base_url 是否正确？",
        )
    elapsed = int((time.monotonic() - started) * 1000)

    if status in (401, 403):
        return PreflightResult(
            False,
            "auth",
            f"HTTP {status} 鉴权被拒（{url}）",
            status=status,
            diagnosis=diagnose_gateway_key(api_key, gateway_config),
            latency_ms=elapsed,
        )
    if status != 200:
        body = payload.get("error") if isinstance(payload, dict) else payload
        return PreflightResult(
            False,
            "http_error",
            f"HTTP {status}（{url}）：{_truncate(body, 120)}",
            status=status,
            latency_ms=elapsed,
        )

    data = payload.get("data") if isinstance(payload, dict) else None
    models = [str(item.get("id")) for item in data or [] if isinstance(item, dict)]
    if model and models and model not in models:
        return PreflightResult(
            False,
            "model_missing",
            f"HTTP 200 但模型 {model} 不在网关清单（{len(models)} 个模型）",
            status=status,
            models_count=len(models),
            latency_ms=elapsed,
            diagnosis="网关若按需路由可忽略；否则核对 provider.model 拼写。",
        )
    target = f"，目标模型 {model} 在列" if model and models else ""
    return PreflightResult(
        True,
        "ok",
        f"HTTP 200，{len(models)} 个模型{target}",
        status=status,
        models_count=len(models),
        latency_ms=elapsed,
    )


def run_startup_preflight(settings: Any = None, *, notify: Callable[[str], None] | None = None) -> PreflightResult:
    """起服前置自检：写日志；失败时（可选）推一条 Telegram。只告警，不拦启动。

    不拦启动是刻意的：monitor 还担着持仓保护（止损/止盈/超时平仓/user-data 流），
    坏 key 拦下启动会连带丢掉这些，代价远大于多烧几轮分析。
    """
    result = probe_provider(settings)
    if result.ok:
        logger.info("Provider 前置自检通过：%s", result.message)
        return result
    logger.error("Provider 前置自检失败：%s", result.summary())
    if notify is not None:
        lines = [
            "🚨 PA Agent 起服自检失败：AI 凭据/网关不可用，分析将全灭",
            "（持仓保护不受影响，监控照常运行）。",
            result.message,
        ]
        if result.diagnosis:
            lines.append(f"→ {result.diagnosis}")
        try:
            notify("\n".join(lines))
        except Exception:
            logger.exception("起服自检告警推送失败")
    return result
