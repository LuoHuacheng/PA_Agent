# ruff: noqa: RUF001, RUF002, RUF003 - Chinese product copy
"""入口门控拦截台账 + 回灌摘要（prompt 侧）。

Why this exists (measured 2026-09-22)
-------------------------------------
BTCUSDT 13:01 / 13:32 / 14:01 连续三轮提交了同一个做空限价方案，三轮回放都被入口门控
``short_block`` 拦下。执行标记（``records/execution_marker.py``）只解决了"被拦方案不被
连续性守卫当成在场挂单"这一半；另一半是：**模型下一轮不知道这个方向刚被硬拦过**，于是
每 30 分钟重新论证一遍同一方向——白烧 token，反应也更慢。

拦截原因此前只落在两处：1MB 的 ``records/pending`` 完整记录（查询要全文反序列化）和
通知推送（人看得到，模型看不到）。这里补一条小而快的审计线：

* :func:`record_gate_block` —— 拦截即时追加一行 JSONL（与 cancel_log 同风格，
  never raise），含方向、门控键与原文；
* :func:`read_gate_blocks` / :func:`summarize_gate_blocks` —— 把最近 N 小时内同一品种的
  拦截压成一段紧凑 prompt 文本，注回下一轮阶段一/阶段二。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pa_agent.config.paths import GATE_BLOCK_LOG_DIR

logger = logging.getLogger(__name__)

_JSON_SEPARATORS = (",", ":")

#: 摘要里每个门控键保留的原文长度上限（超长原文按字符截断）。
_DETAIL_CHAR_CAP = 120


def gate_block_log_path(*, now: float | None = None, base_dir: Path | str | None = None) -> Path:
    """Return the daily gate-block ledger path covering the local date of "now"."""
    directory = Path(base_dir) if base_dir is not None else GATE_BLOCK_LOG_DIR
    stamp = time.time() if now is None else now
    return directory / f"gate_blocks-{datetime.fromtimestamp(stamp):%Y-%m-%d}.jsonl"


def split_gate_reason(reason: str) -> tuple[str, str]:
    """Split a gate reason string into (key, detail).

    Gate reasons are built as ``"<key>: <human detail>"`` (see
    ``signal_pipeline._entry_block_reasons``), e.g.
    ``"rr_block: 计划盈亏比 1.38 < 1.5"`` -> ``("rr_block", "计划盈亏比 ...")``.
    """
    key, _, detail = str(reason or "").partition(":")
    return key.strip(), detail.strip()


def record_gate_block(
    *,
    symbol: str,
    timeframe: str = "",
    direction: str = "",
    gate_reasons: Sequence[str] | Iterable[str] = (),
    entry_price: Any = None,
    base_dir: Path | str | None = None,
    now: float | None = None,
) -> Path | None:
    """Append one gate-block event; returns the file written, or None on failure.

    Never raises: a blocked signal is a fact even when the audit line cannot be
    written, and the audit trail must not be able to abort the order flow.
    """
    try:
        reasons = [str(r) for r in gate_reasons if str(r or "").strip()]
        if not reasons:
            return None
        keys: list[str] = []
        details: dict[str, str] = {}
        for reason in reasons:
            key, detail = split_gate_reason(reason)
            if not key:
                continue
            if key not in keys:
                keys.append(key)
            if detail and key not in details:
                details[key] = detail
        if not keys:
            return None

        stamp = time.time() if now is None else now
        path = gate_block_log_path(now=stamp, base_dir=base_dir)
        record: dict[str, Any] = {
            "ts_ms": int(stamp * 1000),
            "ts_iso": datetime.fromtimestamp(stamp).isoformat(timespec="seconds"),
            "symbol": str(symbol or ""),
            "timeframe": str(timeframe or ""),
            "direction": str(direction or ""),
            "gates": keys,
            "details": details,
            "entry_price": None if entry_price is None else str(entry_price),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=_JSON_SEPARATORS) + "\n")
        return path
    except Exception as exc:  # a gate audit write must never fail a trade
        logger.warning("Gate-block audit write failed for %s: %s", symbol, exc)
        return None


def _ledger_paths(cutoff: float, *, base_dir: Path | str | None = None) -> list[Path]:
    """Gate-block files whose local date could still hold rows newer than *cutoff*."""
    directory = Path(base_dir) if base_dir is not None else GATE_BLOCK_LOG_DIR
    if not directory.is_dir():
        return []
    first_day = (datetime.fromtimestamp(cutoff) - timedelta(days=1)).strftime("%Y-%m-%d")
    paths = [
        p
        for p in directory.glob("gate_blocks-*.jsonl")
        if p.is_file() and p.name.replace("gate_blocks-", "").replace(".jsonl", "") >= first_day
    ]
    paths.sort()
    return paths


def read_gate_blocks(
    *,
    symbol: str | None = None,
    hours: float = 4.0,
    base_dir: Path | str | None = None,
    now: float | None = None,
) -> list[dict]:
    """Recent gate-block rows, oldest first; unreadable lines are skipped.

    Always returns a list: the caller injects this into a prompt, so a missing
    directory or a corrupted line must degrade to "no feedback", never to an
    exception in the analysis path.
    """
    stamp = time.time() if now is None else now
    try:
        window_hours = max(0.0, float(hours))
    except (TypeError, ValueError):
        window_hours = 4.0
    cutoff_ms = int((stamp - window_hours * 3600.0) * 1000)
    rows: list[dict] = []
    for path in _ledger_paths(stamp - window_hours * 3600.0, base_dir=base_dir):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.debug("Gate-block ledger unreadable (%s): %s", path, exc)
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            if int(row.get("ts_ms") or 0) < cutoff_ms:
                continue
            if symbol and str(row.get("symbol") or "") != str(symbol):
                continue
            rows.append(row)
    rows.sort(key=lambda r: int(r.get("ts_ms") or 0))
    return rows


def summarize_gate_blocks(rows: Sequence[dict], *, max_chars: int = 400) -> str:
    """Render recent gate blocks as one compact prompt block ("" when empty).

    Grouped by (direction, gate key) so three identical ``short_block`` hits read
    as one line with a count and a time list, not three near-identical lines.
    """
    if not rows:
        return ""
    try:
        budget = max(80, int(max_chars))
    except (TypeError, ValueError):
        budget = 400

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        direction = str(row.get("direction") or "").strip() or "方向未记"
        gates = row.get("gates") or []
        if not isinstance(gates, (list, tuple)):
            gates = []
        details = row.get("details")
        if not isinstance(details, dict):
            details = {}
        ts_ms = int(row.get("ts_ms") or 0)
        stamp = datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M") if ts_ms else "?"
        for gate in gates:
            key = str(gate or "").strip()
            if not key:
                continue
            group_key = (direction, key)
            if group_key not in groups:
                groups[group_key] = {"times": [], "detail": str(details.get(key) or "")}
                order.append(group_key)
            groups[group_key]["times"].append(stamp)
            if not groups[group_key]["detail"]:
                groups[group_key]["detail"] = str(details.get(key) or "")

    if not order:
        return ""

    header = "【近期入口门控拦截（程序侧硬拦，这些方案实际没有下单）】"
    footer = "同一方向再次被拦的概率高：除非出现新的结构事件，不要重复提交已被拦的同向方案。"
    lines = [header]
    for group_key in order:
        direction, key = group_key
        info = groups[group_key]
        times = "/".join(dict.fromkeys(info["times"]))
        detail = str(info["detail"] or "")
        if len(detail) > _DETAIL_CHAR_CAP:
            detail = detail[: _DETAIL_CHAR_CAP - 1] + "…"
        suffix = f" — {detail}" if detail else ""
        lines.append(f"- {direction} ×{len(info['times'])}（{times}）: {key}{suffix}")
    lines.append(footer)
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[: budget - 1] + "…"
    return text
