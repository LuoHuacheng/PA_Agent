# ruff: noqa: RUF002, RUF003 - Chinese config copy
"""Pydantic settings models for PA Agent."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionStance = Literal["conservative", "balanced", "aggressive", "extreme_aggressive"]
DataSourceKind = Literal["mt5", "tradingview"]
NormalizationMode = Literal["strict", "lenient"]


class AIProviderSettings(BaseModel):
    """AI provider connection and behaviour settings."""

    model_config = ConfigDict(extra="ignore")

    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    thinking: bool = True
    reasoning_effort: Literal["low", "medium", "high", "max"] = "high"
    context_window: int = 2_000_000


class PromptSettings(BaseModel):
    """Prompt assembly tuning (accuracy-oriented defaults)."""

    model_config = ConfigDict(extra="ignore")

    #: When True, Stage 2 loads every strategy .txt (legacy/test behaviour).
    stage2_load_full_strategy_library: bool = False
    experience_max_entries: int = Field(default=0, ge=0, le=10)
    experience_max_chars_per_entry: int = Field(default=400, ge=100, le=4000)
    #: Inject pattern判定表 + 速查 brief into Stage 1 user prompt (reduces missed tags).
    stage1_inject_pattern_briefs: bool = True
    #: 阶段一 K 线表最多渲染的最近 K 行数; 0 = 不裁剪(默认, 全量). 裁剪时
    #: 更早的 K 线以十根滚轴概览补充 (Phase C A/B 实验开关).
    stage1_kline_rows_limit: int = Field(default=0, ge=0, le=60)


class ValidationSettings(BaseModel):
    """Post-LLM validation behaviour."""

    model_config = ConfigDict(extra="ignore")

    normalization_mode: NormalizationMode = "lenient"
    #: Stage-1 cross-field checks (gate trace, bar_by_bar, pattern tags). Off by default.
    stage1_coherence_checks: bool = False
    #: Stage-2 trace / diagnosis cross-checks (not order safety). Off by default.
    stage2_coherence_checks: bool = False
    trace_semantic_checks: bool = False
    strict_bar_by_bar_features: bool = False
    #: Allow Stage 1 truncated JSON tail repair before failing syntax validation.
    disable_truncation_repair: bool = False
    #: Re-call API with structured feedback when validation fails (format errors).
    retry_enabled: bool = True
    retry_max: int = Field(default=3, ge=0, le=5)
    #: Max retries for category=c semantic errors (subset only).
    retry_max_semantic: int = Field(default=1, ge=0, le=3)
    retry_stage2: bool = True


class GeneralSettings(BaseModel):
    """UI and data-feed general settings."""

    model_config = ConfigDict(extra="ignore")

    analysis_bar_count: int = Field(default=100, ge=2, le=5000)
    refresh_interval_ms: int = 1000
    context_warning_threshold_pct: float = 80.0
    last_data_source: DataSourceKind = "tradingview"
    #: A-share K-line adjust for East Money / Baostock (qfq=前复权)
    kline_adjust: Literal["qfq", "hfq", "none"] = "qfq"
    #: TradingView 交易所；空字符串 =（自动）依次探测预设列表
    last_tradingview_exchange: str = ""
    #: TradingView 登录凭据 (tvDatafeed 登录会话, 匿名限流宽松、部分品种仅登录可见);
    #: 两者都非空才登录, 否则退回匿名。也可用环境变量 TRADINGVIEW_USERNAME /
    #: TRADINGVIEW_PASSWORD (settings 为空时自动读环境变量)。
    tradingview_username: str = ""
    tradingview_password: str = ""
    last_symbol: str = "XAUUSD"
    last_timeframe: str = "15m"
    decision_flow_auto_play: bool = True
    decision_flow_play_seconds: int = 50
    #: 阶段二给出限价/突破/市价单时：警报音、弹窗，并自动切到「决策」页（跳过决策树可视化演示）
    alert_on_order_opportunity: bool = True
    incremental_max_new_bars: int = Field(default=10, ge=0, le=500)
    #: 阶段二交易倾向：balanced=默认；conservative/aggressive 逐级调整下单意愿
    decision_stance: DecisionStance = "balanced"
    #: 决策树可视化：在「整图适配」基础上的缩放百分比（100=与适配一致；可任意放大，仅下限 10%）
    decision_flow_default_zoom_pct: int = Field(default=600, ge=10)
    #: 「实时」页思考过程/撰写回答框与追问输入框的等宽字体字号（pt）
    stream_pane_font_pt: int = Field(default=11, ge=8, le=28)
    #: K 线图上 #序号 标签的字号（pt）
    chart_seq_label_font_pt: int = Field(default=11, ge=6, le=24)
    #: 两阶段分析结束后是否自动恢复 K 线图表实时刷新
    auto_resume_chart_after_analysis: bool = False
    #: 持续跟踪分析：有新K线收盘时自动触发新一轮分析
    keep_analysis: bool = False
    #: 重试后取消持续跟踪分析：校验失败触发重试后自动关闭 keep_analysis
    cancel_keep_analysis_on_retry: bool = False
    #: 交易决策置信度门槛：仅当 trade_confidence >= 此值时，才视为有下单机会（弹窗警报并提供决策详情）
    # GUI/manual-decision confidence gate. Monitoring auto-execution derives
    # its threshold from the stance tiers (see decision_stance).
    decision_confidence_threshold: int = Field(default=40, ge=0, le=100)
    #: 开启下根K线预期功能；关闭时不向模型请求该预测，节省 token
    enable_next_bar_prediction: bool = False
    #: 同一结构位 entry 相差≤3跳时，禁止反向新方案的冷却 K 线根数（已收盘）
    structure_flip_cooldown_bars: int = Field(default=3, ge=1, le=50)

    @field_validator("last_data_source", mode="before")
    @classmethod
    def _coerce_legacy_data_source(cls, v: object) -> object:
        # 已下线的 A 股/多源适配器(akshare/eastmoney/tushare/yfinance)一律回落 tradingview
        if v not in ("mt5", "tradingview", None, ""):
            return "tradingview"
        return v

    @field_validator("decision_flow_default_zoom_pct", mode="before")
    @classmethod
    def _coerce_zoom_pct(cls, v: object) -> object:
        if v is None:
            return 50
        return v


_FEISHU_CONFIG_KEYS = (
    "enabled",
    "webhook_url",
    "secret",
    "app_id",
    "app_secret",
    "notify_on_order_only",
)


class FeishuSettings(BaseModel):
    """Feishu bot notification settings (persisted in settings.json)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    webhook_url: str = ""
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    #: True = only push when there is an order opportunity.
    notify_on_order_only: bool = True



