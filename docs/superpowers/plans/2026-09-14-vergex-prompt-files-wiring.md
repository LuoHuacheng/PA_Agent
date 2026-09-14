# VergeX 移植策略文件接入决策链路实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Task 7 产出的两个 VergeX 移植策略文件接入 PA_Agent stage2 决策链路,使其真实参与每根收盘的自动分析与下单决策,并按计划 Task 7 Step 3 的口径可被 48h shadow 验证观察。

**Architecture:** 决策链路已在线(monitoring 10 币 30m + testnet 自动执行 enabled/dry_run=false)。stage2 策略文件有三个加载点:① pattern 路由(`pa_agent/ai/router.py` 的 `route_strategy_files` + `_ALL_VALID_FILES` 白名单,消费 stage1 的 `detected_patterns`);② 全库模式(`prompt_assembler.py:800` 元组,`prompt.stage2_load_full_strategy_library` 开关,当前 false);③ 常驻基础文件(`prompt_assembler.py:793` `STAGE2_BASE_PROMPT_TXT_FILES`,每次 stage2 必注入)。本计划主案走 **③ 常驻注入**,② 与 ① 为备选(见"加载模式决策")。

**Tech Stack:** Python 3.12 / uv / pytest / ruff;零第三方新依赖;不改执行器、不改 settings.json 开关。

**Spec:**
- `prompt_engineering/文件24-利弗莫尔趋势跟随移植.txt`、`prompt_engineering/文件25-三层评分趋势移植.txt`(分支 feat/vergex-strategy-integration,commit 3f5a231)
- `docs/superpowers/plans/2026-09-14-vergex-strategy-integration.md` Task 7 Step 3(48h shadow 验证口径)
- `pa_agent/ai/router.py:41-100`、`pa_agent/ai/prompt_assembler.py:793-823`、`tests/unit/test_prompt_txt_files.py`

## 加载模式决策(已定,写明理由)

**主案:常驻基础文件(STAGE2_BASE_PROMPT_TXT_FILES)。** 理由:

1. **验证口径要求**:48h shadow 靠 `trade_records/outcomes.csv` 的 `strategy_files` 分桶观察效果 —— 只有常驻/全库模式,两个文件才会出现在每笔决策的 `strategy_files` 字段里;pattern 路由模式下没有对应 `detected_patterns` key,几乎不会命中,分桶统计形同虚设。
2. **语义匹配**:两文件是框架类策略(趋势跟随主线 / 三层过滤总纲),不是特定形态 playbook,决策风格应全程在场。
3. **成本量化**:30m × 10 币 = 每 30 分钟一轮 stage2,约 480 次/天;+2 文件 ≈ 124 行 ≈ 3.5k token,全价 worst case ≈ 1.7M token/天(DeepSeek flash 价数量级 ¥3/天以内),KV 前缀复用后更低。可接受。

**备选(不在本计划执行,接线点已探明):** pattern 路由需改三处 —— `router.py` 加 pattern 分支(`route_strategy_files` 内 `files.append(...)`)+ `_ALL_VALID_FILES` 补录 + `pattern_routing.py` 的 `STAGE1_DETECTED_PATTERNS_GUIDE` 判定表加行。切换时机:若 48h 后发现两文件对决策无增益且 token 成本敏感,再迁路由。

## 全局约束

- 只动 prompt 加载层;**不动** `monitoring`、`binance_usdm_*` 执行开关与既有风控闸门(block_trending_tr / block_session / 日亏熔断 30U 原样)。
- `prompt_routing` 文本里的"文件24/25"引用指向旧文件(最终旗形/MTR),**不得改动**。
- 重命名一律 `git mv` 保历史;文件内容除首行编号自引用外零修改。
- 提交信息 `<emoji> <type>(scope): 中文描述`。

---

### Task 1: 策略文件重编号(消除与既有 文件24/25 的撞号)

**Files:**
- Rename: `prompt_engineering/文件24-利弗莫尔趋势跟随移植.txt` → `prompt_engineering/文件29-利弗莫尔趋势跟随移植.txt`
- Rename: `prompt_engineering/文件25-三层评分趋势移植.txt` → `prompt_engineering/文件30-三层评分趋势移植.txt`

- [x] **Step 1: 确认 29/30 编号空闲**:`ls prompt_engineering/ | grep -E "文件29|文件30"` 应为空(2026-09-14 实测现有编号至 28)。
- [x] **Step 2: git mv 两个文件**(保历史)。
- [x] **Step 3: 内容自检**:`grep -n "文件24\|文件25\|文件29\|文件30" prompt_engineering/文件29-*.txt prompt_engineering/文件30-*.txt` —— 两文件若存在对自身旧编号的引用,改为新编号;其余内容零修改。
- [x] **Step 4: 确认 stage1 路由文本零混淆**:`pattern_routing.py`、`router.py` 中的"文件24/文件25"字样仍指旧文件,不加改动(人工核对一遍即可)。
- [x] **Step 5: Commit**

