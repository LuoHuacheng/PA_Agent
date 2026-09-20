# ruff: noqa: RUF001, RUF002, RUF003 - Chinese product copy
"""意图归因 A/B：同一批 K 线（复用记录里已落盘的阶段二提示词）跑多个臂，比"下单意图率"。

为什么复用落盘 prompt 而不是重新组装：
  1. `stage2_messages` 就是生产当时真正发出去的字节，包含当时的 K 线表、特征块、
     执行约束与阶段一诊断 —— 重新组装无法保证与 09-19/09-20 那些记录逐字一致；
  2. 本轮要回答的是"同样的输入，换模型/换文案，模型会不会下单"，其余条件必须冻结；
  3. 阶段一不重跑（`stage1_diagnosis` 已冻结在 prompt 里），调用量减半。

四个臂：
  head       现路由 + 落盘原文（基线）
  scrub      现路由 + 去掉"预言式拒单"文案（E3 候选）
  oldroute   旧路由 a6api/DeepSeek-V4-Flash-0731 + 落盘原文（E2 臂②，验证模型侧）
  oldprompt  现路由 + 回滚 766ede1 改过的两个策略 txt（E2 臂③，验证文案侧）

用法：
    .venv/bin/python tools/ab_intent.py --records 6 --dry-run      # 只看 prompt 差异，不调 API
    .venv/bin/python tools/ab_intent.py --records 6 --workers 3
    .venv/bin/python tools/ab_intent.py --records 6 --arms head,scrub --control
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.json_validator import JsonValidator, ValidationError
from pa_agent.config.paths import RECORDS_PENDING_DIR
from pa_agent.config.settings import load_settings
from pa_agent.demo.record_loader import frame_from_record_klines
from pa_agent.util.threading import CancelToken

# 766ede1(市价单解禁, 2026-09-20 19:06) 改过的策略文件 —— oldprompt 臂回滚这两个
REVERT_COMMIT = "766ede1^"
REVERT_FILES = (
    "prompt_engineering/提示词大纲_人设与思维方式.txt",
    "prompt_engineering/上涨通道交易策略.txt",
)

# E3：把"模型替执行层预判拒单"的话术换成"照常给方案"
SCRUB_RULES: list[tuple[str, str]] = [
    (
        "校验器会直接拒绝，且即使侥幸通过也会被入口门控拦下不提交。",
        "请让两者保持一致（本项属一致性校验，不构成不下单的理由）。",
    ),
    (
        "只改下单方向、不改诊断方向 —— 下一轮仍会被拒绝，且被入口门控拦下不提交。",
        "只改下单方向、不改诊断方向属于不一致，下一轮请同步。",
    ),
    (
        "## 程序侧硬约束（执行层校验，违反即拒单、该轮信号作废）",
        "## 程序侧硬约束（执行层校验口径，请据此规划三价）",
    ),
    (
        "程序会保留原方案却拒绝下单，请一并校验",
        "请一并按此规划 TP1 距离",
    ),
    (
        "- **逆势**（与上方日线趋势相反）方案直接拒单；",
        "- **逆势**（与上方日线趋势相反）方案执行层不会执行；",
    ),
    (
        "**⚠️ 诊断方向 neutral 时不得提交方向单（限价单/突破单/市价单）：**",
        "**⚠️ 诊断方向 neutral 时若提交方向单，须先按 node 2.3 同步方向：**",
    ),
    # ↓ 2026-09-21 追加：预言式文案的主要载体其实是"上一轮决策"的连续性块
    #   （模型自己写的 trade_confidence_reasoning / decision_trace.label 被回灌进下一轮），
    #   形成自我强化：一旦写过"会被执行层拒单"，下一轮就会再写一次。
    ("会被执行层拒单", "属执行层门控范畴"),
    ("被执行层拒单", "属执行层门控范畴"),
    ("执行层会直接拒单", "属执行层门控范畴"),
    ("执行层直接拒单", "属执行层门控范畴"),
    ("被入口门控拦下不提交", "属门控范畴"),
    ("逆日线拒单条件", "逆日线口径"),
    ("会被拒单", "属门控范畴"),
    ("直接拒单", "属门控范畴"),
]
PREDICTIVE_RE = re.compile(r"(会被拒|直接拒绝|拦下不提交|拒单|作废)")

INTENT_TYPES = {"限价单", "突破单", "市价单"}


def read_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pick_records(since: str, until: str, n: int, *, control: bool,
                 control_since: str, control_until: str, seed: int) -> list[Path]:
    """在窗口内均匀取 n 条（跨品种、跨时段），可选再取一组阳性对照。"""
    def window(lo: str, hi: str) -> list[Path]:
        return sorted(p for p in RECORDS_PENDING_DIR.glob("*.json")
                      if lo <= p.name[:16] < hi)

    def spread(pool: list[Path], k: int) -> list[Path]:
        if not pool or k <= 0:
            return []
        if len(pool) <= k:
            return pool
        rnd = random.Random(seed)
        idx = sorted(rnd.sample(range(len(pool)), k))
        return [pool[i] for i in idx]

    picked = spread(window(since, until), n)
    if control:
        picked += spread(window(control_since, control_until), max(2, n // 3))
    return picked


def _git_file(rev: str, rel_path: str) -> str:
    out = subprocess.run(["git", "show", f"{rev}:{rel_path}"],
                         capture_output=True, text=True, check=True)
    return out.stdout


def reverse_diffs() -> list[tuple[str, str]]:
    """[(新文本块, 旧文本块)] —— 用于把落盘 prompt 回滚到 766ede1^ 的措辞。"""
    pairs: list[tuple[str, str]] = []
    for rel in REVERT_FILES:
        old = _git_file(REVERT_COMMIT, rel)
        try:
            new = Path(rel).read_text(encoding="utf-8")
        except OSError:
            continue
        sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines())
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            old_block = "\n".join(old.splitlines()[i1:i2])
            new_block = "\n".join(new.splitlines()[j1:j2])
            if new_block.strip():
                pairs.append((new_block, old_block))
    # 长块优先，避免短块先命中把长块切碎
    return sorted(pairs, key=lambda p: -len(p[0]))


def scrub_messages(messages: list[dict]) -> tuple[list[dict], int, int]:
    """E3 变体：替换预言式拒单话术，返回 (新消息, 命中规则数, 剩余命中关键词数)。"""
    out = copy.deepcopy(messages)
    hits = 0
    for msg in out:
        if not isinstance(msg.get("content"), str):
            continue
        text = msg["content"]
        for new_wording, replacement in SCRUB_RULES:
            if new_wording in text:
                hits += text.count(new_wording)
                text = text.replace(new_wording, replacement)
        msg["content"] = text
    left = sum(len(PREDICTIVE_RE.findall(m["content"]))
               for m in out if isinstance(m.get("content"), str))
    return out, hits, left


def oldprompt_messages(messages: list[dict], pairs: list[tuple[str, str]]) -> tuple[list[dict], int]:
    out = copy.deepcopy(messages)
    hits = 0
    for msg in out:
        if not isinstance(msg.get("content"), str):
            continue
        text = msg["content"]
        for new_block, old_block in pairs:
            if new_block in text:
                hits += 1
                text = text.replace(new_block, old_block)
        msg["content"] = text
    return out, hits


def providers(settings):
    """(臂名 → provider 设置)。旧路由 key 从 2026-09-10 的备份里取。"""
    head = settings.provider
    try:
        head_copy = head.model_copy(deep=True)
    except AttributeError:
        head_copy = copy.deepcopy(head)

    old = head_copy.model_copy(deep=True)
    bak = Path("config/settings.json.bak_20260910_whitelist10")
    if bak.exists():
        src = json.loads(bak.read_text(encoding="utf-8"))["provider"]
        for field, value in src.items():
            if hasattr(old, field) and field != "reasoning_effort":
                setattr(old, field, value)
    old.reasoning_effort = "medium"   # 只改路由，effort 与 head 对齐，避免混入第二个变量
    return {"head": head_copy, "scrub": head_copy, "oldroute": old, "oldprompt": head_copy}


def run_arm(arm: str, provider, messages: list[dict], frame, validator, settings) -> dict:
    client = create_ai_client(provider)
    t0 = time.time()
    try:
        reply = client.stream_chat(
            messages,
            cancel_token=CancelToken(),
            thinking=bool(getattr(provider, "thinking", True)),
            reasoning_effort=getattr(provider, "reasoning_effort", None),
        )
    except Exception as exc:
        return {"arm": arm, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "latency_s": round(time.time() - t0, 1)}
    content = getattr(reply, "content", "") or ""
    res = validator.validate("stage2", content, kline_frame=frame)
    if isinstance(res, ValidationError):
        return {"arm": arm, "ok": False, "error": f"validation: {res}"[:300],
                "latency_s": round(time.time() - t0, 1), "raw_head": content[:200]}
    obj = res.obj or {}
    decision = (obj.get("decision") or {}) if isinstance(obj, dict) else {}
    order_type = decision.get("order_type") or "?"
    return {
        "arm": arm,
        "ok": True,
        "order_type": order_type,
        "intent": order_type in INTENT_TYPES,
        "direction": decision.get("order_direction"),
        "entry_price": decision.get("entry_price"),
        "trade_confidence": decision.get("trade_confidence"),
        "latency_s": round(time.time() - t0, 1),
        "prompt_chars": sum(len(m.get("content") or "") for m in messages),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records", type=int, default=6, help="零意图窗口取几条（默认 6）")
    ap.add_argument("--since", default="2026-09-19_13", help="窗口起点（按文件名前缀比较）")
    ap.add_argument("--until", default="2026-09-21_00", help="窗口终点")
    ap.add_argument("--control", action="store_true", help="加一组阳性对照（09-19 00:00-12:59，当年意图率 24.6%）")
    ap.add_argument("--control-since", default="2026-09-19_00")
    ap.add_argument("--control-until", default="2026-09-19_13")
    ap.add_argument("--arms", default="head,scrub,oldroute,oldprompt")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="只组装变体，不调 API")
    ap.add_argument("--out", default="", help="结果 jsonl 路径（默认 logs/ab_intent_<ts>.jsonl）")
    args = ap.parse_args()

    settings = load_settings()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    provs = providers(settings)
    pairs = reverse_diffs() if "oldprompt" in arms else []
    validator = JsonValidator(settings)

    paths = pick_records(args.since, args.until, args.records, control=args.control,
                         control_since=args.control_since, control_until=args.control_until,
                         seed=args.seed)
    if not paths:
        print("没有选到记录")
        return 1

    print(f"记录 {len(paths)} 条 | 臂 {arms} | 对照 {'开' if args.control else '关'}")
    if pairs:
        print(f"oldprompt 回滚块 {len(pairs)} 处（来自 {REVERT_COMMIT}）")
    tasks = []
    for p in paths:
        raw = read_record(p)
        base_msgs = raw.get("stage2_messages") or []
        if not base_msgs:
            print(f"跳过（无 stage2_messages）: {p.name}")
            continue
        frame = frame_from_record_klines(raw["kline_data"], symbol=raw["meta"]["symbol"],
                                        timeframe=raw["meta"]["timeframe"])
        variants: dict[str, list[dict]] = {"head": base_msgs}
        variants["scrub"], scrub_hits, scrub_left = scrub_messages(base_msgs)
        variants["oldprompt"], op_hits = (oldprompt_messages(base_msgs, pairs)
                                          if pairs else (base_msgs, 0))
        meta = {
            "record": p.name,
            "symbol": raw["meta"]["symbol"],
            "stored_intent": (raw.get("stage2_decision") or {}).get("decision", {}).get("order_type"),
            "scrub_hits": scrub_hits, "scrub_keywords_left": scrub_left,
            "oldprompt_blocks": op_hits,
            "prompt_chars": sum(len(m.get("content") or "") for m in base_msgs),
        }
        if args.dry_run:
            print(f"  {p.name} prompt={meta['prompt_chars']} scrub命中={scrub_hits} "
                  f"残留关键词={scrub_left} oldprompt块={op_hits} 原判={meta['stored_intent']}")
            continue
        for arm in arms:
            tasks.append((arm, meta, variants.get(arm, base_msgs), frame))

    if args.dry_run:
        return 0

    results: list[dict] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(run_arm, arm, provs[arm], msgs, frame, validator, settings):
                (arm, meta) for arm, meta, msgs, frame in tasks}
        for fut in as_completed(futs):
            arm, meta = futs[fut]
            row = dict(meta)
            row.update(fut.result())
            with lock:
                results.append(row)
            flag = "下单" if row.get("intent") else ("失败" if not row.get("ok") else "观望")
            print(f"  [{arm:9s}] {meta['record'][:26]} {flag} "
                  f"{row.get('order_type', row.get('error', ''))[:40]}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out or f"logs/ab_intent_{ts}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in sorted(results, key=lambda r: (r["record"], r["arm"])):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("\n=== 汇总（意图率按臂） ===")
    by_arm: dict[str, list[dict]] = {}
    for row in results:
        by_arm.setdefault(row["arm"], []).append(row)
    for arm in arms:
        rows = by_arm.get(arm, [])
        ok = [r for r in rows if r.get("ok")]
        intents = [r for r in ok if r.get("intent")]
        lat = [r["latency_s"] for r in ok] or [0]
        print(f"  {arm:9s} 调用={len(rows):3d} 成功={len(ok):3d} 意向={len(intents):3d} "
              f"意向率={(100 * len(intents) / len(ok) if ok else 0):5.1f}%  中位耗时={sorted(lat)[len(lat)//2]:.0f}s")
    print(f"\n结果: {out}")
    stored = [r for r in results if r["arm"] == arms[0]]
    if stored:
        print("落盘原判（这些记录当时都是观望）:",
              {r["stored_intent"] for r in stored})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
