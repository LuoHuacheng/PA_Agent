# VergeX 策略集成实施计划(代码策略反推 + 提示词策略移植)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> ⚠️ **复选框状态（2026-09-20 复核）：正文 `- [ ]` 全部未回填，但 Task 1–7 产物已落地** ——
> `tools/vergex_{loader,align,gens,report}.py` + `tests/unit/test_vergex_*.py`（4 个）、
> `prompt_engineering/文件29-利弗莫尔趋势跟随移植.txt`、`文件30-三层评分趋势移植.txt`、
> `docs/vergex移植策略shadow验证清单.md`。**以文件与提交为准**。
> 唯一真未完成项：Task 7 Step 3 的 48h shadow **效果判定** —— 已在 shadow 清单 §6 记为
> "接线生效、效果不可归因"（无对照臂 + 窗口跨多次配置变更）。

**Goal:** 把 VergeX 平台上已获取的策略(代码策略的成交序列 + 提示词策略的公开全文)转译为 PA_Agent 的 baseline_lab 对照流生成器与策略提示词文件,在滚动样本外验证后,以"还原度矩阵"对照平台公布的收益率与胜率。

**Architecture:** 三条数据流汇入现有 `tools/audit/baseline_lab.py` 实验台:(1) VergeX 代码策略的公开逐笔成交 → 对齐分析 → 反推规则参数 → 新生成器;(2) VergeX 提示词策略全文 → 机械规则子集直接生成器化,LLM 子集翻译成 `prompt_engineering/` 策略文件;(3) 全部经 baseline_lab 滚动样本外(线1a)+ 切片(线1b)验证,由还原度报告工具对照平台数字。VergeX 侧抓取只能走浏览器同源请求(Cloudflare 挡脚本),仓库内代码全部离线消费落盘 JSON。

**Tech Stack:** Python 3.12 / uv / pytest / ruff;复用 `tools/shadow_replay.py`(Plan, simulate_plan, fee_rate_from_outcomes)与 `tools/sim_trailing_stop.py`(fetch_klines)。无新增第三方依赖。

**Spec:**
- `research/VergeX代码策略Top20_2026-09-13.md`(平台元数据与 ID 索引)
- `research/VergeX提示词策略Top20全文_2026-09-13.md`(30 个公开提示词全文与执行配置)
- 本计划"还原目标与不可还原边界"一节

## 还原目标与不可还原边界(全局判据)

平台参考数字(抓取时点, 见 spec 文档):

| 策略 | 类型 | 胜率 | 笔数 | 收益% | 回撤% | 反推难度 |
|------|------|------|------|-------|-------|----------|
| ETH中线策略-稳中求进 | 代码 | 50.45% | 111 | 281.87 | 46.89 | 低(30m 收盘触发已确认) |
| BTC_ZEC_MultiTF_Rotation | 代码 | 45.79% | 653 | 399.32 | 58.91 | 中(653 笔样本足) |
| ZEC_MOM1_5x_50pct | 代码 | 28.37% | 282 | 125.40 | 38.63 | 中 |
| ZEC_MACD | 代码 | 32.65% | 49 | 116.68 | 42.22 | 中 |
| ZEC高频趋势追踪大王 | 代码 | 41.76% | 91 | 689.16 | 87.07 | 中高(高频) |
| trend strategy(提示词) | 提示词 | 不公开 | 31 | 96.40 | 27.60 | 低(全文已公开) |
| AI500 高频动能(公开版) | 提示词 | 不公开 | 0 | 0 | 0 | 低(规则完整) |

**还原判据(Task 6 自动判定):** 胜率差 ≤ 8pp、笔数差 ≤ 25%、盈亏方向一致 → 达标;收益率只要求同数量级。

**不可还原边界(写进报告免责):** ① 平台收益含复利/杠杆/资金费,`simulate_plan` 是固定风险 R 口径,只可比胜率与均 R 形态;② 美股代币策略(MRVL/AMD/Silver/GOOGL/PLTR/TELA)无 Binance 数据,排除;③ TickStrategy 的 tick 级细节在 K 线粒度只能近似;④ 平台头部数字有幸存者偏差,还原目标是"同规则同数据段统计形态一致",不是复刻 +17302%。

## Global Constraints

