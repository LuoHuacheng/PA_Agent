# ruff: noqa: RUF001, RUF002, RUF003 - Chinese product copy
"""两阶段分析连续失败告警（密钥失效 / 代理拒绝 / 网络中断）。

监控进程以前只在每轮写一条 "分析未产生有效阶段二决策" 日志：AI 密钥一旦失效，
信号就静默停摆，无人值守时谁也不知情。这里把连续失败折算成一条 Telegram 提醒，
恢复后再补一条 "已恢复"，让"信号停了"这件事自己冒头。

阈值取 "整轮监控品种全灭"（默认 5），之后每累计 20 次失败补一条（约 1 小时一轮
5 品种，即约 2 轮），既不漏停摆也不刷屏。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: 连续失败达到该次数即首次告警（默认 5 = 一轮 5 个监控品种全灭）。
ALERT_AFTER_FAILURES = 5
#: 首次告警后，每再累计这么多次失败补一次告警。
REPEAT_EVERY_FAILURES = 20

#: 密钥/鉴权类失败的关键词。本地代理与常见 OpenAI 兼容网关的原文都含
#: 401 / unauthorized；中文网关（cline-pass-switcher）另给 "代理密钥缺失或错误"。
_AUTH_HINTS = (
    "401",
    "unauthorized",
    "代理密钥",
    "auth_error",
    "invalid api key",
    "invalid_api_key",
)


def classify_failure(message: str) -> str:
    """按错误原文归类: auth（密钥/鉴权）或 network（传输/上游）。"""
    lowered = (message or "").lower()
    return "auth" if any(hint in lowered for hint in _AUTH_HINTS) else "network"


def _kind_label(kind: str) -> str:
    return "密钥/鉴权" if kind == "auth" else "网络"


def _stage_label(stage: str) -> str:
    return {"stage1": "阶段一", "stage2": "阶段二"}.get(stage, stage or "分析")


def _hint_for(kind: str) -> str:
    if kind == "auth":
        # 2026-09-21 那次静默停摆就是这个形状: 代理换了 proxyKey, 客户端还是旧值。
        return (
            "多半是本机代理/网关的密钥变了：核对 config/settings.json 的 "
            "provider.api_key 与代理配置里的 proxyKey 是否一致，改完重启 pa-monitor。"
        )
    return "上游或网络不稳，先观察；持续不恢复时检查代理进程与网关连通性。"


def format_failure_alert(
    *, kind: str, count: int, stage: str = "", message: str = ""
) -> str:
    """首次/重复告警文案：说清停了几次、卡在哪、下一步看哪里。"""
    detail = " ".join((message or "").split())[:200]
    lines = [
        f"🚨 PA Agent 分析连续失败 {count} 次（{_stage_label(stage)}｜{_kind_label(kind)}）",
        "监控仍在按 K 线收盘运行，但本轮起不产生新信号。",
    ]
    if detail:
        lines.append(f"最近错误：{detail}")
    lines.append(f"→ {_hint_for(kind)}")
    return "\n".join(lines)


def format_recovery_alert(*, count: int, kind: str = "") -> str:
    """恢复通知：只有打扰过用户的失败轮才补发。"""
    return (
        f"✅ PA Agent 分析已恢复（此前连续失败 {count} 次，{_kind_label(kind)}）。"
        "信号恢复推送。"
    )


class AnalysisFailureAlerter:
    """连续失败计数 + 节流告警。

    多品种分析跑在线程池里（默认并发 3），计数与状态读写都在锁内；
    推送在锁外执行，网络慢不会卡住分析线程。
    """

    def __init__(
        self,
        notify: Callable[[str], None],
        *,
        alert_after: int = ALERT_AFTER_FAILURES,
        repeat_every: int = REPEAT_EVERY_FAILURES,
    ) -> None:
        self._notify = notify
        self._alert_after = max(1, int(alert_after))
        self._repeat_every = max(1, int(repeat_every))
        self._lock = threading.Lock()
        self._streak = 0
        self._kind = ""
        self._stage = ""
        self._message = ""
        self._alerted = False

    @property
    def streak(self) -> int:
        """当前连续失败次数（任意一次成功即清零）。"""
        with self._lock:
            return self._streak

    def note_failure(self, *, stage: str = "", message: str = "") -> None:
        """记一次 AI 调用失败；命中阈值时推送告警。"""
        with self._lock:
            self._streak += 1
            self._kind = classify_failure(message)
            self._stage = stage or self._stage
            self._message = message or self._message
            count = self._streak
            due = count == self._alert_after or (
                count > self._alert_after
                and (count - self._alert_after) % self._repeat_every == 0
            )
            if due:
                self._alerted = True
                text = format_failure_alert(
                    kind=self._kind,
                    count=count,
                    stage=self._stage,
                    message=self._message,
                )
            else:
                text = ""
        if text:
            self._safe_notify(text)

    def note_success(self) -> None:
        """记一次正常完成的分析；若之前告警过，补一条恢复通知。"""
        with self._lock:
            if not self._alerted:
                self._streak = 0
                return
            text = format_recovery_alert(count=self._streak, kind=self._kind)
            self._streak = 0
            self._alerted = False
        self._safe_notify(text)

    def note_record(self, record: Any) -> None:
        """按分析记录归类：exception.type == network_error 算失败，其余算完成。

        record 为 None 时无从判断，不计入任何一边（避免把"没跑起来"错报成恢复）。
        """
        exc = getattr(record, "exception", None) if record is not None else None
        if isinstance(exc, Mapping) and exc.get("type") == "network_error":
            self.note_failure(
                stage=str(exc.get("stage") or ""),
                message=str(exc.get("message") or ""),
            )
        elif record is not None:
            self.note_success()

    def _safe_notify(self, text: str) -> None:
        try:
            self._notify(text)
        except Exception:  # 告警是尽力而为，绝不因此打断监控
            logger.exception("分析失败告警推送失败")