```bash
git add prompt_engineering
git commit -m "🔧 chore(prompt): 移植策略文件重编号 24/25 为 29/30 消除撞号"
```

---

### Task 2: 常驻接线 + 测试同步

**Files:**
- Modify: `pa_agent/ai/prompt_assembler.py:793-798`(`STAGE2_BASE_PROMPT_TXT_FILES`)
- Modify: `tests/unit/test_prompt_txt_files.py`(`test_stage2_txt_files_order` 的硬编码 `-4:`)
- Test: `tests/unit/test_prompt_txt_files.py`(新增常驻断言)

**Interfaces:**
- Consumes: `stage2_user_task_txt_files(strategy_files, direction, load_full_strategy_library)`(prompt_assembler.py:842,`core.extend(STAGE2_BASE_PROMPT_TXT_FILES)` 无需改动)。
- Produces: 每次 stage2 user prompt 的 base 文件由 4 个变 6 个(逐棒分析检查单 / 文件16 / 文件17 / 文件23 / 文件29 / 文件30)。

- [x] **Step 1: 写失败测试**(追加到 test_prompt_txt_files.py):

```python
def test_stage2_base_contains_transplanted_strategies() -> None:
    from pathlib import Path

    base = STAGE2_BASE_PROMPT_TXT_FILES
    assert "文件29-利弗莫尔趋势跟随移植.txt" in base
    assert "文件30-三层评分趋势移植.txt" in base
    for name in ("文件29-利弗莫尔趋势跟随移植.txt", "文件30-三层评分趋势移植.txt"):
        assert (Path("prompt_engineering") / name).exists(), f"磁盘缺文件: {name}"


def test_stage2_routed_includes_base_transplants() -> None:
    # 路由模式下(默认 false 全库), 移植文件仍应随 BASE 常驻注入
    files = stage2_user_task_txt_files([], direction="bullish")
    assert "文件29-利弗莫尔趋势跟随移植.txt" in files
    assert "文件30-三层评分趋势移植.txt" in files
```

- [x] **Step 2: 跑测试确认失败**:`uv run pytest tests/unit/test_prompt_txt_files.py -v` → 新增 2 用例 FAIL(文件名不在 base)。
- [x] **Step 3: 实现**:`STAGE2_BASE_PROMPT_TXT_FILES` 元组末尾追加两个文件名(保持现有 4 项顺序不变,新文件排最后 —— 与 `test_stage2_txt_files_order` 的尾部断言兼容)。
- [x] **Step 4: 同步既有断言**:`test_stage2_txt_files_order` 里 `assert files[-4:] == list(STAGE2_BASE_PROMPT_TXT_FILES)` 的硬编码 `4` 改为 `len(STAGE2_BASE_PROMPT_TXT_FILES)`(否则 BASE 4→6 必挂):
```python
assert files[-len(STAGE2_BASE_PROMPT_TXT_FILES):] == list(STAGE2_BASE_PROMPT_TXT_FILES)
```
- [x] **Step 5: 跑测试确认通过**:`uv run pytest tests/unit/test_prompt_txt_files.py -v` 全 PASS。
- [x] **Step 6: Commit**

```bash
git add pa_agent/ai/prompt_assembler.py tests/unit/test_prompt_txt_files.py
git commit -m "✨ feat(prompt): Livermore/三层评分移植文件常驻 stage2 基础注入"
```

---

### Task 3: 路由白名单补录 + shadow 验证清单

**Files:**
- Modify: `pa_agent/ai/router.py:71-96`(`_ALL_VALID_FILES`)
- Create: `docs/vergex移植策略shadow验证清单.md`

**Interfaces:**
- `_ALL_VALID_FILES` 是 routed 文件名的合法性白名单(stage1 `strategy_files_needed` 中不在集合内的名字会被丢弃)。BASE 文件虽不经此校验,但补录后未来迁 pattern 路由模式(备选方案)时无需再改此处,且 stage1 模型点名移植文件不会静默失效。