- 运行/测试一律 `uv run pytest ...`、`uv run python ...`;提交信息 `<emoji> <type>(scope): 中文描述`。
- 不新增第三方依赖;只用标准库 + 仓库现有模块。
- VergeX 凭据只存在于浏览器 localStorage;仓库代码、数据文件、文档一律不得包含 token 明文。
- VergeX API 抓取仅浏览器同源可用(Cloudflare);仓库内 Python 工具只读落盘 JSON,不发网络请求。
- 标的限定 Binance USDT 永续:`BTCUSDT ETHUSDT SOLUSDT XRPUSDT ZECUSDT`(`tools/_pa_sim_common.py:21` 的 DEFAULT_SYMBOLS)。
- K 线数据统一 5 元组 `(ts_ms, open, high, low, close)` 旧→新排序,与 `merge_timelines` 产出一致。
- 无量能数据(fetch_klines 产物只含 OHLC),涉及量比的规则一律排除或用价格代理。

---

### Task 1: VergeX 成交数据抓取落盘(数据任务)

**Files:**
- Create: `research/vergex/data/eth_mid/detail.json`
- Create: `research/vergex/data/btc_zec_rotation/detail.json`
- Create: `research/vergex/data/zec_mom1_5x/detail.json`
- Create: `research/vergex/data/zec_high_freq/detail.json`
- Create: `research/vergex/SCHEMA.md`

**Interfaces:**
- Consumes: VergeX API `GET /api/v1/marketplace/code-strategies/{id}`(Bearer,浏览器内 fetch)。
- Produces: 后续所有任务消费的 `detail.json`,关键字段路径 `backtest_snapshot.paired_trades[]`,已实测字段:`action, close_fee, close_reason, close_timestamp, entry_price, exit_price, holding_seconds, open_fee, open_reason, open_timestamp, pnl_gross, pnl_net, quantity, side, slippage, source, symbol, total_fee`。

**ID 对照**(来自 `research/VergeX代码策略Top20_2026-09-13.md`):

| slug | id | 策略 |
|------|----|------|
| eth_mid | 0cf1415d-5ca2-4bce-920f-18db10c90f2d | ETH中线策略-稳中求进 |
| btc_zec_rotation | 227e057a-c21b-4b08-a674-cf43d0553630 | BTC_ZEC_MultiTF_Rotation |
| zec_mom1_5x | e649c06d-fbc0-4c2b-b32c-c946a3f8814e | ZEC_MOM1_5x_50pct |
| zec_high_freq | 23a82440-bc0b-43c2-b3df-eb4300251984 | ZEC高频趋势追踪大王 |

- [ ] **Step 1: 浏览器逐个抓取**(用 browser-use 在已登录 IAB 内执行,每策略一次 fetch)

```js
// 在 vergex.trade 页面上下文执行;slug 换对应目录名
const id = "0cf1415d-5ca2-4bce-920f-18db10c90f2d";
const tk = localStorage.getItem("auth_token");
const j = await (await fetch(`/api/v1/marketplace/code-strategies/${id}`,
  {headers: {Authorization: `Bearer ${tk}`}})).json();
return JSON.stringify(j); // 由执行器 Write 到 research/vergex/data/eth_mid/detail.json
```

- [ ] **Step 2: 校验数据完整性**

Run: `uv run python -c "import json;d=json.load(open('research/vergex/data/eth_mid/detail.json'));print(len(d['backtest_snapshot']['paired_trades']), d['total_trades'])"`
Expected: 两个数字相等(ETH 中线 = 111)。

- [ ] **Step 3: 记录 SCHEMA.md**

内容必须包含:端点、鉴权方式(只写"Bearer token,浏览器 localStorage,勿入库")、`paired_trades` 全字段表、`side` 与 `close_reason` 的实测取值枚举(从数据里 `set()` 归纳)、`equity_curve` 字段说明。
- [ ] **Step 4: Commit**

```bash
git add research/vergex
git commit -m "📦 data(vergex): 四个目标代码策略成交序列落盘 + SCHEMA"
```

---

### Task 2: VergexDump 加载器

**Files:**
- Create: `tools/vergex_loader.py`
- Test: `tests/unit/test_vergex_loader.py`
- Create: `tests/fixtures/vergex/eth_mid_trades.json`(从真实 dump 截取 5 条 paired_trades 的最小 detail 结构)