class PushPlusSettings(BaseModel):
    """PushPlus notification settings (settings.json only; no GUI)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""


class TelegramSettings(BaseModel):
    """Telegram Bot notification settings (settings.json only; no GUI)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class BinanceUSDMTestnetSettings(BaseModel):
    """Binance U本位合约 Testnet automatic-execution safety controls.

    Credentials persist in the gitignored local ``settings.json`` file.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    dry_run: bool = True
    emergency_stop: bool = True
    require_analysis_symbol_match: bool = True
    symbol: str = "BTCUSDT"
    symbol_whitelist: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    leverage: int = Field(default=1, ge=1, le=20)
    max_notional_usdt: float = Field(default=100.0, gt=0, le=10000.0)
    # P2-1 单笔风险等额仓位: 0=关闭(维持名义=保证金*杠杆), 0<x<=1000 时按
    # 风险金/|入场价-止损价| 计算 quantity, 每笔最大亏损(不含费)相同;
    # 所需名义仍受 max_notional_usdt*leverage 上限约束, 超出拒单.
    risk_per_trade_usdt: float = Field(default=0.0, ge=0.0, le=1000.0)
    cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    # Enforce the §10.3 trader's equation (win_rate×reward > (1-win_rate)×risk)
    # before automating a market order. Default on for safety.
    require_trader_equation: bool = True
    limit_order_enabled: bool = True
    # Resting limit entries use a fill watcher and attach TP/SL after fill.
    limit_fill_timeout_minutes: int = Field(default=60, ge=1, le=1440)
    limit_poll_interval_seconds: int = Field(default=10, ge=2, le=300)
    # 同价位轮换冷却：上一笔挂单被撤后，若新方案的入场价与它相差不超过 N 跳，
    # 且在冷却窗内，则跳过本次挂单（同价位反复撤挂只赔价差与手续费）。
    # 任一参数取 0 即关闭该闸门。
    limit_repricing_min_ticks: int = Field(default=3, ge=0, le=1000)
    limit_repricing_cooldown_minutes: int = Field(default=30, ge=0, le=1440)
    # Stop loss minimum distance from the entry/mark price (percent). "fixed"
    # mode: constant min_stop_distance_pct floor. "atr" mode: floor =
    # max(min_stop_distance_pct, min_stop_atr_multiple × 分析周期 ATR%), where
    # ATR% comes from the latest analyzed bar (decision.atr_pct, injected by the
    # monitor) - high-volatility symbols get a noise-safe minimum while quiet
    # symbols keep tight structural stops. Without atr_pct the plain
    # min_stop_distance_pct floor applies. (P0-2 / 动态止损下限)
    min_stop_mode: Literal["fixed", "atr"] = "fixed"
    min_stop_distance_pct: float = Field(default=0.45, ge=0.0, le=10.0)
    min_stop_atr_multiple: float = Field(default=0.8, ge=0.0, le=10.0)
    # Rate-limit recovery: 限流失败不重试(one-shot, 见 execute_market_signal docstring)。
    # 检测到 Testnet 共享 IP 限流(HTTP 418/-1003, banned until)时, 暂停监控的
    # 行情分析与信号推送直到封禁到期(错过的不补发信号); False = 保持现状.
    pause_monitoring_on_rate_limit: bool = True
    # 限流响应无 banned-until 时间戳(如纯 429)时按此秒数估算封禁窗口.
    rate_limit_no_until_seconds: int = Field(default=60, ge=5, le=3600)
    # 用户数据流(user-data websocket): True = 订阅订单/账户推送事件, REST 轮询
    # 降级为低频对账(共享 IP 限流缓解, P0)。默认关闭, 人工验证连通后开启。
    user_data_stream_enabled: bool = False
    # 用户数据流 WS 网关; 留空 = 默认测试网网关。live 迁移时换成主网网关。
    user_data_stream_ws_url: str = ""
    # WS 事件触发快照刷新/唤醒的最小间隔(秒): 事件风暴防抖.
    user_data_event_refresh_gap_seconds: int = Field(default=10, ge=1, le=300)
    # --- 日线大趋势护栏 (逆势单保护) ---
    # 以币种日线 close 的 trend_lookback_days 天涨跌幅定义大趋势; |涨跌| <=
    # trend_neutral_band_pct 视为无趋势 (不做限制). 仅作用于白名单内的币种.
    # 命名不写死天数: 窗口本身是配置值, 早期 trend_30d_* 的写法在窗口调成 7 天
    # 之后已经名不副实 (2026-09-10 改名).
    trend_lookback_days: int = Field(default=7, ge=5, le=120)
    trend_neutral_band_pct: float = Field(default=1.0, ge=0.0, le=100.0)
    # 逆大趋势的单直接拒绝。2026-09-10 实证: 逆势单的方向命中比同期市场基线低
    # 24 个百分点(顺势 45.9% / 逆势 37.9% / 基线 62.1%), 是样本里最大的亏损来源。
    # 开启后 counter_trend_min_confidence 与 counter_trend_size_scale 不再参与
    # 判定(先拦, 再谈减仓)。
    counter_trend_block: bool = False
    # 逆大趋势的单所需最低 trade_confidence; 不足直接拒绝 (0 = 关闭该门槛).
    counter_trend_min_confidence: int = Field(default=55, ge=0, le=100)
    # 逆大趋势且通过门槛的单, 杠杆 (名义仓位随杠杆同比例) 乘以此系数, 最小 1x.
    counter_trend_size_scale: float = Field(default=0.5, ge=0.1, le=1.0)
    # --- 日度亏损熔断 ---
    # 当日已实现净亏损(REALIZED_PNL + COMMISSION, 不含资金费)达到该值时停止自动
    # 开新仓, 次日(本地日)自动解除。0 = 关闭。账本查询 weight 30, 所以当天一旦
    # 触发就不再重复查询(直接读运行时 state)。
    daily_loss_limit_usdt: float = Field(default=0.0, ge=0.0, le=10000.0)
    # --- 保本移动止损 (breakeven stop) ---
    # 持仓浮盈达标后把交易所 STOP 保护单移到入场价, 将"赚过又回吐"的亏损转成
    # 保本离场. 触发条件: 1r = 浮盈 >= 1R (risk = |entry - stop|); tp = 浮盈触及
    # TP1; 1r_or_tp = 两者先到先触发; 也支持分数 R (如 "0.5r" = 浮盈达 0.5R 即
    # 移保本), 用于 RR 较高/浅浮盈回头的单; off = 关闭该功能.
    breakeven_stop_trigger: str = "1r"
    # 仅对 trade_confidence >= 此值的信号启用保本止损 (0 = 全部启用).
    breakeven_min_confidence: int = Field(default=55, ge=0, le=100)
    # 持仓守护线程轮询 mark price 的间隔秒数.
    breakeven_poll_seconds: int = Field(default=10, ge=2, le=600)
    # 账户快照(poller)可接受的最大新鲜度, 秒: 0 = 自动
    # (max(45, 轮询周期 × 1.2), 保证轮询间隙内守护线程不因快照过期
    # 集体回退直连 REST 而放大共享 IP 限流)。
    snapshot_stale_seconds: int = Field(default=0, ge=0, le=600)
    # --- TP1 部分止盈 / runner(TP2) ---
    # 到达 TP1 时平掉此比例的仓位(0 = 关闭, 维持现状全平; 50 = 平一半),
    # 剩余仓位保本后继续持有至 TP2. 0-100 闭区间.
    tp_partial_close_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    # P2-2 持仓超时(分钟): 0=关闭; >0 时持仓超过该时长(自保护单挂上起算)后,
    # time-stop 线程市价清掉剩余仓位(TP2 阶段剩余半仓同样适用). 上限 7 天.
    time_stop_minutes: int = Field(default=0, ge=0, le=10080)
    # P2-3 conf->胜率回流运行时闸门: off=只用模型 estimated_win_rate(默认;
    # 回归显示样本不足, 不宜自动生效); on=当同 conf 5 分位桶实测样本 >=
    # conf_feedback_min_samples 时, executor 用桶实测胜率覆盖模型值参与
    # 交易者方程. 桶数据由 tools/trade_pnl_report.py --conf-buckets-out 生成.
    conf_feedback_mode: Literal["off", "on"] = "off"
    conf_feedback_min_samples: int = Field(default=30, ge=5, le=200)

    @field_validator("breakeven_stop_trigger")
    @classmethod
    def _validate_breakeven_trigger(cls, v: object) -> str:
        """Accept off|1r|tp|1r_or_tp plus fractional-R values (0.25r..1r)."""
        s = str(v or "").strip().lower()
        if s in ("off", "1r", "tp", "1r_or_tp"):
            return s
        if s.endswith("r") and len(s) > 1:
            try:
                factor = float(s[:-1])
            except ValueError as exc:
                raise ValueError(
                    "breakeven_stop_trigger must be off|1r|tp|1r_or_tp or a "
                    "fractional-R value such as 0.5r"
                ) from exc
            if 0 < factor <= 1:
                return f"{factor:g}r"
        raise ValueError(
            "breakeven_stop_trigger must be off|1r|tp|1r_or_tp or a "
            "fractional-R value such as 0.5r"
        )
    # --- 结构否定自动离场 (structure-failure exit) ---
    # 已开仓位在静态保护单之外, 还可响应诊断否定: 当 stage-1 方向连续
    # structure_exit_confirm_bars 根已收盘K与持仓相反, 且最新收盘已跌破入场价
    # (做多) / 升破入场价 (做空) 而尚未触及静态止损时, 提前市价平仓.
    # 模式: off = 关闭; dry_run = 只通知不成交 (默认, 安全观察); on = 自动平仓.
    structure_exit_mode: Literal["off", "dry_run", "on"] = "dry_run"
    # 诊断反向连续确认所需已收盘K线数 (每轮分析一根新K).
    structure_exit_confirm_bars: int = Field(default=2, ge=1, le=6)
    # --- 方向质量闸门 (direction gates) ---
    # 下单前的程序化方向过滤: G1 限价/突破单入场价贴近最近收盘价(<=2跳,
    # 追价而非回踩), G2 与上一轮诊断方向相反的即时新单. 模式: off = 关闭;
    # dry_run = 只记录不拦截 (默认, 先观察); on = 拒绝并拦截执行与通知.
    direction_gates_mode: Literal["off", "dry_run", "on"] = "dry_run"


class MonitorTarget(BaseModel):
    """One settings.json-defined background K-line monitoring target."""

    model_config = ConfigDict(extra="ignore")

    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=2)
    enabled: bool = True

    @field_validator("symbol", "timeframe")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class AutoDiscoverSettings(BaseModel):
    """Auto-discover monitor targets from Binance USDM 24h tickers.

    When enabled, the static ``targets`` list is replaced by the top-N
    USDⓈ-M contracts ranked by 24h quote volume (成交额) or by the absolute
    24h price change percentage (涨跌幅), refreshed periodically while the
    monitor runs.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    #: USDⓈ-M futures (fapi.binance.com/fapi/v1/ticker/24hr).
    market: Literal["usdm_futures"] = "usdm_futures"
    #: Rank by "quote_volume" (24h成交额), "price_change_pct" (涨跌幅绝对值)
    #: or "market_cap" (CoinGecko 市值排名, 候选池稳定、近似固定).
    rank_by: Literal["quote_volume", "price_change_pct", "market_cap"] = "quote_volume"
    top_n: int = Field(default=10, ge=1, le=100)
    refresh_minutes: int = Field(default=60, ge=5, le=1440)
    #: K-line timeframe applied to auto-discovered targets.
    timeframe: str = Field(default="15m", min_length=2)
    #: Keep only USDT-settled pairs (excludes USDC/FDUSD/TUSD/etc. markets).
    stablecoin_only: bool = True