- [x] **Step 1: `_ALL_VALID_FILES` 追加** `"文件29-利弗莫尔趋势跟随移植.txt"`、`"文件30-三层评分趋势移植.txt"`(保持文件名字典序/分组风格)。
- [x] **Step 2: 回归**:`uv run pytest tests/unit -k "prompt or router" -v` 全 PASS(router 相关既有测试不受影响)。
- [x] **Step 3: 写 shadow 验证清单**(docs/vergex移植策略shadow验证清单.md),内容必须含:
  - 重启指引:代码改动需重启 `pa-agent`(GUI;monitor 随 GUI 自启)或 `pa-monitor`(无头,参考 `config/monitoring.pid` 确认旧进程已停)。
  - 观察口径:48h 后 `trade_records/outcomes.csv` 按 `strategy_files` 分桶;重点看文件29/30 在场的决策桶胜率/均 R 与其不在场桶的对比;`rule_stats` 的 `min_samples` 未满不下结论(预期两文件常驻后每笔决策都在场,分桶即全样本)。
  - 回滚路径:从 `STAGE2_BASE_PROMPT_TXT_FILES` 移除两行并重启即完全回退;执行侧无需任何改动。
  - 决策行为红旗清单:stage2 若出现与移植文件规则冲突的输出(如趋势市 barbwire 开仓、逆 EMA 关系的三价),先查两文件注入是否与既有 22 个 playbook 打架(共注入 6 个 base + 路由文件),必要时退回路由模式(备选接线点见"加载模式决策")。
- [x] **Step 4: Commit**

```bash
git add pa_agent/ai/router.py docs/
git commit -m "📝 docs(prompt): 路由白名单补录移植文件 + 48h shadow 验证清单"
```

---

### Task 4: 上线验证(会话内可完成部分)

- [x] **Step 1: 全量回归**:`uv run pytest tests/unit` 全 PASS(基线 1099 passed, 1 skipped,允许新增用例)。
- [x] **Step 2: lint**:`uv run ruff check pa_agent/ai/prompt_assembler.py pa_agent/ai/router.py tests/unit/test_prompt_txt_files.py`。
- [x] **Step 3: 注入冒烟**(不依赖 GUI):用 prompt_assembler 直接构造一次 `stage2_prompt_txt_files([], direction="bullish")`,确认返回列表含 6 个 base 文件且两移植文件在末尾。
- [ ] **Step 4: 48h shadow 观察为人工步骤**(未完成:需操作者重启 + 等 48h),按 Task 3 清单执行,不在会话内等待;启动重启动作由操作者执行(GUI 会话)。

## 验收判据

1. Task 2 两个新测试 PASS;全量单测无回归。
2. 重启后任取一份新 `records/pending/*.json`,其 stage2 调试信息(或 `_prompt_files_panel` 调试 tab)的 stage2 文件列表含文件29/30。
3. 48h 后 `outcomes.csv` 的 `strategy_files` 分桶可按两文件过滤(常驻在场,应有非零样本)。
4. 执行开关零变化:`binance_usdm_testnet.enabled/dry_run` 与既有闸门配置前后一致(计划不触碰)。

## 任务依赖

```
Task 1(重编号) → Task 2(常驻接线) → Task 3(白名单+清单) → Task 4(验证)
```

预计总量:3 个小提交,代码改动 < 15 行,文档 1 篇;半天内完成(不含 48h shadow 等待)。

---

## 执行状态(2026-09-14 会话内)

- 实际提交 5 个(计划 3 个 + 落盘口径修正 1 个 + 验收数据补录 1 个),见 `feat/vergex-strategy-integration`:
  `e6afe69` 重编号 → `d2324c0` 常驻接线 → `848a4f5` 白名单+清单 → `52a118e` `strategy_files_used` 口径修正 → `d94214a` 验收数据。
- **计划外修正(必需)**:原计划假设"常驻/全库模式下两文件会出现在每笔决策的 `strategy_files` 字段"不成立 ——
  `strategy_files_used` 原先只记路由结果,BASE 文件从不落盘,`outcomes.csv` 分桶将永远看不到 29/30
  (验收判据 3 与 Task 3 观察口径都会失效)。已在 `two_stage.py` Step 10 把 BASE 常驻文件并入记录,
  并加 `tests/integration/test_two_stage_strategy_files_base.py` 锁定。
- 实测:`tests/unit` 1101 passed/1 skipped(基线 1099+新增 2);`tests/integration` 22 passed;
  `tests/property` PASS;`tests/e2e` 4 例失败为基线既有(GUI 夹具 `ctx.settings` 为 None,worktree 同栈复现);
  ruff 零新增(基线 1483 → 1482,存量不净与本次无关)。
- 未完成(人工):重启 `pa-agent`/`pa-monitor`、48h shadow 观察 —— 见 `docs/vergex移植策略shadow验证清单.md`。