**Interfaces:**
- Produces: `PairTrade`(dataclass:`open_ms, close_ms, side(1/-1), symbol, entry, exit, qty, pnl_net, pnl_gross, fee, holding_s, close_reason, open_reason`)、`VergexDump(slug, name, total_trades, trades: tuple[PairTrade, ...])`、`load_dump(path: Path, *, slug: str | None = None) -> VergexDump`(trades 按 open_ms 升序)。

- [ ] **Step 1: 写失败测试**

```python
import json
from pathlib import Path
from tools.vergex_loader import load_dump

FIX = Path("tests/fixtures/vergex/eth_mid_trades.json")

def _raw():
    return json.loads(FIX.read_text(encoding="utf-8"))

def test_load_parses_all_trades_sorted():
    dump = load_dump(FIX, slug="eth_mid")
    assert dump.slug == "eth_mid"
    assert dump.total_trades == len(dump.trades) == 5
    ms = [t.open_ms for t in dump.trades]
    assert ms == sorted(ms)

def test_side_mapping_and_numbers():
    dump = load_dump(FIX)
    t = dump.trades[0]
    raw0 = _raw()["backtest_snapshot"]["paired_trades"][0]
    assert t.side == (1 if str(raw0["side"]).lower() in ("long", "buy", "1") else -1)
    assert t.entry == float(raw0["entry_price"])
    assert t.pnl_net == float(raw0["pnl_net"])
    assert t.holding_s == int(raw0["holding_seconds"])

def test_win_rate_computed_not_read():
    dump = load_dump(FIX)
    wins = sum(1 for t in dump.trades if t.pnl_net > 0)
    assert abs(dump.win_rate - wins / len(dump.trades)) < 1e-9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_vergex_loader.py -v`
Expected: FAIL `ModuleNotFoundError: tools.vergex_loader`

- [ ] **Step 3: 最小实现**

```python
"""VergeX 策略 detail.json(浏览器抓取产物)加载与归一化."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PairTrade:
    open_ms: int
    close_ms: int
    side: int  # 1 多 / -1 空
    symbol: str
    entry: float
    exit: float
    qty: float
    pnl_net: float
    pnl_gross: float
    fee: float
    holding_s: int
    close_reason: str
    open_reason: str


@dataclass(frozen=True)
class VergexDump:
    slug: str
    name: str
    total_trades: int
    win_rate: float
    trades: tuple[PairTrade, ...]


def _side(raw) -> int:
    return 1 if str(raw).strip().lower() in ("long", "buy", "1") else -1


def load_dump(detail_path: Path, *, slug: str | None = None) -> VergexDump:
    doc = json.loads(Path(detail_path).read_text(encoding="utf-8"))
    rows = (doc.get("backtest_snapshot") or {}).get("paired_trades") or []
    trades = tuple(
        PairTrade(
            open_ms=int(r["open_timestamp"]), close_ms=int(r["close_timestamp"]),
            side=_side(r.get("side")), symbol=str(r.get("symbol") or ""),
            entry=float(r["entry_price"]), exit=float(r["exit_price"]),
            qty=float(r.get("quantity") or 0.0),
            pnl_net=float(r.get("pnl_net") or 0.0),
            pnl_gross=float(r.get("pnl_gross") or 0.0),
            fee=float(r.get("total_fee") or 0.0),
            holding_s=int(r.get("holding_seconds") or 0),
            close_reason=str(r.get("close_reason") or ""),
            open_reason=str(r.get("open_reason") or ""),
        )
        for r in rows
    )
    trades = tuple(sorted(trades, key=lambda t: t.open_ms))
    wins = sum(1 for t in trades if t.pnl_net > 0)
    wr = wins / len(trades) if trades else 0.0
    return VergexDump(
        slug=slug or Path(detail_path).parent.name,
        name=str(doc.get("name") or ""),
        total_trades=int(doc.get("total_trades") or len(trades)),
        win_rate=wr, trades=trades,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_vergex_loader.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add tools/vergex_loader.py tests/unit/test_vergex_loader.py tests/fixtures/vergex
git commit -m "✨ feat(vergex): detail.json 加载器与成对成交归一化"
```

---

### Task 3: 成交-K 线对齐分析(反推参数)

**Files:**
- Create: `tools/vergex_align.py`
- Test: `tests/unit/test_vergex_align.py`
- Create: `research/vergex/align_<slug>.md`(运行产物,4 份)