class MonitoringSettings(BaseModel):
    """Settings for close-of-bar multi-symbol analysis, alerts, and (when
    binance_usdm_testnet.enabled) Testnet auto-execution.

    The monitor sends notifications and, if Binance Testnet automation is
    enabled, also invokes the guarded auto-executor for market signals.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    targets: list[MonitorTarget] = Field(default_factory=list)
    auto_discover: AutoDiscoverSettings = Field(default_factory=AutoDiscoverSettings)
    max_concurrent_analyses: int = Field(default=1, ge=1, le=8)
    poll_lead_seconds: int = Field(default=5, ge=0, le=120)
    poll_retry_attempts: int = Field(default=3, ge=0, le=10)
    poll_retry_seconds: int = Field(default=5, ge=1, le=120)
    #: C3 轻量模式: 无结构事件连续安静达 N 根时跳过阶段二(默认关, monitor only).
    light_mode_enabled: bool = False
    light_mode_max_quiet_bars: int = Field(default=3, ge=1, le=24)
    #: D1 HTF 背景摘要: 每根 K 线收盘额外拉取高周期程序特征(默认关).
    htf_context_enabled: bool = False
    htf_timeframes: list[str] = Field(default_factory=lambda: ["1h", "4h"])
    htf_max_summary_chars: int = Field(default=400, ge=100, le=2000)


class FeedbackSettings(BaseModel):
    """结果回灌闭环（Phase A）：base-rate 统计与校准工具的参数。

    全部默认关/默认安全：enabled 打开只影响离线统计与 prompt 注入，
    不触碰执行路径（binance_usdm_* 节才是自动交易开关）。
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    #: 统计滚动窗口（自然日）。
    days: int = Field(default=30, ge=1, le=365)
    #: 分组至少需多少已平仓样本才给出胜率/期望（门控，禁止小样本硬编）。
    min_samples: int = Field(default=10, ge=1, le=1000)
    #: 注入 stage2 的历史先验最大行数。
    max_prompt_lines: int = Field(default=6, ge=1, le=30)
    #: 决策可使用的分组键（strategy_file / cycle_direction）。
    group_bys: list[str] = Field(
        default_factory=lambda: ["strategy_file", "cycle_direction"]
    )
    #: 决策记录与成交归属的最大时间窗（小时）。
    join_hours: int = Field(default=48, ge=1, le=720)


