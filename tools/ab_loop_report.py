# ruff: noqa: RUF002 - Chinese product copy
"""汇总 ab_loop.py 的前滚结果：逐文件、逐臂、逐轮对照（下单 / 复发 / 注入刺激）。

用法：
    .venv/bin/python tools/ab_loop_report.py logs/ab_loop_stim.jsonl logs/ab_loop_clean.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: Path) -> list[dict]:
    if not path.exists():
        print(f"缺文件: {path}")
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def report(name: str, rows: list[dict]) -> None:
    if not rows:
        return
    arms = sorted({r["arm"] for r in rows})
    print(f"\n=== {name} ===")
    for arm in arms:
        rs = sorted([r for r in rows if r["arm"] == arm], key=lambda r: r["round"])
        ok = [r for r in rs if r.get("ok")]
        intents = sum(1 for r in ok if r.get("intent"))
        cells = "  ".join(
            f"r{r['round']}:" + ("✔" if r.get("intent") else ("ERR" if not r.get("ok") else "·"))
            + f"(注入{r.get('injected_pred', 0)}/产出{r.get('produced_pred', 0)})"
            for r in rs
        )
        rec = sum(1 for r in ok if r.get("produced_pred"))
        inj = [r.get("injected_pred", 0) for r in rs]
        print(f"  {arm:10s} 下单 {intents}/{len(ok)}  复发轮 {rec}/{len(ok)}  "
              f"平均注入 {sum(inj) / max(1, len(inj)):.1f}")
        print(f"             {cells}")


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] or [Path("logs/ab_loop.jsonl")]
    for p in paths:
        report(p.name, load(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