**Interfaces:**
- Consumes: `tools.vergex_loader.load_dump`、`tools.sim_trailing_stop.fetch_klines(symbols, start_ms, end_ms, interval, cache_dir)`(cache_dir/klines.json,格式 `{SYMBOL: [binance原始行]}`,行前 5 列即 ts/o/h/l/c)。
- Produces: `align_report(dump, klines, *, bar_ms=900_000) -> dict`,键:`bar_close_entry_rate`(入场落在 K 线开界 ±60s 内的比例,>0.8 即"上根收盘决策")、`entry_offset_hist`、`stop_dist_atr`(close_reason 含 sl 的 |entry-exit|/ATR14 中位数)、`hold_hours_median`、`monthly_pnl`、`close_reason_counts`。CLI:`uv run python tools/vergex_align.py --slug eth_mid --interval 15m`。

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path
from tools.vergex_loader import PairTrade, VergexDump
from tools.vergex_align import align_report

def _klines():
    # 3 根 15m K 线, 从 0 开始
    base = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    return [(base + i * 900_000, 100.0, 101.0, 99.0, 100.5) for i in range(3)]

def _dump(open_ms, close_ms, exit_px=99.0):
    tr = PairTrade(open_ms=open_ms, close_ms=close_ms, side=1, symbol="ETHUSDT",
                   entry=100.0, exit=exit_px, qty=1.0, pnl_net=-1.0, pnl_gross=-1.0,
                   fee=0.0, holding_s=900, close_reason="stop_loss", open_reason="")
    return VergexDump(slug="t", name="t", total_trades=1, win_rate=0.0, trades=(tr,))

def test_entry_right_after_bar_boundary_counts_as_close_decision():
    base = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    rep = align_report(_dump(base + 900_000 + 5_000, base + 900_000 + 900_000), _klines())
    assert rep["bar_close_entry_rate"] == 1.0

def test_stop_distance_uses_sl_rows():
    base = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    rep = align_report(_dump(base + 900_000 + 5_000, base + 2 * 900_000), _klines())
    assert rep["close_reason_counts"].get("stop_loss") == 1
    assert rep["hold_hours_median"] == 0.25
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_vergex_align.py -v`
Expected: FAIL(模块不存在)

- [ ] **Step 3: 实现**(核心逻辑)

```python
"""VergeX 成交序列与 Binance K 线对齐, 反推策略粒度/止损/持仓参数."""
from __future__ import annotations

import argparse
import statistics
from collections import Counter
from pathlib import Path

from tools.vergex_loader import VergexDump, load_dump


def _atr14(klines) -> float:
    trs = []
    for i in range(1, len(klines)):
        _, o, h, low, c = klines[i]
        prev_c = klines[i - 1][4]
        trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    if len(trs) < 14:
        return 0.0
    return sum(trs[-14:]) / 14


def align_report(dump: VergexDump, klines, *, bar_ms=900_000) -> dict:
    entries = [t.open_ms for t in dump.trades]
    close_entry = sum(1 for ms in entries if ms % bar_ms < 60_000)
    sl_rows = [t for t in dump.trades if "sl" in t.close_reason.lower()
               or "stop" in t.close_reason.lower()]
    atr = _atr14(klines)
    stop_atr = sorted(abs(t.entry - t.exit) / atr for t in sl_rows if atr > 0)
    holds = sorted(t.holding_s / 3600 for t in dump.trades)
    return {
        "bar_close_entry_rate": close_entry / len(entries) if entries else 0.0,
        "stop_dist_atr": statistics.median(stop_atr) if stop_atr else None,
        "hold_hours_median": statistics.median(holds) if holds else None,
        "close_reason_counts": dict(Counter(t.close_reason for t in dump.trades)),
        "win_rate": dump.win_rate,
        "n": len(dump.trades),
    }
```

CLI main():`--slug --interval --cache-dir` → `load_dump` + 读 `cache_dir/klines.json` → 打印 report 并写 `research/vergex/align_<slug>.md`(表格 + 判读:"bar_close_entry_rate>0.8 则按上根收盘决策生成器建模")。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_vergex_align.py -v`
Expected: 2 PASS

- [ ] **Step 5: 拉真实 K 线并产出 4 份对齐报告**

