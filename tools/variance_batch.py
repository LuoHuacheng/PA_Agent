#!/usr/bin/env python3
"""批量一致性实验: 单次方向 vs 多数投票方向, 谁的命中率高.

背景
----
variance_baseline 证明了同一份 K 线跑 5 次, direction 只有约 70% 一致。
但"一致性低"不等于"投票有用" —— 方差和偏差要分开看:

    方差(variance)  同一输入给出不同答案    -> 多数投票能压掉
    偏差(bias)      答案系统性偏向一边      -> 投票压不掉

本脚本对一批历史记录各跑 N 次, 用记录之后的真实 K 线作裁判, 比较:

    A 单次命中率   取第 1 次的 direction
    B 投票命中率   取 N 次的众数 direction

若 B 明显高于 A, 说明方差是主要问题, 生产上跑多次采样值得那份成本;
若两者接近, 说明是偏差, 该换模型或换信号, 而不是加采样。

用法
----
    python tools/variance_batch.py --count 15 --runs 5 --horizon 8 --workers 2
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import variance_baseline as vb  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
KLINE_EPOCH_OFFSET_MS = 8 * 3600 * 1000
BAR_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}
OUT_DIR = ROOT / "logs" / "variance_batch"


def _is_directional(name):
    """该记录当时的阶段一方向是否明确 (neutral 占比过半, 先筛掉免得白花钱)."""
    try:
        doc = json.loads((vb.RECORDS_PENDING_DIR / name).read_text(encoding="utf-8"))
    except Exception:
        return False
    diag = doc.get("stage1_diagnosis") or {}
    return str(diag.get("direction") or "").lower() in ("bullish", "bearish")


def _name_ms(name):
    """从文件名解析决策时刻 (本地墙上时间 -> epoch ms)。"""
    try:
        dt = datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S").replace(tzinfo=TZ8)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _has_future(timeline, record_ms, horizon, bar_ms):
    """决策之后是否有连续 horizon 根 K 线可判定 (挑样本时先剔除, 免得白花调用)。"""
    if record_ms is None or not timeline:
        return False
    future = [b for b in timeline if b[0] > record_ms]
    if len(future) < horizon:
        return False
    for i in range(horizon - 1):
        if future[i + 1][0] - future[i][0] != bar_ms:
            return False
    return True


def pick_records(count, timeframe, per_symbol, days=45, directional_only=False,
                 timelines=None, horizon=8, bar_ms=1_800_000):
    """按 symbol 与时间分散地挑记录.

    默认**不**过滤方向: 只挑"当时方向明确"的记录, 等于把高方差的模糊场景先
    筛掉, 会让"投票是否有用"天然偏向"没用"。改为只剔除没有后续 K 线可判定的
    记录 —— 那条是纯粹的样本浪费, 与偏差无关。
    """
    cutoff = (datetime.now(TZ8) - timedelta(days=days)).strftime("%Y-%m-%d")
    files = sorted(glob.glob(str(vb.RECORDS_PENDING_DIR / ("*_%s.json" % timeframe))))
    by_symbol = defaultdict(list)
    for path in files:
        name = os.path.basename(path)
        if name[:10] < cutoff:
            continue
        parts = name.split("_")
        if len(parts) < 4:
            continue
        by_symbol[parts[-2]].append(name)
    picked = []
    for symbol in sorted(by_symbol):
        names = sorted(by_symbol[symbol], reverse=True)
        timeline = (timelines or {}).get((symbol, timeframe), [])
        step = max(1, len(names) // max(1, per_symbol * 4))
        taken = 0
        for name in names[::step]:
            if taken >= per_symbol:
                break
            if directional_only and not _is_directional(name):
                continue
            if timelines is not None and not _has_future(
                timeline, _name_ms(name), horizon, bar_ms
            ):
                continue
            picked.append(name)
            taken += 1
    picked.sort(key=lambda n: (n[:10], n.split("_")[2]))
    return picked[:count]


def load_timeline(symbol, timeframe):
    """合并同一 symbol/timeframe 所有 pending 记录里的 K 线, 去重升序。"""
    store = {}
    pattern = str(vb.RECORDS_PENDING_DIR / ("*_%s_%s.json" % (symbol, timeframe)))
    for path in glob.glob(pattern):
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        for bar in doc.get("kline_data") or []:
            ts = bar.get("ts_open")
            if not ts:
                continue
            try:
                store[int(ts) - KLINE_EPOCH_OFFSET_MS] = (
                    int(ts) - KLINE_EPOCH_OFFSET_MS,
                    float(bar["open"]), float(bar["high"]),
                    float(bar["low"]), float(bar["close"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(store.values())


def future_ret(timeline, record_ms, horizon, bar_ms):
    """决策之后连续 horizon 根 K 线的收盘变化率; 不连续或不足返回 None。"""
    past = [b for b in timeline if b[0] <= record_ms]
    if not past:
        return None
    anchor = past[-1][4]
    if anchor <= 0:
        return None
    future = [b for b in timeline if b[0] > record_ms]
    if len(future) < horizon:
        return None
    for i in range(horizon - 1):
        if future[i + 1][0] - future[i][0] != bar_ms:
            return None
    return (future[horizon - 1][4] - anchor) / anchor


def run_one(name, runs, client, settings, validator, assembler):
    """对一条记录跑 runs 次, 返回 draws 列表(失败返回错误串)。"""
    try:
        raw = json.loads((vb.RECORDS_PENDING_DIR / name).read_text(encoding="utf-8"))
    except Exception as exc:
        return None, "load %s" % exc
    frame = vb.frame_from_record_klines(
        raw["kline_data"],
        symbol=raw["meta"]["symbol"],
        timeframe=raw["meta"]["timeframe"],
    )
    draws = []
    for _ in range(runs):
        diag, err = vb._one_draw(assembler, client, validator, frame, settings)
        if diag is None:
            return None, err
        draws.append({
            "direction": diag.get("direction"),
            "cycle_position": diag.get("cycle_position"),
        })
    return {
        "record": name,
        "symbol": raw["meta"]["symbol"],
        "timeframe": raw["meta"]["timeframe"],
        "record_ms": int(raw["meta"]["timestamp_local_ms"]),
        "draws": draws,
    }, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=15)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=8, help="判定用的后续 K 线根数")
    ap.add_argument("--timeframe", default="30m")
    ap.add_argument("--per-symbol", type=int, default=3)
    ap.add_argument("--days", type=int, default=45, help="只看近 N 天的记录")
    ap.add_argument("--directional-only", action="store_true",
                    help="只挑当时方向明确的记录(会引入选择偏差, 一般不用)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--variant", default="trimmed")
    args = ap.parse_args()

    vb._select_variant(args.variant)
    settings = vb.load_settings()
    validator = vb.JsonValidator(settings)

    bar_ms = BAR_MS.get(args.timeframe, 1_800_000)
    symbols = set()
    for path in glob.glob(str(vb.RECORDS_PENDING_DIR / ("*_%s.json" % args.timeframe))):
        parts = os.path.basename(path).split("_")
        if len(parts) >= 4:
            symbols.add(parts[-2])
    print("加载 %d 个品种的 K 线时间线..." % len(symbols), flush=True)
    timelines = {s: load_timeline(s, args.timeframe) for s in sorted(symbols)}
    names = pick_records(
        args.count, args.timeframe, args.per_symbol,
        days=args.days, directional_only=args.directional_only,
        timelines={(s, args.timeframe): tl for s, tl in timelines.items()},
        horizon=args.horizon, bar_ms=bar_ms,
    )
    print("选中记录 %d 条:" % len(names))
    for n in names:
        print("   ", n)
    print("每条跑 %d 次, 预计 %d 次调用" % (args.runs, len(names) * args.runs), flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results, failures = [], []

    def worker(name):
        # 每条记录一个独立的 client/assembler, 避免线程共享状态
        client = vb.create_ai_client(settings.provider)
        assembler = vb.PromptAssembler(
            vb.PROMPT_DIR, vb.ExperienceReader(), prompt_settings=settings.prompt
        )
        data, err = run_one(name, args.runs, client, settings, validator, assembler)
        if data is None:
            return name, None, err
        return name, data, ""

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for name, data, err in pool.map(worker, names):
            if data is None:
                print("  FAIL %s: %s" % (name, err), flush=True)
                failures.append((name, err))
                continue
            (OUT_DIR / (name + ".json")).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            dirs = [d["direction"] for d in data["draws"]]
            mode = Counter(dirs).most_common(1)[0][0]
            print("  ok   %-42s dirs=%s mode=%s" % (name, dirs, mode), flush=True)
            results.append(data)

    print()
    if not results:
        print("没有成功的记录")
        return 1

    # ---- 用后续真实 K 线作裁判 (timelines / bar_ms 在选样阶段已建好) ----
    scored = []
    for data in results:
        key = (data["symbol"], data["timeframe"])
        if key not in timelines:
            timelines[key] = load_timeline(*key)
        ret = future_ret(timelines[key], data["record_ms"], args.horizon, bar_ms)
        data["future_ret"] = ret
        scored.append(data)

    usable = [d for d in scored if d["future_ret"] is not None]
    print("有后续 K 线可判定的记录: %d/%d" % (len(usable), len(scored)))

    single = {"hit": 0, "n": 0}
    vote = {"hit": 0, "n": 0}
    agree_n = 0
    for data in usable:
        actual_up = data["future_ret"] > 0
        dirs = [d["direction"] for d in data["draws"]]
        if len(set(dirs)) == 1:
            agree_n += 1
        first = dirs[0]
        mode = Counter(dirs).most_common(1)[0][0]
        for value, bucket in ((first, single), (mode, vote)):
            if value not in ("bullish", "bearish"):
                continue
            bucket["n"] += 1
            if (value == "bullish") == actual_up:
                bucket["hit"] += 1

    print()
    print("=== 单次 vs 多数投票 (horizon=%d 根, n_runs=%d) ===" % (args.horizon, args.runs))
    print("direction 完全一致: %d/%d" % (agree_n, len(usable)))
    for label, bucket in (("单次(第1次)", single), ("多数投票", vote)):
        if bucket["n"]:
            print("%-12s 命中 %2d/%2d = %.1f%%" % (
                label, bucket["hit"], bucket["n"], bucket["hit"] / bucket["n"] * 100))
        else:
            print("%-12s 无有效方向" % label)
    if single["n"] and vote["n"]:
        delta = (vote["hit"] / vote["n"] - single["hit"] / single["n"]) * 100
        print("投票 - 单次: %+.1f 个百分点" % delta)
    print()
    print("明细:")
    for data in usable:
        dirs = [d["direction"] for d in data["draws"]]
        print("  %-42s ret=%+.3f%%  dirs=%s" % (
            data["record"], data["future_ret"] * 100, dirs))
    if failures:
        print()
        print("失败 %d 条: %s" % (len(failures), failures[:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