class Settings(BaseModel):
    """Root settings object persisted to config/settings.json."""

    model_config = ConfigDict(extra="ignore")

    provider: AIProviderSettings = Field(default_factory=AIProviderSettings)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    validation: ValidationSettings = Field(default_factory=ValidationSettings)
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    pushplus: PushPlusSettings = Field(default_factory=PushPlusSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    #: 自动执行环境: "testnet" = 测试网(默认, 兼容既有配置), "live" = 实盘.
    #: 切换后 REST/WS 网关、运行时状态文件与通知标签均随环境解析,
    #: 但绝不代表实盘许可: live 节本身仍须显式 enabled + dry_run=false。
    binance_usdm_environment: Literal["testnet", "live"] = "testnet"
    #: 测试网自动执行节 (environment=testnet 时的活动配置, 语义不变)。
    binance_usdm_testnet: BinanceUSDMTestnetSettings = Field(
        default_factory=BinanceUSDMTestnetSettings
    )
    #: 实盘自动执行节: 字段与 testnet 节同构, 但默认全安全
    #: (enabled=false / dry_run=true / emergency_stop=true / 密钥为空),
    #: environment=live 时成为活动配置; 与 testnet 节互斥启用。
    binance_usdm_live: BinanceUSDMTestnetSettings = Field(
        default_factory=BinanceUSDMTestnetSettings
    )
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    feedback: FeedbackSettings = Field(default_factory=FeedbackSettings)


def provider_api_key_configured(settings: Settings | None) -> bool:
    """Return True when a non-empty API key is loaded in memory."""
    if settings is None:
        return False
    return bool((settings.provider.api_key or "").strip())


# ── Persistence ───────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)


def _migrate_legacy_feishu_json(raw: dict, settings_path: Path) -> bool:
    """Merge legacy config/feishu.json into settings.feishu when needed."""
    legacy_path = settings_path.parent / "feishu.json"
    if not legacy_path.exists():
        return False

    feishu = raw.setdefault("feishu", {})
    if (feishu.get("webhook_url") or "").strip():
        return False

    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("legacy feishu.json unreadable (%s); skipping migration", exc)
        return False

    migrated = False
    for key in _FEISHU_CONFIG_KEYS:
        if key not in legacy:
            continue
        value = legacy.get(key)
        if value in (None, ""):
            continue
        if feishu.get(key) in (None, ""):
            feishu[key] = value
            migrated = True
    if migrated:
        logger.info("Migrated Feishu config from %s into settings.json", legacy_path)
    return migrated


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from *path* (default: SETTINGS_JSON_PATH).

    Returns default Settings and writes them to disk if the file is absent.
    """
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH

    if not path.exists():
        defaults = Settings()
        save_settings(defaults, path)
        return defaults

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("settings.json unreadable (%s); using defaults", exc)
        return Settings()

    # Migrate legacy field names
    general = raw.get("general", {})
    if "cost_warning_threshold_pct" in general and "context_warning_threshold_pct" not in general:
        general["context_warning_threshold_pct"] = general.pop("cost_warning_threshold_pct")
    general.pop("last_htf_text", None)
    from pa_agent.data.market_defaults import migrate_general_gold_defaults

    migrate_general_gold_defaults(general)
    if "default_bar_count" in general and "analysis_bar_count" not in general:
        general["analysis_bar_count"] = general.pop("default_bar_count")
    raw["general"] = general
    provider = raw.get("provider", {})
    provider.pop("pricing", None)
    raw["provider"] = provider

    migrated_feishu = _migrate_legacy_feishu_json(raw, path)
    settings = Settings.model_validate(raw)
    dirty = migrated_feishu
    if (
        settings.pushplus.enabled
        and not settings.pushplus.token.strip()
        and not (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    ):
        settings.pushplus.enabled = False
        logger.info(
            "PushPlus enabled but token empty — auto-disabled (Feishu notifications unaffected)"
        )
        dirty = True
    if dirty:
        save_settings(settings, path)
    return settings


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Persist settings to *path* (default: SETTINGS_JSON_PATH)."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH

    path = path or SETTINGS_JSON_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    data = settings.model_dump()

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")