```bash
uv run python - <<'EOF'
from pathlib import Path
from tools.sim_trailing_stop import fetch_klines
cache = Path("research/vergex/data/_klines"); cache.mkdir(parents=True, exist_ok=True)
fetch_klines(["BTCUSDT", "ETHUSDT", "ZECUSDT"], start_ms=1746000000000,
             end_ms=1789300000000, interval="15m", cache_dir=cache)
EOF
uv run python tools/vergex_align.py --slug eth_mid --interval 15m  # 其余 slug 同理
```

- [ ] **Step 6: Commit**

```bash
git add tools/vergex_align.py tests/unit/test_vergex_align.py research/vergex
git commit -m "✨ feat(vergex): 成交-K线对齐分析, 反推粒度/止损/持仓参数"
```

---

### Task 4: 三个反推生成器

**Files:**
- Create: `tools/vergex_gens.py`
- Test: `tests/unit/test_vergex_gens.py`

**Interfaces:**
- Consumes: `tools.shadow_replay.Plan`、`_cutoff_from_days`。
- Produces: 与 `gen_donchian` 同签名的三个生成器,供 baseline_lab RULES 注册:

```python
def gen_momo_breakout(timeline, symbol, timeframe, *, days=None, target_rr=2.0,
                      stop_atr_mult=1.0, per_day=2, momo_n=14, momo_atr=1.5) -> list[Plan]
def gen_dual_supertrend(timeline, symbol, timeframe, *, days=None, target_rr=2.0,
                        stop_atr_mult=1.0, per_day=2, st_period=10, st_mult=3.0) -> list[Plan]
def gen_trend_pullback(timeline, symbol, timeframe, *, days=None, target_rr=2.0,
                       stop_atr_mult=1.0, per_day=1, ema_fast=20, ema_slow=50) -> list[Plan]
```

规则语义(反推依据写在各函数 docstring):
- `gen_momo_breakout`(近似 ZEC_MOM1/动量族):close > 前 momo_n 根最高且 close−close[-momo_n] > momo_atr×ATR → 顺势突破单,止损 ATR 倍数。
- `gen_dual_supertrend`(近似 Dual-SuperTrend 族):标准 SuperTrend(st_period/st_mult)翻线即入场,趋势线作初始止损,RR 目标。
- `gen_trend_pullback`(近似 Livermore/退婚重生/trend strategy 的 K 线子集):EMA50>EMA200 定多空方向,价回踩 EMA20 不破且收复(收盘价重新在 EMA20 同侧)时入场,逆势一律跳过。

三者共同实现约束(照抄 `gen_donchian` 骨架,`tools/audit/baseline_lab.py:46-93`):`cutoff` 用 `_cutoff_from_days(days)` 按 UTC+8 日期过滤;`per_day` + `cooldown_until = i + 8`;产出 `Plan(path="vergex-<rule>", symbol=symbol, timeframe=timeframe, record_ms=ts, order_type="突破单", direction=..., entry=c, stop=..., target=..., target2=None, cycle_position="baseline", diag_direction="baseline", trade_conf=None, diag_conf=None, est_win_rate=None)`;timeline 长度不足(如 < donchian_n+60 同级的暖机要求)返回空列表。

- [ ] **Step 1: 写失败测试**

```python
from tools.shadow_replay import simulate_plan
from tools.vergex_gens import gen_momo_breakout, gen_dual_supertrend, gen_trend_pullback

def _tl(n=300, drift=0.4):
    # 上行随机游走: 保证三种规则都有信号
    out, price, base = [], 100.0, 1_700_000_000_000
    import random
    rng = random.Random(7)
    for i in range(n):
        o = price
        price = max(1.0, price + drift + rng.uniform(-0.6, 0.6))
        out.append((base + i * 900_000, o, price + 0.3, price - 0.3, price))
    return out

PLANS = (gen_momo_breakout(_tl(), "ZECUSDT", "15m"),
         gen_dual_supertrend(_tl(), "ZECUSDT", "15m"),
         gen_trend_pullback(_tl(), "ETHUSDT", "15m"))

def test_each_gen_produces_plans():
    for plans in PLANS:
        assert plans, "上行序列应产生信号"

def test_plan_geometry_valid():
    for plans in PLANS:
        for p in plans:
            assert p.stop < p.entry < p.target
            assert p.order_type == "突破单" and p.direction in ("做多", "做空")

def test_simulate_plan_accepts_plans():
    tl = _tl()
    outs = [simulate_plan(p, tl, risk_usdt=4.0, fee_rate=0.0, leverage=10,
                          margin_usdt=250.0, partial_pct=50.0, timeout_min=60,
                          time_stop_min=720, min_stop_pct=0.2, min_stop_atr=0.0,
                          be_trigger=0.5, entry_gtx=True, maker_fee=0.0003,
                          taker_fee=0.0006) for p in PLANS[0]]
    assert outs and all(hasattr(o, "filled") for o in outs)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_vergex_gens.py -v`
