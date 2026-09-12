# ruff: noqa: RUF001, RUF002, RUF003
"""统一的"下单机会评估 → 落盘 → 执行 → 通知"管线。

GUI（分析完成回调）与 monitor（bar-close 轮询）曾经各持一份已分叉的副本：
monitor 路径会注入分析棒 atr_pct、把计划止损抬到执行层 ATR 动态下限、跑方向
闸门、拒单后静默信号推送；GUI 路径缺全部这些安全步骤，还用
``settings.general.last_symbol``（而非被分析品种）作为 execution 的
``analysis_symbol``。现在双方都通过 :meth:`SignalPipeline.dispatch` 走同一条
管线，机会判定、门控、落盘、执行、通知策略只活在这一处。

线程归属也收进 module：GUI 调 :meth:`SignalPipeline.dispatch_async`（后台线程
+ 可选回调），monitor 在自己的工作线程里同步调 :meth:`SignalPipeline.dispatch`。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 视为"下单机会"的 order_type 集合（与 stage2 输出契约一致）。
ORDER_OPPORTUNITY_TYPES = frozenset({"限价单", "突破单", "市价单"})


def frame_atr_pct(frame: Any) -> float | None:
    """Latest analyzed-bar ATR14 as a percent of its close; None when unavailable.

    ATR is already computed on the analysis frame (IndicatorBundle.atr14,
    newest-first); this helper only converts it to a percentage so the executor
    can apply the dynamic stop-distance floor without any extra API request.
    """
    indicators = getattr(frame, "indicators", None)
    if indicators is None:
        return None
    atr14 = tuple(getattr(indicators, "atr14", ()) or ())
    bars = tuple(getattr(frame, "bars", ()) or ())
    if not atr14 or not bars:
        return None
    try:
        atr = float(atr14[0])
        close = float(bars[0].close)
    except (TypeError, ValueError, IndexError):
        return None
    if atr != atr or close <= 0:  # NaN during ATR warm-up
        return None
    return atr / close * 100.0


def signal_notification_allowed(exec_result: Any) -> bool:
    """Whether the order-signal push should still be sent after execution.

    A rejected execution (stop too close, whitelist, trader equation, duplicate
    position, ...) means no order is working, so announcing the signal as if it
    were live is misleading. Every other status - submitted, pending, dry_run,
    skipped (disabled/cooldown) and failed - keeps the notification; dry-run
    and disabled still feed the human pipeline and failures carry their own
    alert.
    """
    return exec_result is None or getattr(exec_result, "status", "") != "rejected"


@dataclass(frozen=True)
class OrderSignal:
    """一次下单机会的完整载荷（跨 seam 的类型化契约）。"""

    decision: dict  # 完整 stage2 载荷（含 next_cycle_prediction 等）
    inner: dict  # stage2 内层 decision（执行面；dispatch 会原地注入 atr_pct/抬升止损）
    symbol: str  # 必须来自被分析的 frame，不得来自 UI 状态（如 last_symbol）
    timeframe: str = ""
    frame: Any = None
    stage1_diagnosis: dict | None = None
    previous_record: Any = None  # 方向闸门需要上一条记录
    decision_stance: str = ""
    model_name: str = ""


@dataclass(frozen=True)
class Evaluation:
    """dispatch 前的纯判定结果（无副作用，供 UI 在主线程问询）。"""

    opportunity: bool
    gate_reasons: tuple[str, ...] = ()
    gate_mode: str = "off"  # off / dry_run / on


@dataclass
class DispatchResult:
    opportunity: bool = False
    gate_blocked: bool = False
    gate_reasons: tuple[str, ...] = field(default_factory=tuple)
    exec_result: Any = None
    notified: bool = False


def _session_blocked_now(now_hour: int | None = None) -> bool:
    """04:00-11:59 UTC+8 低流动性窗口(与回放分桶口径一致)."""
    if now_hour is None:
        from datetime import datetime, timedelta, timezone

        now_hour = datetime.now(timezone(timedelta(hours=8))).hour
    return 4 <= now_hour < 12


class SignalPipeline:
    """下单机会的单一入口：评估、门控、落盘、执行、通知。"""

    def __init__(
        self,
        settings: Any,
        *,
        on_gate_hit: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        # 方向闸门命中即回调（含 dry-run），monitor 用它维护统计。
        self._on_gate_hit = on_gate_hit

    # ── 配置 ────────────────────────────────────────────────────────────

    @property
    def _binance_cfg(self) -> Any:
        from pa_agent.trading.binance_env import active_cfg

        return active_cfg(self._settings)

    def confidence_threshold(self) -> int:
        """执行端置信度门槛 = max(stance 档位门槛, general 里用户另设的下限)。

        stance 档位门槛与提示词许可区间对齐（见 decision_stance 模块）；
        ``general.decision_confidence_threshold`` 允许运营再抬高底线。
        """
        general = getattr(self._settings, "general", None)
        from pa_agent.ai.decision_stance import confidence_threshold_for_stance

        stance_floor = confidence_threshold_for_stance(
            str(getattr(general, "decision_stance", "") or "")
        )
        user_floor = int(getattr(general, "decision_confidence_threshold", 0) or 0)
        return max(stance_floor, user_floor)

    def has_order_opportunity(self, inner: Any) -> bool:
        if not isinstance(inner, dict):
            return False
        if str(inner.get("order_type") or "") not in ORDER_OPPORTUNITY_TYPES:
            return False
        try:
            confidence = int(float(str(inner.get("trade_confidence") or "")))
        except (TypeError, ValueError):
            return False
        return confidence >= self.confidence_threshold()

    # ── 评估（纯判定，无副作用，UI 可在主线程调用） ─────────────────────

    def evaluate(
        self,
        inner: Any,
        *,
        frame: Any = None,
        previous_record: Any = None,
        decision: dict | None = None,
    ) -> Evaluation:
        if not self.has_order_opportunity(inner):
            return Evaluation(opportunity=False)
        env_reasons = self._entry_block_reasons(inner, decision=decision)
        if env_reasons:
            cfg = self._binance_cfg
            gates_key = ",".join(sorted(r.split(":", 1)[0] for r in env_reasons))
            if self._on_gate_hit is not None:
                try:
                    self._on_gate_hit(gates_key)
                except Exception:
                    logger.debug("entry-gate stats callback failed", exc_info=True)
            logger.warning(
                "[入口门控] %s 拦截 %s: %s",
                self._frame_label(frame), gates_key, "; ".join(env_reasons),
            )
            return Evaluation(opportunity=True, gate_reasons=env_reasons, gate_mode="on")
        reasons = self._direction_gate_reasons(
            inner, frame=frame, previous_record=previous_record
        )
        if not reasons:
            return Evaluation(opportunity=True)
        cfg = self._binance_cfg
        mode = str(getattr(cfg, "direction_gates_mode", "off") or "off").strip()
        gates_key = ",".join(sorted({str(r).split(":", 1)[0] for r in reasons}))
        if self._on_gate_hit is not None:
            try:
                self._on_gate_hit(gates_key)
            except Exception:
                logger.debug("direction-gate stats callback failed", exc_info=True)
        label = self._frame_label(frame)
        detail = "; ".join(reasons)
        if mode == "dry_run":
            logger.info(
                "[方向闸门 dry-run] %s 命中 %s: %s", label, gates_key, detail
            )
            return Evaluation(opportunity=True, gate_reasons=reasons, gate_mode="dry_run")
        logger.warning("[方向闸门] %s 拦截 %s: %s", label, gates_key, detail)
        return Evaluation(opportunity=True, gate_reasons=reasons, gate_mode="on")

    def _frame_label(self, frame: Any) -> str:
        return f"{getattr(frame, 'symbol', '?')} {getattr(frame, 'timeframe', '?')}"

    def _entry_block_reasons(self, inner: dict, *, decision: dict | None = None) -> tuple[str, ...]:
        """入口环境门控（与方向闸门独立, 不受 direction_gates_mode 影响）。

        配置默认全关; 开关组合来自 2026-09-13 的 45 天影子回放变体验证,
        见 settings.py block_*_entry 注释与 tools/shadow_replay.py。
        """
        cfg = self._binance_cfg
        reasons: list[str] = []
        direction = str(inner.get("order_direction") or "")
        if getattr(cfg, "block_short_entry", False) and "空" in direction:
            reasons.append("short_block: 回放 45 天空单胜率 16%")
        diag = {}
        if isinstance(decision, dict):
            diag = decision.get("diagnosis_summary") or {}
            if not isinstance(diag, dict):
                diag = {}
        cycle = str(diag.get("cycle_position") or "")
        if getattr(cfg, "block_trending_tr_entry", False) and cycle == "trending_tr":
            reasons.append("trending_tr_block: 回放 118 笔净/风险 -0.55")
        if getattr(cfg, "block_neutral_diag_entry", False) and str(diag.get("direction") or "") == "neutral":
            reasons.append("neutral_diag_block: 实盘 neutral 单胜率 25%")
        if getattr(cfg, "block_session_entry", False) and _session_blocked_now():
            reasons.append("session_block: 回放 04-12时(UTC+8) 74 笔 -290U")
        return tuple(reasons)

    def _direction_gate_reasons(
        self, inner: dict, *, frame: Any, previous_record: Any
    ) -> tuple[str, ...]:
        cfg = self._binance_cfg
        mode = str(getattr(cfg, "direction_gates_mode", "off") or "off").strip()
        if mode == "off" or frame is None:
            return ()
        try:
            from pa_agent.trading.direction_gates import evaluate_direction_gates
            from pa_agent.util.price_tick import infer_price_tick_from_frame

            tick = infer_price_tick_from_frame(frame)
            if tick is None:
                return ()
            reasons = evaluate_direction_gates(
                decision=inner,
                bars=getattr(frame, "bars", None),
                tick=tick,
                previous_record=previous_record,
            )
        except Exception as exc:
            logger.warning(
                "Direction-gate evaluation failed for %s: %s", self._frame_label(frame), exc
            )
            return ()
        return tuple(reasons or ())

    # ── 执行（评估 + 落盘 + 下单 + 通知） ────────────────────────────────

    def dispatch(self, signal: OrderSignal) -> DispatchResult:
        evaluation = self.evaluate(
            signal.inner,
            frame=signal.frame,
            previous_record=signal.previous_record,
            decision=signal.decision,
        )
        if not evaluation.opportunity:
            return DispatchResult(opportunity=False)
        if evaluation.gate_mode == "on":
            return DispatchResult(
                opportunity=True,
                gate_blocked=True,
                gate_reasons=evaluation.gate_reasons,
            )

        self._prepare_execution_prices(signal)
        self._save_record(signal)
        result = self._execute(signal)
        notified = self._notify_signal(signal, result)
        return DispatchResult(
            opportunity=True,
            gate_reasons=evaluation.gate_reasons,
            exec_result=result,
            notified=notified,
        )

    def dispatch_async(
        self, signal: OrderSignal, *, on_done: Callable[[DispatchResult], None] | None = None
    ) -> threading.Thread:
        """后台线程执行 dispatch；GUI 用，避免秒级执行阻塞事件循环。"""

        def _run() -> None:
            try:
                result = self.dispatch(signal)
            except Exception:  # 执行绝不影响分析/记录/通知
                logger.exception("Signal pipeline dispatch failed")
                return
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:
                    logger.exception("dispatch on_done callback failed")

        thread = threading.Thread(target=_run, name="signal-pipeline-dispatch", daemon=True)
        thread.start()
        return thread

    def _prepare_execution_prices(self, signal: OrderSignal) -> None:
        """注入分析棒 atr_pct，并把计划止损抬到执行层 ATR 动态下限。

        Plan B（止损距离下限前移）：结构止损低于执行层 ATR 动态下限时，在落盘前
        抬升止损到 tick 对齐下限，消除"计划已记录、执行被 Stop loss too close
        to entry 拒绝"的断层。仅真实执行（非 dry-run/停用）时介入。
        """
        cfg = self._binance_cfg
        atr_pct = frame_atr_pct(signal.frame)
        if atr_pct is not None:
            signal.inner["atr_pct"] = atr_pct
        if (
            atr_pct is None
            or str(signal.inner.get("order_type") or "") not in ("限价单", "市价单")
            or not getattr(cfg, "enabled", False)
            or getattr(cfg, "dry_run", False)
            or getattr(cfg, "emergency_stop", False)
        ):
            return
        try:
            from pa_agent.trading.binance_usdm_testnet import (
                lift_stop_to_min_distance_floor,
            )
            from pa_agent.util.price_tick import infer_price_tick_from_frame

            old_stop = signal.inner.get("stop_loss_price")
            if lift_stop_to_min_distance_floor(
                signal.inner,
                cfg,
                tick=infer_price_tick_from_frame(signal.frame),
            ):
                logger.info(
                    "计划止损抬升至 ATR 动态下限 %s %s: %s -> %s",
                    signal.symbol,
                    signal.timeframe,
                    old_stop,
                    signal.inner["stop_loss_price"],
                )
        except Exception as exc:
            logger.warning(
                "Stop-floor lift failed for %s %s: %s",
                signal.symbol,
                signal.timeframe,
                exc,
            )

    def _save_record(self, signal: OrderSignal) -> None:
        try:
            from pa_agent.records.trade_logger import save_trade_record

            save_trade_record(
                decision_inner=signal.inner,
                stage2_full=signal.decision,
                stage1_diagnosis=signal.stage1_diagnosis,
                frame=signal.frame,
                meta_symbol=signal.symbol,
                meta_timeframe=signal.timeframe,
                decision_stance=signal.decision_stance,
                model_name=signal.model_name,
                structure_flip_cooldown_bars=int(
                    getattr(
                        getattr(self._settings, "general", None),
                        "structure_flip_cooldown_bars",
                        3,
                    )
                    or 3
                ),
            )
        except Exception as exc:
            logger.warning("Trade record logging failed: %s", exc)

    def _execute(self, signal: OrderSignal) -> Any:
        result = None
        try:
            from pa_agent.trading.binance_env import resolve_env
            from pa_agent.trading.binance_usdm_testnet import execute_market_signal

            result = execute_market_signal(
                signal.inner, self._settings, analysis_symbol=signal.symbol
            )
            env_label = (
                resolve_env(self._settings).label_zh if self._settings is not None else "测试网"
            )
            logger.info(
                "Binance U本位 %s 自动执行: status=%s symbol=%s reason=%s",
                env_label,
                result.status,
                result.symbol,
                result.reason,
            )
        except Exception as exc:
            logger.exception("Binance U本位自动执行异常: %s", exc)
        if result is not None and getattr(result, "status", "") == "failed":
            # 失败不能静默：除信号消息外额外告警。
            try:
                from pa_agent.notify.telegram_notifier import send_execution_failure

                send_execution_failure(
                    symbol=signal.symbol,
                    timeframe=signal.timeframe,
                    status=result.status,
                    reason=result.reason,
                    settings=self._settings,
                )
            except Exception:
                logger.exception("Execution-failure notification failed")
        return result

    def _notify_signal(self, signal: OrderSignal, exec_result: Any) -> bool:
        """推送下单信号；被拒执行静默（避免"像有单在跑"的误导）。"""
        if not signal_notification_allowed(exec_result):
            logger.info(
                "跳过被拒信号的推送 %s %s (status=%s reason=%s)",
                signal.symbol,
                signal.timeframe,
                getattr(exec_result, "status", "?"),
                getattr(exec_result, "reason", ""),
            )
            return False
        from pa_agent.notify.dispatcher import send_order_signal_all
        from pa_agent.records.trade_logger import latest_chart_image

        outcomes = send_order_signal_all(
            decision_inner=signal.inner,
            stage2_full=signal.decision,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            settings=self._settings,
            chart_image_path=latest_chart_image(signal.symbol, signal.timeframe),
        )
        logger.info(
            "Order-signal notification outcomes for %s %s: %s",
            signal.symbol,
            signal.timeframe,
            outcomes,
        )
        return any(outcomes.values())
