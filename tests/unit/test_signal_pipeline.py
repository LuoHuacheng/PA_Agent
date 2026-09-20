"""Tests for the unified order SignalPipeline (GUI/monitor shared seam)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pa_agent.config.settings import Settings
from pa_agent.trading.signal_pipeline import (
    OrderSignal,
    SignalPipeline,
    frame_atr_pct,
    signal_notification_allowed,
)


class _Res:
    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason
        self.symbol = "BTCUSDT"


def _frame(atr: float = 1.5, close: float = 100.0) -> Any:
    # OHLC 齐全: infer_price_tick_from_frame 需要 open/high/low 才能推出 tick,
    # 缺字段时 tick 推断静默失败(价格保持原样), 会让 tick 相关断言测了个寂寞。
    return type(
        "Frame",
        (),
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "bars": (
                type(
                    "Bar",
                    (),
                    {"open": close, "high": close, "low": close - 0.1, "close": close},
                )(),
            ),
            "indicators": type("Ind", (), {"atr14": (atr,)})(),
        },
    )()


def _decision(order_type: str = "市价单", conf: int = 90) -> dict:
    return {
        "decision": {
            "order_type": order_type,
            "order_direction": "做多",
            "trade_confidence": conf,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 110,
        }
    }


def _signal(decision: dict, frame: Any = None) -> OrderSignal:
    return OrderSignal(
        decision=decision,
        inner=decision["decision"],
        symbol="BTCUSDT",
        timeframe="15m",
        frame=frame,
    )


def _patch_io(monkeypatch, exec_status: str = "submitted") -> tuple[list, list]:
    """Keep the pipeline away from disk/network; return (exec_calls, notified)."""
    exec_calls: list[dict] = []
    notified: list[str] = []
    monkeypatch.setattr(
        "pa_agent.records.trade_logger.save_trade_record", lambda **_kw: None
    )
    monkeypatch.setattr(
        "pa_agent.records.trade_logger.latest_chart_image",
        lambda _s, _t: None,
    )

    def fake_execute(inner, _settings, *, analysis_symbol=""):
        exec_calls.append({"inner": inner, "analysis_symbol": analysis_symbol})
        return _Res(exec_status)

    monkeypatch.setattr(
        "pa_agent.trading.binance_usdm_testnet.execute_market_signal", fake_execute
    )
    for mod in ("telegram_notifier", "feishu_notifier", "pushplus_notifier"):
        monkeypatch.setattr(
            f"pa_agent.notify.{mod}.send_order_signal",
            lambda _m=mod, **_kw: notified.append(_m) or True,
        )
    monkeypatch.setattr(
        "pa_agent.notify.telegram_notifier.send_gate_blocked_signal",
        lambda **_kw: notified.append("gate_blocked") or True,
    )
    return exec_calls, notified


def test_below_stance_threshold_is_not_an_opportunity() -> None:
    settings = Settings()
    settings.general.decision_stance = "conservative"  # floor 55
    pipeline = SignalPipeline(settings)
    assert pipeline.evaluate(_decision(conf=54)["decision"]).opportunity is False
    assert pipeline.evaluate(_decision(conf=55)["decision"]).opportunity is True


def test_user_floor_raisess_threshold_above_stance() -> None:
    settings = Settings()
    settings.general.decision_stance = "extreme_aggressive"  # floor 25
    settings.general.decision_confidence_threshold = 70
    pipeline = SignalPipeline(settings)
    assert pipeline.confidence_threshold() == 70
    assert pipeline.evaluate(_decision(conf=69)["decision"]).opportunity is False
    assert pipeline.evaluate(_decision(conf=70)["decision"]).opportunity is True


def test_dispatch_below_confidence_marks_skipped(monkeypatch) -> None:
    """有计划但被置信度门槛刷掉, 记录必须能回答"为什么没下"。

    回归 2026-09-20 ZECUSDT: 02:01 出限价空单(trade_confidence=52 < 保守档 55)
    未提交, execution 键缺失 → 与"本来就没计划"的 47 轮无法区分。
    """
    settings = Settings()
    settings.general.decision_stance = "conservative"  # floor 55
    exec_calls, notified = _patch_io(monkeypatch)
    pipeline = SignalPipeline(settings)
    decision = _decision(conf=52)
    result = pipeline.dispatch(_signal(decision, frame=_frame()))
    assert result.opportunity is False
    assert result.skipped is True
    assert exec_calls == [] and notified == []
    assert decision["execution"] == {
        "status": "skipped",
        "gate_reasons": ["confidence_below_threshold: 52 < 55"],
    }


def test_dispatch_plain_wait_leaves_no_execution_marker(monkeypatch) -> None:
    """本来就不下单的轮次没有可提交的方案, 不写 execution 标记。"""
    settings = Settings()
    settings.general.decision_stance = "conservative"
    _patch_io(monkeypatch)
    pipeline = SignalPipeline(settings)
    decision = _decision(order_type="不下单", conf=0)
    result = pipeline.dispatch(_signal(decision, frame=_frame()))
    assert result.opportunity is False
    assert result.skipped is False
    assert "execution" not in decision


def test_dispatch_injects_atr_pct_from_frame(monkeypatch) -> None:
    settings = Settings()
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(settings)
    result = pipeline.dispatch(_signal(_decision(), frame=_frame(atr=1.5, close=100.0)))
    assert result.opportunity is True
    assert exec_calls and exec_calls[0]["inner"]["atr_pct"] == 1.5
    assert exec_calls[0]["analysis_symbol"] == "BTCUSDT"


def test_gate_on_mode_blocks_execution_and_counts(monkeypatch) -> None:
    settings = Settings()
    settings.binance_usdm_testnet.direction_gates_mode = "on"
    exec_calls, notified = _patch_io(monkeypatch)
    hits: list[str] = []
    monkeypatch.setattr(
        "pa_agent.util.price_tick.infer_price_tick_from_frame", lambda _f: 0.01
    )
    monkeypatch.setattr(
        "pa_agent.trading.direction_gates.evaluate_direction_gates",
        lambda **_kw: ["G1: 逆势", "G2: 中性带"],
    )
    pipeline = SignalPipeline(settings, on_gate_hit=hits.append)
    decision = _decision()
    result = pipeline.dispatch(_signal(decision, frame=_frame()))
    assert result.gate_blocked is True
    # 不下单, 但必须通知(否则操作者分不清"没机会"与"被拦下")
    assert exec_calls == [] and notified == ["gate_blocked"]
    assert hits and "G1" in hits[0] and "G2" in hits[0]
    # 记录里留下"未提交"印记, 供连续性守卫与事后复盘
    marker = decision["execution"]
    assert marker["status"] == "gate_blocked"
    assert marker["gate_reasons"] == ["G1: 逆势", "G2: 中性带"]


def test_gate_dry_run_logs_but_passes(monkeypatch) -> None:
    settings = Settings()
    settings.binance_usdm_testnet.direction_gates_mode = "dry_run"
    exec_calls, notified = _patch_io(monkeypatch)
    monkeypatch.setattr(
        "pa_agent.util.price_tick.infer_price_tick_from_frame", lambda _f: 0.01
    )
    monkeypatch.setattr(
        "pa_agent.trading.direction_gates.evaluate_direction_gates",
        lambda **_kw: ["G1: 逆势"],
    )
    pipeline = SignalPipeline(settings, on_gate_hit=lambda _k: None)
    result = pipeline.dispatch(_signal(_decision()))
    assert result.gate_blocked is False
    assert exec_calls and notified


def test_rejected_execution_suppresses_all_channels(monkeypatch) -> None:
    settings = Settings()
    _exec_calls, notified = _patch_io(monkeypatch, exec_status="rejected")
    pipeline = SignalPipeline(settings)
    result = pipeline.dispatch(_signal(_decision()))
    assert result.exec_result is not None and result.exec_result.status == "rejected"
    assert notified == []


def test_failed_execution_pushes_channels_and_keeps_result(monkeypatch) -> None:
    settings = Settings()
    _exec_calls, notified = _patch_io(monkeypatch, exec_status="failed")
    failures: list[str] = []
    monkeypatch.setattr(
        "pa_agent.notify.telegram_notifier.send_execution_failure",
        lambda **_kw: failures.append("telegram") or True,
    )
    pipeline = SignalPipeline(settings)
    result = pipeline.dispatch(_signal(_decision()))
    assert result.exec_result is not None and result.exec_result.status == "failed"
    assert sorted(notified) == ["feishu_notifier", "pushplus_notifier", "telegram_notifier"]
    assert failures == ["telegram"]


def test_signal_notification_allowed_only_suppresses_rejected() -> None:
    assert signal_notification_allowed(None) is True
    for ok in ("submitted", "pending", "dry_run", "skipped", "failed"):
        assert signal_notification_allowed(_Res(ok)) is True, ok
    assert signal_notification_allowed(_Res("rejected")) is False


def test_frame_atr_pct_handles_warmup_and_bad_input() -> None:
    assert frame_atr_pct(_frame(atr=1.5, close=100.0)) == 1.5
    nan_frame = type(
        "Frame",
        (),
        {
            "bars": (type("Bar", (), {"close": 100})(),),
            "indicators": type("Ind", (), {"atr14": (float("nan"),)})(),
        },
    )()
    assert frame_atr_pct(nan_frame) is None
    assert frame_atr_pct(type("Frame", (), {})()) is None


def test_gate_off_skips_gate_evaluation_entirely(monkeypatch, tmp_path: Path) -> None:
    settings = Settings()
    called = {"gates": 0}

    def _fail(**_kw):
        called["gates"] += 1
        raise AssertionError("mode=off 时不得评估方向闸门")

    monkeypatch.setattr(
        "pa_agent.trading.direction_gates.evaluate_direction_gates", _fail
    )
    pipeline = SignalPipeline(settings)
    assert pipeline.evaluate(_decision(), frame=_frame()).gate_mode == "off"
    assert called["gates"] == 0


# ── 入口环境门控 (block_*_entry) ────────────────────────────────────────────

def _env_signal(direction: str, cycle: str, diag: str, conf: int = 90) -> OrderSignal:
    decision = {
        "decision": {
            "order_type": "市价单",
            "order_direction": direction,
            "trade_confidence": conf,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 110,
        },
        "diagnosis_summary": {"cycle_position": cycle, "direction": diag},
    }
    return OrderSignal(decision=decision, inner=decision["decision"], symbol="BTCUSDT")


def _env_settings(**flags: bool) -> Settings:
    settings = Settings()
    settings.general.decision_stance = "balanced"  # floor 40, conf=90 必过
    cfg = settings.binance_usdm_testnet
    for key, value in flags.items():
        setattr(cfg, key, value)
    return settings


def test_all_entry_gates_off_by_default(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings())
    result = pipeline.dispatch(_env_signal("做空", "trending_tr", "neutral"))
    assert result.gate_blocked is False
    assert len(exec_calls) == 1


def test_block_short_entry(monkeypatch) -> None:
    """文案必须引权威样本口径, 不得再引已作废的"胜率 16%"。

    16% 出自未修正费用模型的 242 笔样本(logs/_variants_exp.txt, 做空 87 笔
    -267.48U), 已被修正后的回放推翻; 权威口径是 T2 样本
    logs/shadow_rows_baseline45.csv(闸门裁定表 §4): 做空 22 笔胜率 32%,
    90%CI [-2.81,+0.32] 跨零。
    """
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_short_entry=True))
    result = pipeline.dispatch(_env_signal("做空", "normal_channel", "bullish"))
    assert result.gate_blocked and "short_block" in result.gate_reasons[0]
    assert "22 笔胜率 32%" in result.gate_reasons[0]
    assert "16%" not in result.gate_reasons[0]
    assert exec_calls == []
    # 做多不受影响
    result2 = pipeline.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    assert not result2.gate_blocked and len(exec_calls) == 1


def test_block_trending_tr_entry(monkeypatch) -> None:
    """trending_tr 保持硬拦: 2026-09-19 45 天矩阵复跑里它是单档收益最大的开关.

    回放证据: 全量成交 245 笔净/风险 -0.122, 单禁 trending_tr 后 146 笔 +0.043;
    该段本身成交 99 笔 -144.56U, 是全量里唯一的净亏大项。原计划的"软化"方案
    (降级为缩放仓位)被这轮回放推翻, 故断言原因文案里必须带上证据数字。
    """
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_trending_tr_entry=True))
    result = pipeline.dispatch(_env_signal("做多", "trending_tr", "bullish"))
    assert result.gate_blocked and "trending_tr_block" in result.gate_reasons[0]
    assert "99 笔 -144.56U" in result.gate_reasons[0]
    assert exec_calls == []


def test_block_cycle_allowlist(monkeypatch) -> None:
    """周期白名单: 非空时只有名单内的 cycle_position 能入场.

    回放 45 天(2026-09-18 修正费用模型): trending_tr 20 笔 -29.58U,
    tight_channel/spike 3 笔 -8.03U; 通道/区间 22 笔 +39.18U。
    """
    exec_calls, _ = _patch_io(monkeypatch)
    allow = ["broad_channel", "normal_channel", "trading_range"]
    pipeline = SignalPipeline(_env_settings(block_cycle_allowlist=allow))
    blocked = pipeline.dispatch(_env_signal("做多", "trending_tr", "bullish"))
    assert blocked.gate_blocked and "cycle_block" in blocked.gate_reasons[0]
    assert exec_calls == []
    allowed = pipeline.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    assert not allowed.gate_blocked and len(exec_calls) == 1


def test_cycle_allowlist_empty_is_noop(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_cycle_allowlist=[]))
    result = pipeline.dispatch(_env_signal("做多", "trending_tr", "bullish"))
    assert not result.gate_blocked and len(exec_calls) == 1


def test_cycle_allowlist_missing_cycle_is_allowed(monkeypatch) -> None:
    """诊断没给周期位置时不拦(否则白名单会把所有缺字段的信号一起杀掉)."""
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_cycle_allowlist=["broad_channel"]))
    result = pipeline.dispatch(_env_signal("做多", "", "bullish"))
    assert not result.gate_blocked and len(exec_calls) == 1


def test_block_neutral_diag_entry(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_neutral_diag_entry=True))
    result = pipeline.dispatch(_env_signal("做多", "trading_range", "neutral"))
    assert result.gate_blocked and "neutral_diag_block" in result.gate_reasons[0]
    assert exec_calls == []


def test_session_window_is_utc_defined() -> None:
    """窗口用 UTC 定义: 20:00-03:59 UTC 等价原 04:00-11:59 UTC+8 的墙钟时间。"""
    from pa_agent.trading.signal_pipeline import _session_blocked_now

    # 窗口内, 跨零点两侧
    assert _session_blocked_now(now_hour=20)
    assert _session_blocked_now(now_hour=23)
    assert _session_blocked_now(now_hour=0)
    assert _session_blocked_now(now_hour=3)
    # 窗口外
    assert not _session_blocked_now(now_hour=4)
    assert not _session_blocked_now(now_hour=19)
    assert not _session_blocked_now(now_hour=12)
    # 小时数归一: 负数 / 大于 24
    assert _session_blocked_now(now_hour=24) and _session_blocked_now(now_hour=-1)
    # 自定义不跨零点的窗口
    assert _session_blocked_now(now_hour=9, start_hour_utc=8, end_hour_utc=12)
    assert not _session_blocked_now(now_hour=13, start_hour_utc=8, end_hour_utc=12)
    # start == end 视为空窗口, 不会整天误拦
    assert not _session_blocked_now(now_hour=9, start_hour_utc=8, end_hour_utc=8)


def test_block_session_entry(monkeypatch) -> None:
    from pa_agent.trading.signal_pipeline import _session_blocked_now

    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(block_session_entry=True))
    result = pipeline.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    if _session_blocked_now():  # 实际运行时刻落在窗口内才拦截
        assert result.gate_blocked and "session_block" in result.gate_reasons[0]
        assert exec_calls == []
    else:
        assert not result.gate_blocked


def test_block_low_plan_reward_risk(monkeypatch) -> None:
    """RR 门槛: 计划盈亏比低于门槛直接拦.

    回放 45 天: RR>=1.5 保留 36% 成交, 净亏 -138.40U -> -20.74U.
    _env_signal 的价格是 entry=100 / stop=95 / tp=110 -> 盈亏比 2.0.
    """
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(min_plan_reward_risk=2.5))
    result = pipeline.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    assert result.gate_blocked and "rr_block" in result.gate_reasons[0]
    assert exec_calls == []
    # 门槛低于实际盈亏比 -> 放行
    pipeline2 = SignalPipeline(_env_settings(min_plan_reward_risk=1.5))
    result2 = pipeline2.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    assert not result2.gate_blocked
    assert len(exec_calls) == 1


def test_plan_reward_risk_gate_off_by_default(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings())
    result = pipeline.dispatch(_env_signal("做多", "normal_channel", "bullish"))
    assert not result.gate_blocked
    assert len(exec_calls) == 1


def test_block_uncomputable_reward_risk(monkeypatch) -> None:
    """算不出盈亏比(价格缺失/方向矛盾)时, 门槛开启即按不合格拦."""
    exec_calls, _ = _patch_io(monkeypatch)
    pipeline = SignalPipeline(_env_settings(min_plan_reward_risk=1.5))
    signal = _env_signal("做多", "normal_channel", "bullish")
    signal.inner["stop_loss_price"] = None
    result = pipeline.dispatch(signal)
    assert result.gate_blocked and "rr_block" in result.gate_reasons[0]
    assert exec_calls == []


def test_entry_gate_block_notifies_and_marks(monkeypatch) -> None:
    """入口门控命中: 不下单, 但通知 + 记录标记必须都在。

    回归 2026-09-13: short_block 连拦 6 条限价单, 无推送无痕迹, 事后无法解释
    "为什么没下单"; 记录也没标记, 连续性守卫一直等那张不存在的挂单。
    """
    exec_calls, notified = _patch_io(monkeypatch)
    signal = _env_signal("做空", "normal_channel", "bearish")
    pipeline = SignalPipeline(_env_settings(block_short_entry=True))
    result = pipeline.dispatch(signal)

    assert result.gate_blocked is True
    assert result.notified is True
    assert exec_calls == []
    assert notified == ["gate_blocked"]
    assert signal.decision["execution"]["status"] == "gate_blocked"
    assert "short_block" in signal.decision["execution"]["gate_reasons"][0]


def test_executed_signal_marks_submitted(monkeypatch) -> None:
    _patch_io(monkeypatch, exec_status="submitted")
    signal = _signal(_decision())
    SignalPipeline(Settings()).dispatch(signal)
    assert signal.decision["execution"]["status"] == "submitted"


def test_rejected_signal_marks_not_working(monkeypatch) -> None:
    """被拒执行同样没有在场挂单, 标记必须能被连续性守卫读出来。"""
    from pa_agent.records.execution_marker import plan_is_working

    _patch_io(monkeypatch, exec_status="rejected")
    signal = _signal(_decision())
    SignalPipeline(Settings()).dispatch(signal)
    assert signal.decision["execution"]["status"] == "rejected"
    assert plan_is_working({"stage2_decision": signal.decision}) is False


# ── TP2 盈亏比上限 (2026-09-19) ──────────────────────────────────────────────
# 无上限时 TP2 中位 3.28R / 最高 21.37R, 实际从没兑现, 都落到 12h time-stop。
# TP1 上限靠"推远止损"; TP2 反过来"拉近目标", 已执行的风险口径完全不动。

def _tp2_signal() -> OrderSignal:
    decision = {
        "decision": {
            "order_type": "市价单",
            "order_direction": "做多",
            "trade_confidence": 90,
            "entry_price": 100,
            "stop_loss_price": 95,
            "take_profit_price": 106,
            "take_profit_price_2": 130,  # RR 6.0 > 3.0
        }
    }
    return OrderSignal(
        decision=decision,
        inner=decision["decision"],
        symbol="BTCUSDT",
        timeframe="15m",
        frame=_frame(),
    )


def test_tp2_cap_off_by_default(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    signal = _tp2_signal()
    pipeline = SignalPipeline(_env_settings())
    pipeline.dispatch(signal)
    assert signal.inner["take_profit_price_2"] == 130  # 默认不改行为
    assert exec_calls[0]["inner"]["take_profit_price_2"] == 130


def test_tp2_cap_pulls_far_target_before_save_and_execution(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    signal = _tp2_signal()
    pipeline = SignalPipeline(_env_settings(max_plan_reward_risk_tp2=3.0))
    pipeline.dispatch(signal)
    # risk=5 -> 100 + 3*5 = 115, tick 0.1 -> floor 115.0
    assert signal.inner["take_profit_price_2"] == 115.0
    assert exec_calls[0]["inner"]["take_profit_price_2"] == 115.0
    # 风险端与 TP1 一律不动
    assert signal.inner["stop_loss_price"] == 95
    assert signal.inner["take_profit_price"] == 106
    assert signal.inner["entry_price"] == 100


def test_tp2_cap_ignores_non_actionable_order_type(monkeypatch) -> None:
    exec_calls, _ = _patch_io(monkeypatch)
    signal = _tp2_signal()
    signal.inner["order_type"] = "不下单"
    pipeline = SignalPipeline(_env_settings(max_plan_reward_risk_tp2=3.0))
    pipeline.dispatch(signal)
    assert signal.inner["take_profit_price_2"] == 130
    assert exec_calls == []