Expected: FAIL(模块不存在)

- [ ] **Step 3: 实现**(SuperTrend 参考实现,其余两条同骨架)

```python
def _supertrend(klines, period=10, mult=3.0):
    """返回与 klines 等长的 (trend(1/-1), st_line) 列表; 暖机段为 (0, nan)."""
    import math
    trs, out = [], []
    prev_close = None
    upper = lower = None
    trend = 0
    for i, (_ts, _o, h, low, c) in enumerate(klines):
        tr = h - low if prev_close is None else max(
            h - low, abs(h - prev_close), abs(low - prev_close))
        trs.append(tr)
        atr = sum(trs[-period:]) / min(period, len(trs))
        mid = (h + low) / 2
        up, lo = mid + mult * atr, mid - mult * atr
        if upper is not None:
            up = min(up, upper) if c > upper else up
            lo = max(lo, lower) if c < lower else lo
        if trend <= 0 and c > (upper or math.inf):
            trend = 1
        elif trend >= 0 and c < (lower or -math.inf):
            trend = -1
        line = lo if trend == 1 else up
        out.append((trend, line))
        upper, lower, prev_close = up, lo, c
    return out
```

`gen_dual_supertrend` 主循环:遍历 `range(st_period + 2, len(timeline) - 5)`,当 `trend` 由 -1→1 且当日未超 `per_day` 且非冷却,按 `Plan(...)` 追加,`stop=line`,`target=entry+target_rr*|entry-stop|`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_vergex_gens.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add tools/vergex_gens.py tests/unit/test_vergex_gens.py
git commit -m "✨ feat(vergex): 动量突破/双Supertrend/趋势回踩三个反推生成器"
```

---

### Task 5: baseline_lab 集成 + Binance 时间线源 + 滚动样本外运行

**Files:**
- Modify: `tools/audit/baseline_lab.py:95-125`(RULES/PARAM_GRID/run_rule)与 `main()` 时间线来源
- Create: `research/vergex/oos_run_YYYY-MM-DD.md`(运行产物)

**Interfaces:**
- Consumes: Task 4 三个生成器;`tools.sim_trailing_stop.fetch_klines`。
- Produces: `RULES: dict[str, tuple[Callable, list[dict]]]`(rule → (gen, 参数网格 dict 列表));`build_timeline_from_klines(cache_dir, symbol, interval) -> list`;CLI 新增 `--source records|binance --symbols ZECUSDT,... --kline-cache PATH --json-out PATH`。

- [ ] **Step 1: 写失败测试**

```python
import json
from pathlib import Path
from tools.audit.baseline_lab import build_timeline_from_klines

