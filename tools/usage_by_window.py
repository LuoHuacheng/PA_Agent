"""按入口门控禁掉的时段分桶统计模型用量 (2026-09-22).

为什么: 窗口内 stage2 被 monitoring.light_mode_skip_blocked_session 跳过后, 每日
调用数应从约 2 次/轮 降到约 1 次/轮。本工具把日志里的 KV-cache / stream_chat 行按
窗口分桶, 给出 in_window / outside 两组, 用来验证省钱是否真的发生、哪天开始生效。

口径:
  - 日志时间戳是**本机本地时间**, 而窗口是 **UTC**(与 signal_pipeline._session_blocked_now
    同源, 从 binance_usdm_testnet.block_session_* 读), 因此按机器 UTC 偏移换算。
  - calls 只数 stream_chat done 行(= 一次模型调用); prompt_tokens = hit + miss。
  - 只读, 不写任何配置。

用法:
    .venv/bin/python tools/usage_by_window.py --days 2
    .venv/bin/python tools/usage_by_window.py --days 2 --tsv logs/usage_by_window.tsv
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
KV_RE = re.compile(r"KV-cache: hit=(\d+) miss=(\d+) total_prompt=(\d+)")
DONE_RE = re.compile(
    r"stream_chat done: latency=(\d+) ms reasoning_chars=(\d+) content_chars=(\d+)"
)
SYMBOL_RE = re.compile(r"_([A-Z0-9]+USDT)_")

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class UsageSample:
    """一条用量样本: ts 按日志原文(本机本地时间)解析。"""

    ts: datetime
    kind: str  # prompt | completion
    hit: int = 0
    miss: int = 0
    out_chars: int = 0


def parse_line(line: str) -> UsageSample | None:
    """从一行日志里抽出用量; 与该行无关时返回 None。"""
    head = TS_RE.match(line)
    if not head:
        return None
    try:
        ts = datetime.strptime(head.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    kv = KV_RE.search(line)
    if kv:
        return UsageSample(ts=ts, kind="prompt", hit=int(kv.group(1)), miss=int(kv.group(2)))
    done = DONE_RE.search(line)
    if done:
        return UsageSample(ts=ts, kind="completion", out_chars=int(done.group(3)))
    return None


def in_blocked_window(ts_utc: datetime, *, start_hour: int, end_hour: int) -> bool:
    """UTC 小时是否落在入口门控禁用的窗口内(左闭右开, 可跨零点)。"""
    hour = ts_utc.hour % 24
    start = int(start_hour) % 24
    end = int(end_hour) % 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _empty_bucket() -> dict[str, int]:
    return {"calls": 0, "prompt_tokens": 0, "hit": 0, "miss": 0, "out_chars": 0}


def summarize(
    samples: Iterable[UsageSample | None],
    *,
    start_hour: int,
    end_hour: int,
    tz_offset_hours: float = 0.0,
) -> dict[str, Any]:
    """按窗口分桶; tz_offset_hours 是日志本地时间相对 UTC 的偏移(如 UTC+8 传 8)。"""
    out: dict[str, Any] = {"in_window": _empty_bucket(), "outside": _empty_bucket(), "ignored": 0}
    shift = timedelta(hours=float(tz_offset_hours))
    for sample in samples:
        if sample is None:
            out["ignored"] += 1
            continue
        bucket = (
            out["in_window"]
            if in_blocked_window(sample.ts - shift, start_hour=start_hour, end_hour=end_hour)
            else out["outside"]
        )
        if sample.kind == "prompt":
            bucket["hit"] += sample.hit
            bucket["miss"] += sample.miss
            bucket["prompt_tokens"] += sample.hit + sample.miss
        else:
            bucket["calls"] += 1
            bucket["out_chars"] += sample.out_chars
    return out


def count_window_skips(paths: Sequence[Path]) -> dict[str, Any]:
    """统计 pending 记录里 light_skip_stage2 命中的条数(用于算 调用/轮 比值)。"""
    stats: dict[str, Any] = {"records": 0, "skipped": 0, "by_symbol": {}}
    for path in paths:
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stage2 = doc.get("stage2_decision")
        if not isinstance(stage2, dict):
            continue
        stats["records"] += 1
        if not stage2.get("light_skip_stage2"):
            continue
        stats["skipped"] += 1
        match = SYMBOL_RE.search(Path(path).name)
        if match:
            symbol = match.group(1)
            stats["by_symbol"][symbol] = stats["by_symbol"].get(symbol, 0) + 1
    return stats


def load_window(settings_path: Path) -> tuple[int, int]:
    """从 settings.json 读入口门控窗口(UTC 小时); 读不到时退回 20/4。"""
    try:
        doc = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        cfg = doc.get("binance_usdm_testnet") or {}
        return (
            int(cfg.get("block_session_utc_start_hour", 20)),
            int(cfg.get("block_session_utc_end_hour", 4)),
        )
    except (OSError, ValueError, TypeError):
        return 20, 4


def local_utc_offset_hours(now: datetime | None = None) -> float:
    """本机相对 UTC 的偏移小时数(如 UTC+8 得 8.0)。"""
    ref = now or datetime.now().astimezone()
    offset = ref.utcoffset()
    return 0.0 if offset is None else offset.total_seconds() / 3600.0


def iter_log_files(logs_dir: Path) -> list[Path]:
    """pa_agent.log 及其轮转文件, 旧的在前(便于按时间阅读)。"""
    directory = Path(logs_dir)
    base = directory / "pa_agent.log"

    def _rot_key(path: Path) -> int:
        suffix = path.name.rsplit(".", 1)[-1]
        return int(suffix) if suffix.isdigit() else 0

    rotated = sorted(directory.glob("pa_agent.log.*"), key=_rot_key, reverse=True)
    return rotated + ([base] if base.exists() else [])


def collect_samples(paths: Sequence[Path], *, days: float, now: datetime) -> list[UsageSample]:
    """读日志收集用量样本; 文件按 mtime 粗筛, 行按时间戳精筛。"""
    cutoff = now - timedelta(days=float(days))
    samples: list[UsageSample] = []
    for path in paths:
        try:
            if datetime.fromtimestamp(Path(path).stat().st_mtime) < cutoff:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    sample = parse_line(line)
                    if sample is not None and sample.ts >= cutoff:
                        samples.append(sample)
        except OSError:
            continue
    return samples


def coverage_start_ts(samples: Iterable[UsageSample | None], *, fallback_ts: float) -> float:
    """日志实际覆盖的起点(epoch 秒); 没有样本时退回 fallback。

    分母(窗内轮次)必须与分子(窗内调用)覆盖同一段时间: 日志只留约一天, 而
    records/pending 更久, 不按日志起点收敛分母, 调用/轮 会被没有日志的那段拉低。
    """
    stamps = [s.ts.timestamp() for s in samples if s is not None]
    return min(stamps) if stamps else fallback_ts


def window_record_paths(
    records_dir: Path, *, start_hour: int, end_hour: int, cutoff_ts: float
) -> list[Path]:
    """窗内(按 mtime 的 UTC 小时)且落在统计期内的 pending 记录。

    cutoff 用 epoch 秒比较: mtime 是 aware UTC、日志时间戳是 naive 本地, 混用会在
    边界上抛 naive/aware 比较错误(2026-09-22 实跑踩到, 单测当时没覆盖这条路径)。
    """
    out: list[Path] = []
    for path in sorted(Path(records_dir).glob("*.json")):
        try:
            mtime_ts = Path(path).stat().st_mtime
        except OSError:
            continue
        if mtime_ts < cutoff_ts:
            continue
        if in_blocked_window(
            datetime.fromtimestamp(mtime_ts, UTC), start_hour=start_hour, end_hour=end_hour
        ):
            out.append(path)
    return out


def _fmt_tokens(value: int) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按入口门控窗口分桶统计模型用量(只读)")
    parser.add_argument("--days", type=float, default=1.0, help="统计最近多少天(默认 1)")
    parser.add_argument("--logs-dir", default=str(ROOT / "logs"))
    parser.add_argument("--records-dir", default=str(ROOT / "records" / "pending"))
    parser.add_argument("--settings", default=str(ROOT / "config" / "settings.json"))
    parser.add_argument("--tsv", default="", help="追加一行到该 TSV(日检用)")
    parser.add_argument("--json", action="store_true", help="输出 JSON(机器可读)")
    parser.add_argument("--oneline", action="store_true", help="只输出一行摘要(日检用)")
    parser.add_argument("--tz-offset", type=float, default=None, help="覆盖日志本地时区偏移小时")
    args = parser.parse_args(argv)

    now = datetime.now()
    start_hour, end_hour = load_window(Path(args.settings))
    offset = local_utc_offset_hours() if args.tz_offset is None else float(args.tz_offset)
    samples = collect_samples(iter_log_files(Path(args.logs_dir)), days=args.days, now=now)
    summary = summarize(samples, start_hour=start_hour, end_hour=end_hour, tz_offset_hours=offset)
    cutoff = now - timedelta(days=float(args.days))
    # 分母起点与日志覆盖对齐, 否则轮转掉的那段只进分母(见 coverage_start_ts)。
    covered_from = coverage_start_ts(samples, fallback_ts=cutoff.timestamp())
    skips = count_window_skips(
        window_record_paths(
            Path(args.records_dir),
            start_hour=start_hour,
            end_hour=end_hour,
            cutoff_ts=covered_from,
        )
    )
    rounds = int(skips["records"])
    window_calls = int(summary["in_window"]["calls"])
    calls_per_round = (window_calls / rounds) if rounds else 0.0
    payload = {
        "window_utc": f"{start_hour:02d}:00-{(end_hour - 1) % 24:02d}:59",
        "days": float(args.days),
        "tz_offset_hours": offset,
        "in_window": summary["in_window"],
        "outside": summary["outside"],
        "window_rounds": rounds,
        "window_stage2_skipped": int(skips["skipped"]),
        "calls_per_round": round(calls_per_round, 3),
    }
    if args.oneline:
        print(
            f"窗口内调用={window_calls} 调用/轮={calls_per_round:.2f}"
            f" 跳过stage2={skips['skipped']}"
            f" 窗内prompt={_fmt_tokens(summary['in_window']['prompt_tokens'])}"
        )
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"窗口(UTC) {payload['window_utc']} | 最近 {args.days:g} 天 | 日志本地偏移 {offset:+g}h"
        )
        print(
            f"窗口内: 调用={window_calls} prompt={_fmt_tokens(summary['in_window']['prompt_tokens'])}"
            f" out={_fmt_tokens(summary['in_window']['out_chars'])}"
        )
        print(
            f"窗口外: 调用={summary['outside']['calls']}"
            f" prompt={_fmt_tokens(summary['outside']['prompt_tokens'])}"
            f" out={_fmt_tokens(summary['outside']['out_chars'])}"
        )
        print(
            f"窗内记录={rounds} 跳过stage2={skips['skipped']}"
            f" 调用/轮={calls_per_round:.2f} (跳过生效时应 ~1.0, 未生效 ~2.0)"
        )
    if args.tsv:
        tsv = Path(args.tsv)
        tsv.parent.mkdir(parents=True, exist_ok=True)
        header_needed = not tsv.exists()
        with tsv.open("a", encoding="utf-8") as handle:
            if header_needed:
                handle.write(
                    "ts\t窗口内调用\t窗口内prompt\t窗口外调用\t窗内记录\t跳过stage2\t调用每轮\n"
                )
            handle.write(
                f"{now.strftime('%Y-%m-%d %H:%M:%S')}\t{window_calls}"
                f"\t{summary['in_window']['prompt_tokens']}\t{summary['outside']['calls']}"
                f"\t{rounds}\t{skips['skipped']}\t{calls_per_round:.2f}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
