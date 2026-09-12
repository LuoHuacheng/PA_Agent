"""LLM 输出契约共享原语: 跨 stage 重复的解析/校验助手唯一归属。

json_validator / stage1_normalizer / stage2_normalizer / trace_normalize /
coherence_checks / decision_nodes 曾各自维护同一份实现(枚举后缀剥离逐字
两份、§14 判定两份、trace 节点查找三份、K 线最大 seq 两份)。本 module
收编这些契约原语; stage 间差异留在各 normalizer 内部。

统一约定:
- node_id 匹配: strip 后字符串相等(trace 由模型生成, 空白宽松)。
- §14: answer=是 且 reason 不含否认短语才算触犯(防模型把"扫描完成"
  误写成"是"); reason 含否认短语时记 debug 并视为未触犯。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: reason 中出现即视为"模型答错方向"(实际未触犯)的短语。
SECTION14_DENIAL_PHRASES = (
    "未触犯", "未违反", "无触犯", "无违规", "通过扫描", "扫描通过", "无禁止", "未触发",
)

_ENUM_SEPARATORS = ("（", "(", "【", "[", "—", "–", " - ", "：", ":")


def strip_enum_suffix(raw: str) -> str:
    """Drop trailing annotations models append to closed enums (e.g. ``invalid（…）``)."""
    text = raw.strip()
    for sep in _ENUM_SEPARATORS:
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if head:
                return head
    return text


def find_trace_item(
    trace: list[dict[str, Any]] | None, node_id: str
) -> dict[str, Any] | None:
    """Return the trace item whose node_id matches (stripped) — first wins."""
    if not isinstance(trace, list):
        return None
    for item in trace:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id", "")).strip() == node_id:
            return item
    return None


def trace_node_answer(trace: Any, node_id: str) -> str | None:
    """Return the stripped answer text of *node_id*, or None when absent."""
    item = find_trace_item(trace, node_id)
    if item is None:
        return None
    return str(item.get("answer", "") or "").strip()


def trace_node_ids(trace: Any) -> list[str]:
    """Ordered node_ids present in the trace (dedup not applied)."""
    return [
        str(item.get("node_id", "")).strip()
        for item in (trace or [])
        if isinstance(item, dict) and item.get("node_id")
    ]


def section14_violated(trace: Any) -> bool:
    """True only when §14 answer is 是 AND the reason text confirms a violation.

    Background: §14 question is "是否触犯禁止行为清单？"
      answer=是  → violated (程序强制 order_type=不下单)
      answer=否  → not violated (can proceed)

    Some models incorrectly write answer=是 to mean "I completed the scan (no
    violations)". Cross-check the reason text: explicit denial phrases mean
    NOT violated. Safety hatch — the prompt specifies answer=否 for the
    no-violation case.
    """
    if not isinstance(trace, list):
        return False
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "")).strip()
        if not nid.startswith("14"):
            continue
        if str(item.get("answer", "")).strip() != "是":
            continue
        reason = str(item.get("reason", "") or "")
        if any(phrase in reason for phrase in SECTION14_DENIAL_PHRASES):
            logger.debug(
                "section14_violated: node %s answer=是 but reason contains denial "
                "phrase; treating as NOT violated (AI should use answer=否)",
                nid,
            )
            continue
        return True
    return False


def max_bar_seq_from_frame(kline_frame: Any) -> int | None:
    """Largest bar seq on the frame, or None when the frame is unusable."""
    bars = getattr(kline_frame, "bars", None) if kline_frame is not None else None
    if not bars:
        return None
    seqs = [int(getattr(b, "seq", 0)) for b in bars if getattr(b, "seq", None)]
    return max(seqs) if seqs else None