def test_build_timeline_from_klines(tmp_path):
    rows = {"ZECUSDT": [[1700000000000 + i * 900000, "10", "11", "9", "10.5", "1.0"]
                        for i in range(3)]}
    (tmp_path / "klines.json").write_text(json.dumps(rows))
    tl = build_timeline_from_klines(tmp_path, "ZECUSDT")
    assert len(tl) == 3 and tl[0][0] == 1700000000000 and tl[0][4] == 10.5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_vergex_gens.py tests/unit -k build_timeline -v`
Expected: FAIL(函数不存在)

- [ ] **Step 3: 实现**

`build_timeline_from_klines`:`json.loads((cache_dir/"klines.json"))[symbol]`,每行取前 5 列转 float、ts 转 int,按 ts 排序。

`RULES` 重构(Task 4 后):

```python
RULES = {
    "ema_pullback": (generate_baseline_plans, [{"ema_period": e, "target_rr": rr,
                     "stop_atr_mult": a} for e in (20, 30, 50)
                     for rr in (1.5, 2.0, 3.0) for a in (1.0, 1.5)]),
    "donchian_break": (gen_donchian, [{"donchian_n": n, "target_rr": rr,
                       "stop_atr_mult": a} for n in (10, 20, 40)
                       for rr in (1.5, 2.0) for a in (1.0, 1.5)]),
    "momo_break": (gen_momo_breakout, [{"momo_n": n, "momo_atr": m, "target_rr": rr,
                   "stop_atr_mult": a} for n in (10, 14, 20) for m in (1.0, 1.5, 2.0)
                   for rr in (1.5, 2.0) for a in (1.0, 1.5)]),
    "dual_supertrend": (gen_dual_supertrend, [{"st_period": p, "st_mult": m,
                        "target_rr": rr} for p in (10, 20) for m in (2.0, 3.0, 4.0)
                        for rr in (1.5, 2.0, 3.0)]),
    "trend_pullback": (gen_trend_pullback, [{"ema_fast": f, "ema_slow": s,
                       "target_rr": rr} for f in (10, 20) for s in (50, 100, 200)
                       for rr in (1.5, 2.0, 3.0)]),
}
```

`run_rule` 参数传递改为 `plans = gen(tl, "", "", days=args.days, **combo)`,combo 内固定键由各 gen kwargs 接住;`main()` 线1a 循环改 `for rule_name, (gen, grid) in RULES.items()`,网格打印用 `json.dumps(best, sort_keys=True)`。

时间线来源:`--source binance` 时跳过 `load_plans/merge_timelines`,改为对 `--symbols` 逐个 `build_timeline_from_klines`(缓存缺失时先调 `fetch_klines`,start/end 由 `--days` 推:`window_bounds(days)` 复用 `_pa_sim_common`);`_TF_CACHE[id(tl)] = (symbol, interval_map[interval])`,`interval_map = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}`。

`--json-out`:运行结束时把线1a 的 `oos_rows` 与每规则全样本最优(`n/wr/r_sum/avg_r/combo`)写 JSON,供 Task 6。

- [ ] **Step 4: 跑测试与既有回归**

Run: `uv run pytest tests/unit -k "vergex or baseline" -v`
Expected: 全 PASS

- [ ] **Step 5: 双源运行(产物落盘)**

```bash
# 品种时间线来自历史决策记录(与既有口径一致)
uv run python tools/audit/baseline_lab.py --days 45 --segments 5 \
  --json-out research/vergex/oos_records.json
# VergeX 策略品种时间线来自 Binance K 线
uv run python tools/audit/baseline_lab.py --days 120 --segments 6 \
  --source binance --symbols BTCUSDT,ETHUSDT,ZECUSDT \
  --kline-cache research/vergex/data/_klines \
  --json-out research/vergex/oos_binance.json
```

Acceptance:两份 JSON 生成;至少一条 vergex 规则 OOS R合计 > 0;把两次运行的控制台摘要抄进 `research/vergex/oos_run_2026-09-14.md` 并写判读。

- [ ] **Step 6: Commit**

```bash
git add tools/audit/baseline_lab.py research/vergex
git commit -m "✨ feat(vergex): baseline_lab 接入三生成器与 Binance 时间线源"
```

---

### Task 6: 还原度报告

**Files:**
- Create: `tools/vergex_report.py`
- Test: `tests/unit/test_vergex_report.py`
- Create: `research/vergex/还原度矩阵_YYYY-MM-DD.md`(运行产物)

**Interfaces:**
- Consumes: Task 5 的 `oos_binance.json`(或全样本最优段);`PLATFORM_REFERENCE` 常量。
- Produces: `verdict(ref_wr, ref_n, rep_wr, rep_n, *, wr_tol=8.0, n_tol=0.25) -> "达标"|"超差"|"样本不足"(rep_n<30)`;CLI `uv run python tools/vergex_report.py --oos research/vergex/oos_binance.json --out research/vergex/还原度矩阵_2026-09-14.md`。

```python
PLATFORM_REFERENCE = {
    "eth_mid":        {"strategy": "ETH中线策略-稳中求进", "rule": "trend_pullback",
                       "wr": 50.45, "n": 111, "ret": 281.87, "dd": 46.89},
    "btc_zec_rotation": {"strategy": "BTC_ZEC_MultiTF_Rotation", "rule": "dual_supertrend",
                       "wr": 45.79, "n": 653, "ret": 399.32, "dd": 58.91},
    "zec_mom1_5x":    {"strategy": "ZEC_MOM1_5x_50pct", "rule": "momo_break",
                       "wr": 28.37, "n": 282, "ret": 125.40, "dd": 38.63},
    "zec_macd":       {"strategy": "ZEC_MACD", "rule": "momo_break",
                       "wr": 32.65, "n": 49, "ret": 116.68, "dd": 42.22},
    "zec_high_freq":  {"strategy": "ZEC高频趋势追踪大王", "rule": "momo_break",
                       "wr": 41.76, "n": 91, "ret": 689.16, "dd": 87.07},
}
```

- [ ] **Step 1: 写失败测试**

```python
from tools.vergex_report import verdict

def test_verdict_bands():
    assert verdict(50.45, 111, 52.0, 100) == "达标"
    assert verdict(50.45, 111, 60.0, 100) == "超差"   # 胜率差 9.55pp
    assert verdict(50.45, 111, 52.0, 70) == "超差"    # 笔数差 37%
    assert verdict(50.45, 111, 52.0, 10) == "样本不足"
```

- [ ] **Step 2: 跑测试确认失败** → Run: `uv run pytest tests/unit/test_vergex_report.py -v`,Expected: FAIL

- [ ] **Step 3: 实现**:`verdict` 按判据三段;报告矩阵列 = 策略/规则/平台wr/回放wr/平台n/回放n/平台ret/回放ret/判定;文档尾部固定附"不可还原边界"四条(本计划头部)。
- [ ] **Step 4: 跑测试确认通过** → Expected: 1 PASS(4 断言)
- [ ] **Step 5: Commit**

```bash
git add tools/vergex_report.py tests/unit/test_vergex_report.py research/vergex
git commit -m "📝 docs(vergex): 还原度矩阵报告工具与首轮对照"
```

---

### Task 7(可选收尾): 提示词策略 → PA_Agent 策略文件

**Files:**
- Create: `prompt_engineering/文件24-利弗莫尔趋势跟随移植.txt`
- Create: `prompt_engineering/文件25-三层评分趋势移植.txt`

**Interfaces:**
- Consumes: `research/VergeX提示词策略Top20全文_2026-09-13.md` 中 #10 Livermore 与 #18 退婚重生全文;PA_Agent 现有特征词汇(`SimpleMarketFeatures` 渲染块、kline_features 表、`supports/resistances`、`barbwire`)。
- Produces: 两个策略文件(stage2 策略库加载,`strategy_files` 分桶统计自动生效)。

- [ ] **Step 1: 翻译规则**(逐条映射,禁止保留平台专有词)

| VergeX 原文概念 | PA_Agent 对应 |
|----------------|---------------|
| AI500 评分 ≥75 | 不移植(平台私有),换成"突破质量=surviving 或 swing_structure 同向" |
| EMA50/200 关系 | kline_features 的 ema_relation + ema_gap_count |
| 关键点突破 | supports/resistances + breakout_quality |
| 三态市场 | cycle_position 二元决策 + barbwire_candidate(震荡) |
| 评分 ≥65 开仓 | trade_confidence 档位描述,交 stage2 置信度体系 |
| {{TAKE_PROFIT}} 模板变量 | 删除,由 stage2 结构位目标替代 |

- [ ] **Step 2: 按现有策略文件格式落盘**(先读 `prompt_engineering/文件13-窄通道与宽通道策略.txt` 的结构:人设段/条件段/禁止段),每个文件 60-120 行,人设+规则+禁止三段,中文。
- [ ] **Step 3: 48h shadow 验证**:GUI 开启 shadow/监控,`trade_records/outcomes.csv` 观察 `strategy_files` 分桶(rule_stats `min_samples` 未满前不下结论)。
- [ ] **Step 4: Commit**

```bash
git add prompt_engineering
git commit -m "✨ feat(prompt): Livermore/三层评分趋势策略移植文件"
```

---

## 任务依赖与里程碑

```
Task 1(数据) → Task 2(加载器) → Task 3(对齐) → Task 4(生成器) → Task 5(集成+OOS) → Task 6(还原度报告)
                                                                                  ↘ Task 7(提示词移植, 独立分支)
```

- 里程碑 A(Task 1-3, 约 1 天):4 份对齐报告,拿到反推参数。
- 里程碑 B(Task 4-6, 约 2-3 天):还原度矩阵出炉,判定达标/超差。
- 里程碑 C(Task 7, 约 1 天 + 48h shadow):LLM 链路侧移植。
