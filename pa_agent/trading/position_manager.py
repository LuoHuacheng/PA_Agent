"""持仓生命周期管理: 四条看护循环 + 止损补挂/桥接/swap + 重启恢复。

从 binance_usdm_testnet 迁出的独立概念: 持仓看护(保本移动/TP1 runner/
时间止损/止损看门狗)只依赖交易所 client 与运行时状态存储, 与"下单执行
管线"分离。本模块单向依赖 binance_usdm_testnet; 执行侧通过 PEP 562
getattr 兜底访问, 不产生模块级循环。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pa_agent.config.settings import BinanceUSDMTestnetSettings, Settings
from pa_agent.trading import binance_env
from pa_agent.trading.binance_usdm_testnet import (
    _STATE_STORE,
    BinanceAPIError,
    BinanceUSDMTestnetClient,
    _decimal_text,
    _drop_guard,
    _guard_enabled,
    _guard_rate_limit_wait,
    _guard_trigger_reached,
    _is_missing_algo_order_error,
    _is_rate_limit_reason,
    _patch_guard,
    _positive_decimal,
    _read_guard,
    _register_guard,
    _stop_would_immediately_trigger,
    configure_binance_environment,
    current_mark_price,
    current_position,
)
from pa_agent.trading.guard_loop import ErrorBudget

logger = logging.getLogger(__name__)

# Per-symbol transition locks: serialise breakeven-stop moves and TP2 swaps
# that run on separate daemon threads for the same symbol (guard vs runner).
_MANAGER_LOCKS: dict[str, threading.Lock] = {}
_MANAGER_LOCKS_GUARD = threading.Lock()


def _manager_lock(symbol: str) -> threading.Lock:
    """Return the transition lock guarding this symbol's SL/TP swaps."""
    with _MANAGER_LOCKS_GUARD:
        lock = _MANAGER_LOCKS.get(symbol)
        if lock is None:
            lock = threading.Lock()
            _MANAGER_LOCKS[symbol] = lock
        return lock


# ---------------------------------------------------------------------------
# 止损单补挂校验 (resting-order verification & re-hang)
# ---------------------------------------------------------------------------
# 背景(2026-09-08 事故): TP1 部分止盈/保本移动阶段, 记录指向的 algo 止损单
# 已被撤/已死(精度 bug 挂新失败后旧单早已撤掉), 程序只信注册表不查实单,
# 持仓裸奔数小时. 以下助手在每次"信任 resting 单"前 GET /fapi/v1/algoOrder
# 校验; 单已死则按候选价补挂并回写注册表, 绝不裸奔.

#: Algo 服务中只有 NEW 表示条件单仍在等待触发(实测: NEW=挂单中, CANCELED=已撤).
_ALGO_LIVE_STATUSES = frozenset({"NEW"})


def _algo_status_live(status: str) -> bool:
    """True 仅当 algo 订单仍在 resting (NEW)。"""
    return status in _ALGO_LIVE_STATUSES


def _rehang_stop_candidates(
    *,
    side: str,
    entry: Decimal,
    stop0: Decimal,
    mark: Decimal,
    stage_tp2: bool,
    floor_pct: float,
) -> list[Decimal]:
    """Ordered protective stop prices to try when the recorded stop is gone.

    TP2/trigger stage prefers the breakeven price (entry); the plain phase
    prefers the original static stop (stop0). Candidates whose trigger is
    already satisfied (-2021 immediate-trigger) are dropped. When nothing is
    placeable, a fresh stop at min_stop_distance_pct outside the mark is
    appended last so a breached-but-unprotected position still gets downside
    protection instead of running naked.
    """
    exit_side = "SELL" if side == "BUY" else "BUY"
    candidates: list[Decimal] = []
    if stage_tp2:
        candidates.extend((entry, stop0))
    else:
        candidates.extend((stop0, entry))
    if floor_pct and floor_pct > 0:
        factor = Decimal(str(floor_pct)) / Decimal("100")
        if side == "BUY":
            candidates.append(mark * (Decimal("1") - factor))
        else:
            candidates.append(mark * (Decimal("1") + factor))
    return [
        price
        for price in candidates
        if price is not None
        and price > 0
        and not _stop_would_immediately_trigger(exit_side, price, mark)
    ]


#: 补挂逐候选尝试时, 仅这类业务拒绝视为"该价位挂不了"换下一候选; 网络/限流等
#: 瞬时错误原样上抛, 由调用方限次重试(不许把止损悄悄降级到更差价位).
_REHANG_SKIP_MARKERS = ('"-1111"', '"-2021"', "-1111", "-2021")


def _stop_resting_alive(client: BinanceUSDMTestnetClient, client_algo_id: str) -> bool:
    """True when the algo stop really rests on the exchange (NEW).

    Terminal/missing states (CANCELED/EXPIRED/triggered/unknown id) return
    False; transient transport/rate-limit errors re-raise for caller retry.
    """
    try:
        payload = client.algo_order_status(client_algo_id=client_algo_id)
        status = payload.get("algoStatus") if isinstance(payload, dict) else None
    except BinanceAPIError as exc:
        if _is_missing_algo_order_error(exc):
            return False
        raise
    return _algo_status_live(str(status or ""))


def _rehang_protective_stop(
    client: BinanceUSDMTestnetClient,
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    stop0: Decimal,
    stage_tp2: bool,
    floor_pct: float,
) -> tuple[bool, str, str]:
    """Place the best placeable replacement STOP for a missing protective stop.

    Tries every candidate from _rehang_stop_candidates in order; business
    rejections (-1111 precision / -2021 immediate) move to the next candidate,
    transient errors raise. Returns (ok, new_stop_algo_id, note).
    """
    exit_side = "SELL" if side == "BUY" else "BUY"
    mark = current_mark_price(client, symbol)
    for price in _rehang_stop_candidates(
        side=side, entry=entry, stop0=stop0, mark=mark,
        stage_tp2=stage_tp2, floor_pct=floor_pct,
    ):
        candidate_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
        try:
            client.place_close_algo_order(
                symbol=symbol,
                side=exit_side,
                order_type="STOP_MARKET",
                stop_price=price,
                client_algo_id=candidate_id,
            )
        except BinanceAPIError as exc:
            message = str(exc)
            if any(marker in message for marker in _REHANG_SKIP_MARKERS):
                logger.warning(
                    "Re-hang candidate %s rejected for %s (%s); trying next",
                    _decimal_text(price),
                    symbol,
                    exc,
                )
                continue
            raise
        return True, candidate_id, f"re-hung protective stop at {_decimal_text(price)}"
    return (
        False,
        "",
        "no placeable re-hang candidate (mark=" + _decimal_text(mark) + ")",
    )


def _ensure_protective_stop(
    client: BinanceUSDMTestnetClient,
    *,
    symbol: str,
    record: dict[str, Any],
    entry: Decimal,
    stage_tp2: bool,
    floor_pct: float,
) -> tuple[str, str, str]:
    """Verify the recorded protective stop really rests; re-hang when gone.

    Returns (status, stop_algo_id, note) with status one of:
      alive  - recorded stop verified resting (NEW); nothing to do
      rehung - recorded stop was gone; replacement placed and registry updated
      error  - transient API failure / no placeable candidate (caller retries)
    The caller must hold the per-symbol manager lock.
    """
    side = str(record.get("side") or "")
    current_stop = str(record.get("stop_algo_id") or "")
    if side not in ("BUY", "SELL") or not current_stop:
        return "error", "", "invalid guard record"
    try:
        alive = _stop_resting_alive(client, current_stop)
    except BinanceAPIError as exc:
        return "error", current_stop, f"algo status query failed: {exc}"
    if alive:
        return "alive", current_stop, ""
    stop0 = _positive_decimal(record.get("stop0"))
    if stop0 is None:
        return "error", current_stop, "guard record missing stop0"
    try:
        ok, new_stop, note = _rehang_protective_stop(
            client,
            symbol=symbol,
            side=side,
            entry=entry,
            stop0=stop0,
            stage_tp2=stage_tp2,
            floor_pct=floor_pct,
        )
    except BinanceAPIError as exc:
        return "error", current_stop, f"re-hang placement failed: {exc}"
    if not ok:
        return "error", current_stop, note
    _patch_guard(symbol, stop_algo_id=new_stop)
    return "rehung", new_stop, note


def _swap_stop_to_price(
    client: BinanceUSDMTestnetClient,
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    live_stop_id: str,
    bridge_qty: Decimal,
) -> tuple[str, str, str]:
    """Replace a resting closePosition STOP with one at entry (breakeven).

    Binance allows only ONE open closePosition order per direction and order
    class (-4130: "An open stop or take profit order with GTE and closePosition
    in the direction is existing"), so a naive place-new-then-cancel-old swap
    is rejected while the old STOP still rests, while a cancel-then-place swap
    leaves the position unprotected between the two calls. This helper bridges
    the move with a reduceOnly+quantity STOP that coexists with a closePosition
    order (same shape the TP1 partial order uses next to the entry STOP):

      1. hang bridge reduceOnly+qty STOP at entry (no -4130, no naked gap)
      2. cancel the old closePosition STOP
      3. hang the canonical closePosition STOP at entry (old gone -> legal)
      4. cancel the bridge

    From step 1 on the position is always protected at entry; any step may
    leave the bridge resting (also at entry), which is strictly better than
    the old stop and never leaves the position naked. A step-1 failure raises
    with nothing changed (old closePosition STOP still protects) so callers can
    keep the original stop and retry later.

    Returns (status, stop_algo_id, note):
      "ok"   - canonical closePosition STOP at entry resting; bridge removed
      "kept" - bridge reduceOnly STOP at entry resting (old closePosition stop
               may also still rest); safe overlap, caller logs and moves on
    Raises BinanceAPIError when nothing changed and the old stop still protects.

    Must be called with the per-symbol manager lock held (callers do).
    """
    if side not in ("BUY", "SELL") or not live_stop_id:
        raise BinanceAPIError("invalid stop swap request")
    if bridge_qty is None or bridge_qty <= 0:
        raise BinanceAPIError(
            "cannot swap stop to entry without a live position amount"
        )
    exit_side = "SELL" if side == "BUY" else "BUY"

    def _hang_cp_stop() -> str:
        stop_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="STOP_MARKET",
            stop_price=entry,
            client_algo_id=stop_id,
        )
        return stop_id

    # 1) bridge: reduceOnly+qty STOP coexists with the old closePosition STOP.
    bridge_id = f"pa-sl-{uuid.uuid4().hex[:24]}"
    try:
        client.place_close_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="STOP_MARKET",
            stop_price=entry,
            client_algo_id=bridge_id,
            quantity=bridge_qty,
            close_position=False,
        )
    except BinanceAPIError:
        # Nothing changed: the old closePosition STOP still protects.
        raise
    # 2) old closePosition STOP is now redundant.
    try:
        client.cancel_algo_order(client_algo_id=live_stop_id)
    except BinanceAPIError as exc:
        if not _is_missing_algo_order_error(exc):
            # Cancel failed: the old stop may still rest. Try the canonical
            # closePosition STOP anyway - it only succeeds when the old one is
            # really gone (-4130 otherwise), so no state is lost either way.
            try:
                canonical_id = _hang_cp_stop()
            except BinanceAPIError as cp_exc:
                return (
                    "kept",
                    bridge_id,
                    f"old stop {live_stop_id} cancel failed ({exc}); bridge STOP "
                    f"at entry kept, canonical placement also failed: {cp_exc}",
                )
            # Old stop really gone: drop the bridge.
            try:
                client.cancel_algo_order(client_algo_id=bridge_id)
            except BinanceAPIError as b_exc:
                if not _is_missing_algo_order_error(b_exc):
                    logger.warning(
                        "Stop swap: bridge %s cancel failed for %s (%s); "
                        "canonical stop %s is in place, bridge may still rest",
                        bridge_id, symbol, b_exc, canonical_id,
                    )
            return "ok", canonical_id, ""
    # 3) old stop gone (cancelled or already missing): hang the canonical stop.
    try:
        canonical_id = _hang_cp_stop()
    except BinanceAPIError as exc:
        return (
            "kept",
            bridge_id,
            f"canonical closePosition STOP placement failed ({exc}); bridge "
            f"STOP at entry kept",
        )
    # 4) bridge no longer needed.
    try:
        client.cancel_algo_order(client_algo_id=bridge_id)
    except BinanceAPIError as exc:
        if not _is_missing_algo_order_error(exc):
            logger.warning(
                "Stop swap: bridge %s cancel failed for %s (%s); canonical stop "
                "%s is in place, bridge may still rest",
                bridge_id, symbol, exc, canonical_id,
            )
    return "ok", canonical_id, ""


def _breakeven_guard_loop(
    client: BinanceUSDMTestnetClient,
    symbol: str,
    trigger: str,
    poll_seconds: float,
    *,
    floor_pct: float = 0.0,
) -> None:
    """Poll an open position; once float profit reaches the trigger, replace
    the resting STOP algo order with one at the entry price (breakeven).
    Exits when the position is closed or the stop has been moved.
    """
    budget = ErrorBudget()
    while True:
        record = _read_guard(symbol)
        if record is None or record.get("moved"):
            return
        side = str(record.get("side") or "")
        stop0 = _positive_decimal(record.get("stop0"))
        target = _positive_decimal(record.get("target"))
        stop_algo_id = str(record.get("stop_algo_id") or "")
        if side not in ("BUY", "SELL") or stop0 is None or target is None or not stop_algo_id:
            return
        try:
            info = current_position(client, symbol)
            amount = info["amount"]
            entry = info["entry"]
            if amount == 0 or entry is None:
                return  # position closed (TP/SL/manual) - nothing to protect
            if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
                return  # not our position anymore
            mark = current_mark_price(client, symbol)
        except BinanceAPIError as exc:
            if _is_rate_limit_reason(str(exc)):
                # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数。
                _guard_rate_limit_wait(poll_seconds)
                continue
            _exhausted = budget.record()
            logger.warning(
                "Breakeven guard poll failed for %s (attempt %d): %s",
                symbol,
                budget.count,
                exc,
            )
            if _exhausted:
                logger.error(
                    "Breakeven guard gave up for %s after repeated API errors; "
                    "original stop stays in place",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if not _guard_trigger_reached(
            mark=mark,
            entry=entry,
            stop0=stop0,
            target=target,
            side=side,
            trigger=trigger,
        ):
            budget.reset()
            time.sleep(poll_seconds)
            continue
        # 移损临界区: 与 TP runner 互斥, 锁内重读注册表防双撤双挂。
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None or fresh.get("moved"):
                return
            live_stop = str(fresh.get("stop_algo_id") or "")
            if not live_stop:
                return
            exit_side = "SELL" if side == "BUY" else "BUY"
            if _stop_would_immediately_trigger(exit_side, entry, mark):
                # 浮盈已回吐(价格回到入场另一侧): 保本 STOP 会立即触发被拒(-2021),
                # 原止损保留; 回到主循环等价格重新满足移损条件, 避免反复失败刷屏。
                logger.info(
                    "Breakeven guard: mark %s already through entry %s for %s; "
                    "keeping original stop %s",
                    _decimal_text(mark),
                    _decimal_text(entry),
                    symbol,
                    live_stop,
                )
                budget.reset()
                time.sleep(poll_seconds)
                continue
            # 交易所只许一个同方向 closePosition STOP resting(-4130), 先挂新后撤旧
            # 会被拒绝; _swap_stop_to_price 用 reduceOnly+qty 桥接单保底换单, 全程
            # 持仓有保护, 无裸奔窗口。桥接挂不上(旧单仍在场)走下方校验/重试。
            try:
                swap_status, new_stop_id, swap_note = _swap_stop_to_price(
                    client,
                    symbol=symbol,
                    side=side,
                    entry=entry,
                    live_stop_id=live_stop,
                    bridge_qty=abs(amount),  # 空单 amount 为负: reduceOnly 数量必须取正
                )
            except BinanceAPIError as exc:
                # 挂保本失败并不代表安全: 若记录指向的旧止损其实已死(历史 bug/
                # 手动撤单/精度错误), "原止损 stays in place" 就是裸奔。先校验
                # 再决定限次重试(旧单活着)还是立即补挂(旧单已死)。
                guard_status, guard_stop, guard_note = _ensure_protective_stop(
                    client,
                    symbol=symbol,
                    record=fresh,
                    entry=entry,
                    stage_tp2=True,
                    floor_pct=floor_pct,
                )
                if guard_status == "error":
                    _exhausted = budget.record()
                    logger.error(
                        "Breakeven stop placement failed for %s (%s) and "
                        "re-hang verification failed: %s",
                        symbol,
                        stop_algo_id,
                        guard_note,
                    )
                    if _exhausted:
                        logger.error(
                            "Breakeven guard gave up for %s after repeated errors; "
                            "record kept for restart resume",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                if guard_status == "rehung":
                    logger.warning(
                        "Breakeven guard: original stop %s for %s was gone; %s",
                        stop_algo_id,
                        symbol,
                        guard_note,
                    )
                    _patch_guard(symbol, moved=True, stop_algo_id=guard_stop)
                    return
                _exhausted = budget.record()
                logger.error(
                    "Breakeven stop placement failed for %s (%s), original stop "
                    "kept resting: %s",
                    symbol,
                    stop_algo_id,
                    exc,
                )
                if _exhausted:
                    logger.error(
                        "Breakeven guard gave up placing breakeven stop for %s after "
                        "repeated errors; original stop stays in place",
                        symbol,
                    )
                    return
                time.sleep(poll_seconds)
                continue
            if swap_status == "kept":
                # 桥接单仍在场(撤旧未遂或正式单被拒): 保本保护已由桥接单落地,
                # 记录指向桥接单即可; 旧 closePosition 单若仍在场由交易所清理。
                logger.warning("Breakeven guard: %s", swap_note)
            _patch_guard(symbol, moved=True, stop_algo_id=new_stop_id)
            logger.info(
                "Breakeven stop moved to entry for %s %s (trigger=%s mark=%s)",
                symbol,
                side,
                trigger,
                _decimal_text(mark),
            )
            return


def _tp_runner_loop(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    poll_seconds: float,
    floor_pct: float = 0.0,
) -> None:
    """Poll a position protected by a partial (reduceOnly) TP1 order.

    The TP1 leg only closes partial_qty, so this thread watches the position
    amount: once it shrinks (TP1 fired) the original SL is cancelled (unless
    the breakeven guard already moved it) and a close-all TAKE_PROFIT at TP2
    is hung so the remainder can run to the far target. When the position is
    fully closed the resting partial TP1 is cancelled and the record removed.
    """
    budget = ErrorBudget()
    while True:
        record = _read_guard(symbol)
        if record is None or record.get("partial_done"):
            return
        side = str(record.get("side") or "")
        qty = _positive_decimal(record.get("qty"))
        partial_qty = _positive_decimal(record.get("partial_qty"))
        stop_algo_id = str(record.get("stop_algo_id") or "")
        tp_algo_id = str(record.get("tp_algo_id") or "")
        target2 = _positive_decimal(record.get("target2"))
        if (
            side not in ("BUY", "SELL")
            or qty is None
            or partial_qty is None
            or target2 is None
            or not stop_algo_id
            or not tp_algo_id
        ):
            return  # 旧 guard 记录(无 partial 字段)或残缺记录: 不归 runner 管
        try:
            info = current_position(client, symbol)
            amount = info["amount"]
            entry = info["entry"]
        except BinanceAPIError as exc:
            if _is_rate_limit_reason(str(exc)):
                # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数。
                _guard_rate_limit_wait(poll_seconds)
                continue
            _exhausted = budget.record()
            logger.warning(
                "TP runner poll failed for %s (attempt %d): %s",
                symbol,
                budget.count,
                exc,
            )
            if _exhausted:
                logger.error(
                    "TP runner gave up for %s after repeated API errors; "
                    "partial TP1 stays in place",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
            return  # not our position anymore
        if amount == 0:
            # 仓位已清: 撤掉可能残留的 TP1 部分单后移除记录。
            try:
                client.cancel_algo_order(client_algo_id=tp_algo_id)
            except BinanceAPIError as exc:
                if _is_missing_algo_order_error(exc):
                    logger.warning(
                        "TP runner: partial TP1 %s for %s already gone (%s)",
                        tp_algo_id,
                        symbol,
                        exc,
                    )
                else:
                    _exhausted = budget.record()
                    logger.error(
                        "TP runner cleanup failed for %s (partial TP1 %s): %s",
                        symbol,
                        tp_algo_id,
                        exc,
                    )
                    if _exhausted:
                        logger.error(
                            "TP runner gave up cleaning %s after repeated errors; "
                            "record kept so a restart resume can retry the cancel",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
            _drop_guard(symbol)
            logger.info("TP runner: %s position closed; cleared partial TP1 %s",
                symbol, tp_algo_id,
            )
            return
        if entry is None:
            return  # 无法取得入场价, 保本价格无从谈起
        # positionRisk 返回带符号 positionAmt(空单为负), qty 记的是正数量:
        # 必须比绝对值, 否则空单永远不等, 首轮就误判 TP1 已半平(2026-09-10 事故)。
        if abs(amount) == qty:
            budget.reset()
            time.sleep(poll_seconds)
            continue
        # TP1 半仓已触发(|amount| < qty): 进入 TP2 阶段。
        # 临界区: 与保本 guard 的移损互斥(per-symbol 锁), 锁内重读注册表, 防双撤双挂。
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None or fresh.get("partial_done"):
                return
            moved = bool(fresh.get("moved"))
            current_stop = str(fresh.get("stop_algo_id") or "")
            current_tp = str(fresh.get("tp_algo_id") or "")
            if not current_stop or not current_tp:
                return
            exit_side = "SELL" if side == "BUY" else "BUY"
            try:
                mark = current_mark_price(client, symbol)
            except BinanceAPIError:
                mark = None  # 预检尽力而为: 拿不到 mark 则保持原行为
            # 先清可能仍 resting 的 TP1 部分单(已成交时 -2011/-2013 视为已清):
            # 手动减仓等场景不能让孤儿部分单对新仓位生效。
            try:
                client.cancel_algo_order(client_algo_id=current_tp)
            except BinanceAPIError as exc:
                if _is_missing_algo_order_error(exc):
                    logger.warning(
                        "TP runner: partial TP1 %s for %s already gone (%s)",
                        current_tp,
                        symbol,
                        exc,
                    )
                else:
                    _exhausted = budget.record()
                    logger.error(
                        "TP runner partial TP1 cancel failed for %s (%s): %s",
                        symbol,
                        current_tp,
                        exc,
                    )
                    if _exhausted:
                        logger.error(
                            "TP runner gave up clearing TP1 for %s after repeated "
                            "errors; original stop stays in place",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
            if moved or (mark is not None and _stop_would_immediately_trigger(
                exit_side, entry, mark
            )):
                # 保本不再重挂(guard 已移 / mark 已回吐穿 entry)。进 TP2 前先
                # 校验记录指向的止损单真实 resting: 2026-09-08 事故中记录仍指向
                # 早已撤掉的单, 程序却"保留原止损"裸奔数小时 → 已死必须补挂。
                if not moved:
                    logger.info(
                        "TP runner: mark %s already through entry %s for %s; "
                        "verifying protective stop %s",
                        _decimal_text(mark),
                        _decimal_text(entry),
                        symbol,
                        current_stop,
                    )
                    moved = True
                    _patch_guard(symbol, moved=True)
                runner_status, runner_stop, runner_note = _ensure_protective_stop(
                    client,
                    symbol=symbol,
                    record=fresh,
                    entry=entry,
                    stage_tp2=True,
                    floor_pct=floor_pct,
                )
                if runner_status == "error":
                    _exhausted = budget.record()
                    logger.error(
                        "TP runner: protective-stop verification failed for %s "
                        "(%s): %s",
                        symbol,
                        current_stop,
                        runner_note,
                    )
                    if _exhausted:
                        logger.error(
                            "TP runner gave up verifying stop for %s after repeated "
                            "errors; record kept for restart resume",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                if runner_status == "rehung":
                    logger.warning(
                        "TP runner: recorded stop %s for %s was gone; %s",
                        current_stop,
                        symbol,
                        runner_note,
                    )
                new_stop_id = runner_stop
            else:
                # C1: 同方向只许一个 closePosition STOP resting(-4130), 保本移动
                # 不能先挂新后撤旧。挂新前先校验旧单真实 resting: 旧单已死时直接
                # 补挂(entry 优先); 活着则经 reduceOnly+qty 桥接单换至 entry, 全程
                # 持仓有保护, 无裸奔窗口。
                runner_status, runner_stop, runner_note = _ensure_protective_stop(
                    client,
                    symbol=symbol,
                    record=fresh,
                    entry=entry,
                    stage_tp2=True,
                    floor_pct=floor_pct,
                )
                if runner_status == "error":
                    _exhausted = budget.record()
                    logger.error(
                        "TP runner: protective-stop verification failed for %s "
                        "(%s): %s",
                        symbol,
                        current_stop,
                        runner_note,
                    )
                    if _exhausted:
                        logger.error(
                            "TP runner gave up verifying stop for %s after repeated "
                            "errors; record kept for restart resume",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                if runner_status == "rehung":
                    logger.warning(
                        "TP runner: recorded stop %s for %s was gone; %s",
                        current_stop,
                        symbol,
                        runner_note,
                    )
                    new_stop_id = runner_stop
                    moved = True  # 替代单已挂(entry 优先), 无需再撤旧
                else:
                    try:
                        swap_status, new_stop_id, swap_note = _swap_stop_to_price(
                            client,
                            symbol=symbol,
                            side=side,
                            entry=entry,
                            live_stop_id=current_stop,
                            bridge_qty=abs(amount),
                        )
                    except BinanceAPIError as exc:
                        _exhausted = budget.record()
                        logger.error(
                            "TP runner breakeven stop placement failed for %s "
                            "(kept original stop): %s",
                            symbol,
                            exc,
                        )
                        if _exhausted:
                            logger.error(
                                "TP runner gave up placing breakeven stop for %s after "
                                "repeated errors; original stop stays in place",
                                symbol,
                            )
                            return
                        time.sleep(poll_seconds)
                        continue
                    if swap_status == "kept":
                        # 桥接单仍在场(撤旧未遂或正式单被拒): 保本保护已由桥接单落地。
                        logger.warning("TP runner: %s", swap_note)
                    moved = True  # 撤旧已由桥接流程处理
            # 保本单已落地(或 guard 已完成): 立即回写注册表, 记录始终指向真实存在的单。
            _patch_guard(symbol, moved=True, stop_algo_id=new_stop_id)
            new_tp_id = f"pa-tp-{uuid.uuid4().hex[:24]}"
            try:
                client.place_close_algo_order(
                    symbol=symbol,
                    side=exit_side,
                    order_type="TAKE_PROFIT_MARKET",
                    stop_price=target2,
                    client_algo_id=new_tp_id,
                )
            except BinanceAPIError as exc:
                _exhausted = budget.record()
                logger.error(
                    "TP runner TP2 placement failed for %s: %s",
                    symbol,
                    exc,
                )
                if _exhausted:
                    logger.error(
                        "TP runner gave up placing TP2 for %s after repeated errors; "
                        "record kept for restart resume",
                        symbol,
                    )
                    return
                time.sleep(poll_seconds)
                continue
            _patch_guard(
                symbol,
                moved=True,
                stop_algo_id=new_stop_id,
                tp_algo_id=new_tp_id,
                partial_done=True,
            )
            logger.info(
                "TP runner: %s %s half closed; breakeven stop + TP2=%s in place",
                symbol,
                side,
                _decimal_text(target2),
            )
            return


def _timestop_deadline_hit(
    record: dict[str, Any] | None, stop_minutes: float, *, now: float | None = None
) -> bool:
    """True when the recorded position has been open past the time-stop."""
    if not isinstance(record, dict) or stop_minutes <= 0:
        return False
    ts = record.get("ts")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False
    return (now if now is not None else time.time()) - float(ts) >= stop_minutes * 60


def _timestop_loop(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    stop_minutes: float,
    poll_seconds: float,
) -> None:
    """Close the remaining position once it aged past stop_minutes.

    Runs alongside the breakeven guard and TP1 runner (all read the same
    registry record under the per-symbol lock). After closing, a resting
    partial TP1 order is cancelled and the record dropped unless an unfinished
    runner still owns that lifecycle (it then observes the flat position on
    its next poll and cleans up itself).
    """
    errors = 0
    while True:
        record = _read_guard(symbol)
        if record is None or not _timestop_deadline_hit(record, stop_minutes):
            time.sleep(poll_seconds)
            continue
        side = str(record.get("side") or "")
        if side not in ("BUY", "SELL"):
            return
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None or str(fresh.get("side") or "") != side:
                return
            if not _timestop_deadline_hit(fresh, stop_minutes):
                continue  # record replaced by a newer position: re-arm on its deadline
            try:
                info = current_position(client, symbol)
                amount = info["amount"]
            except BinanceAPIError as exc:
                if _is_rate_limit_reason(str(exc)):
                    # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数。
                    _guard_rate_limit_wait(poll_seconds)
                    continue
                errors += 1
                logger.warning(
                    "Time-stop poll failed for %s (attempt %d): %s",
                    symbol, errors, exc,
                )
                if errors >= 5:
                    logger.error(
                        "Time-stop gave up for %s after repeated API errors; "
                        "static stop/TP stays in place",
                        symbol,
                    )
                    return
                time.sleep(poll_seconds)
                continue
            exit_side = "SELL" if side == "BUY" else "BUY"
            partial_pending = (
                _positive_decimal(fresh.get("partial_qty")) is not None
                and not bool(fresh.get("partial_done"))
            )
            if amount != 0:
                if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
                    return  # not our position anymore
                try:
                    client.close_market_position(
                        symbol=symbol,
                        side=exit_side,
                        quantity=abs(amount),
                    )
                except BinanceAPIError as exc:
                    errors += 1
                    logger.error(
                        "Time-stop close failed for %s (%s): %s",
                        symbol, _decimal_text(abs(amount)), exc,
                    )
                    if errors >= 5:
                        logger.error(
                            "Time-stop gave up closing %s after repeated errors; "
                            "static stop/TP stays in place",
                            symbol,
                        )
                        return
                    time.sleep(poll_seconds)
                    continue
                logger.info(
                    "Time-stop reached for %s %s: closed remaining %s",
                    symbol, side, _decimal_text(abs(amount)),
                )
            if partial_pending:
                # runner 仍存活, 由它撤残留 TP1 并清理记录 (下一轮见 amount=0).
                return
            tp_algo_id = str(fresh.get("tp_algo_id") or "")
            if tp_algo_id:
                try:
                    client.cancel_algo_order(client_algo_id=tp_algo_id)
                except BinanceAPIError as exc:
                    if not _is_missing_algo_order_error(exc):
                        logger.error(
                            "Time-stop residual TP1 cancel failed for %s (%s): %s",
                            symbol, tp_algo_id, exc,
                        )
                        return
            _drop_guard(symbol)
            return


def _maybe_guard(
    client: BinanceUSDMTestnetClient,
    config: BinanceUSDMTestnetSettings,
    symbol: str,
    side: str,
    stop: Decimal,
    target: Decimal,
    stop_algo_id: str,
    conf: float | None,
    *,
    quantity: Decimal | None = None,
    target2: Decimal | None = None,
    tp_algo_id: str | None = None,
    partial_qty: Decimal | None = None,
) -> None:
    """Register the open position and arm its managers when enabled.

    Two optional managers share one per-symbol registry record: the breakeven
    guard (moves the SL to entry once float profit hits its trigger) and the
    TP1 partial runner (moves the remainder to TP2 after the reduceOnly half
    closed). Either manager only starts when its feature applies.

    ``partial_qty`` comes from _attach_protection (single source of truth):
    the registration never re-queries LOT_SIZE so the manager state always
    matches the TP1 order that was actually placed.
    """
    pct = float(config.tp_partial_close_pct or 0.0)
    if pct <= 0:
        partial_qty = None  # 功能关闭时忽略调用方传入的部分计划
    guard_on = _guard_enabled(config, conf)
    partial_on = (
        partial_qty is not None
        and quantity is not None
        and target2 is not None
        and tp_algo_id is not None
    )
    if not guard_on and not partial_on:
        return
    record: dict[str, Any] = {
        "stop_algo_id": stop_algo_id,
        "stop0": _decimal_text(stop),
        "target": _decimal_text(target),
        "side": side,
        "conf": conf,
        "ts": time.time(),
        "moved": False,
    }
    if partial_on:
        record.update(
            {
                "tp_algo_id": str(tp_algo_id),
                "target2": _decimal_text(target2),
                "qty": _decimal_text(quantity),
                "partial_qty": _decimal_text(partial_qty),
                "partial_done": False,
            }
        )
    _register_guard(symbol, record)
    if guard_on:
        watcher = threading.Thread(
            target=_breakeven_guard_loop,
            kwargs={
                "client": client,
                "symbol": symbol,
                "trigger": str(config.breakeven_stop_trigger),
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        watcher.start()
        logger.info(
            "Breakeven guard started for %s %s (conf=%s)",
            symbol,
            side,
            conf if conf is not None else "-",
        )
    if partial_on:
        runner = threading.Thread(
            target=_tp_runner_loop,
            kwargs={
                "client": client,
                "symbol": symbol,
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        )
        runner.start()
        logger.info(
            "TP partial runner started for %s %s (TP1 close %s%%, runner to TP2=%s)",
            symbol,
            side,
            str(config.tp_partial_close_pct).rstrip("0").rstrip("."),
            _decimal_text(target2),
        )
    # 每个已注册记录都配一个看护线程: breakeven guard 与 TP runner 只在自己的
    # 触发点核验止损, 触发前 resting STOP 若在交易所端消失, 持仓就没有兜底
    # (09-07 事故)。看护线程同时负责 runner 结束后的终态补挂。
    watchdog = threading.Thread(
        target=_stop_watchdog_loop,
        kwargs={
            "client": client,
            "symbol": symbol,
            "poll_seconds": float(config.breakeven_poll_seconds),
            "floor_pct": float(config.min_stop_distance_pct),
        },
        daemon=True,
    )
    watchdog.start()
    logger.info("Stop watchdog started for %s", symbol)
    time_stop_minutes = int(config.time_stop_minutes or 0)
    if time_stop_minutes > 0:
        ts_thread = threading.Thread(
            target=_timestop_loop,
            kwargs={
                "client": client,
                "symbol": symbol,
                "stop_minutes": float(time_stop_minutes),
                "poll_seconds": float(config.breakeven_poll_seconds),
            },
            daemon=True,
        )
        ts_thread.start()
        logger.info(
            "Time-stop manager started for %s %s (limit %d minutes)",
            symbol,
            side,
            time_stop_minutes,
        )
def _resume_guards(
    settings: Settings | None,
    *,
    label: str,
    kind_enabled: Callable[[BinanceUSDMTestnetSettings], bool],
    spawn: Callable[[BinanceUSDMTestnetClient, str, dict[str, Any], BinanceUSDMTestnetSettings], bool],
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Shared restart-resume skeleton for the position-lifecycle managers.

    Owns the common sequence - environment resolution, global kill gates,
    guard-record snapshot, client construction - so each manager only declares
    its kind gate and per-record spawn predicate.
    """
    configure_binance_environment(settings)
    config = binance_env.active_cfg(settings)
    if not config.enabled or config.dry_run or config.emergency_stop:
        return 0
    if not kind_enabled(config):
        return 0
    records = {
        symbol: dict(record)
        for symbol, record in _STATE_STORE.guards_all().items()
        if isinstance(record, dict)
    }
    if not records:
        return 0
    try:
        active = client or BinanceUSDMTestnetClient(config.api_key, config.api_secret)
    except ValueError as exc:
        logger.error("Cannot resume %s: %s", label, exc)
        return 0
    return sum(1 for symbol, record in records.items() if spawn(active, symbol, record, config))


def resume_breakeven_guards(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm breakeven guard loops for registered open positions after restart.

    Returns the number of guards resumed. Guards that already moved their stop
    (or whose position is gone) exit immediately on their first poll.
    """

    def _spawn(
        active: BinanceUSDMTestnetClient,
        symbol: str,
        record: dict[str, Any],
        config: BinanceUSDMTestnetSettings,
    ) -> bool:
        if record.get("moved"):
            return False
        if not _guard_enabled(config, record.get("conf")):
            return False
        threading.Thread(
            target=_breakeven_guard_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "trigger": str(config.breakeven_stop_trigger),
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        ).start()
        return True

    return _resume_guards(
        settings,
        label="breakeven guards",
        kind_enabled=lambda c: str(c.breakeven_stop_trigger) != "off",
        spawn=_spawn,
        client=client,
    )


def resume_tp_runners(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm TP1 partial runners for unfinished records after a restart.

    Runner threads do not survive a process restart. Records with
    ``partial_done`` are skipped; a runner re-polled for the others exits
    quickly when the position is gone (cleaning the residual TP1) or swaps
    the remainder to TP2 when the partial leg fired while we were down.
    Legacy breakeven-only records (no partial fields) are left untouched for
    the guard resume path.

    Returns the number of runners resumed.
    """

    def _spawn(
        active: BinanceUSDMTestnetClient,
        symbol: str,
        record: dict[str, Any],
        config: BinanceUSDMTestnetSettings,
    ) -> bool:
        if record.get("partial_done"):
            return False
        tp_algo_id = str(record.get("tp_algo_id") or "")
        stop_algo_id = str(record.get("stop_algo_id") or "")
        if (
            _positive_decimal(record.get("qty")) is None
            or _positive_decimal(record.get("partial_qty")) is None
            or _positive_decimal(record.get("target2")) is None
            or not tp_algo_id
            or not stop_algo_id
        ):
            return False  # 旧 guard 记录/残缺记录: 留给 guard resume 处理
        threading.Thread(
            target=_tp_runner_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        ).start()
        logger.info("Resumed TP partial runner for %s", symbol)
        return True

    return _resume_guards(
        settings,
        label="TP partial runners",
        kind_enabled=lambda c: float(c.tp_partial_close_pct or 0.0) > 0,
        spawn=_spawn,
        client=client,
    )


def resume_time_stops(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm time-stop managers for registered records after a restart.

    Any record with a side and ts gets a manager (records at or past their
    deadline are closed on the first poll). Returns the resumed count.
    """

    def _spawn(
        active: BinanceUSDMTestnetClient,
        symbol: str,
        record: dict[str, Any],
        config: BinanceUSDMTestnetSettings,
    ) -> bool:
        ts = record.get("ts")
        side = str(record.get("side") or "")
        if not isinstance(ts, (int, float)) or ts <= 0 or side not in ("BUY", "SELL"):
            return False
        threading.Thread(
            target=_timestop_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "stop_minutes": float(config.time_stop_minutes or 0),
                "poll_seconds": float(config.breakeven_poll_seconds),
            },
            daemon=True,
        ).start()
        logger.info("Resumed time-stop manager for %s", symbol)
        return True

    return _resume_guards(
        settings,
        label="time-stop managers",
        kind_enabled=lambda c: int(c.time_stop_minutes or 0) > 0,
        spawn=_spawn,
        client=client,
    )


def resume_stop_watchdogs(
    settings: Settings | None = None,
    *,
    client: BinanceUSDMTestnetClient | None = None,
) -> int:
    """Re-arm protective-stop watchdogs after a restart.

    Runner/guard threads do not survive a restart. Records whose managers had
    already finished (partial_done / moved) or that sit mid-TP2 previously had
    NO watcher at all: a stop that died server-side (cancel race / precision
    bug / manual removal) left the position naked until the next restart.
    Unmoved records (breakeven / TP1 not reached) are armed too, because their
    managers only look at the stop once their own trigger fires.
    Returns the number of watchdogs resumed.
    """

    def _spawn(
        active: BinanceUSDMTestnetClient,
        symbol: str,
        record: dict[str, Any],
        config: BinanceUSDMTestnetSettings,
    ) -> bool:
        if str(record.get("side") or "") not in ("BUY", "SELL"):
            return False
        threading.Thread(
            target=_stop_watchdog_loop,
            kwargs={
                "client": active,
                "symbol": symbol,
                "poll_seconds": float(config.breakeven_poll_seconds),
                "floor_pct": float(config.min_stop_distance_pct),
            },
            daemon=True,
        ).start()
        logger.info("Resumed stop watchdog for %s", symbol)
        return True

    return _resume_guards(
        settings,
        label="stop watchdogs",
        kind_enabled=lambda c: True,
        spawn=_spawn,
        client=client,
    )


#: 未进入保本/TP2 阶段的持仓: breakeven guard 与 TP runner 只在各自触发点才
#: 核验止损, 触发前 resting STOP 若已在交易所端消失, 持仓整段无保护(2026-09-07
#: ZEC 事故)。本看护按此轮数间隔复核存活(轮询 10s 时约 60s 一次), 兼顾 Testnet
#: 共享 IP 限流。
_UNMOVED_STOP_VERIFY_TICKS = 6


def _stop_watchdog_loop(
    *,
    client: BinanceUSDMTestnetClient,
    symbol: str,
    poll_seconds: float,
    floor_pct: float = 0.0,
) -> None:
    """Keep the recorded algo stop really resting on the exchange.

    Covers every stage of the position's life: records whose managers already
    finished (moved / partial_done / TP1-fired remainder) are verified every
    poll, while an unmoved record (breakeven and TP1 not reached yet) is
    verified every _UNMOVED_STOP_VERIFY_TICKS polls - before 2026-09-07
    nothing checked that leg at all, so a stop that died server-side left the
    position naked until the next restart. A missing stop is re-hung; when the
    position is flat the residual TP order is cancelled and the record dropped.
    """
    budget = ErrorBudget()
    unmoved_ticks = 0
    while True:
        record = _read_guard(symbol)
        if record is None:
            return
        side = str(record.get("side") or "")
        qty = _positive_decimal(record.get("qty"))
        partial_qty = _positive_decimal(record.get("partial_qty"))
        if side not in ("BUY", "SELL"):
            return  # 残缺记录: 不归看护管
        legacy = qty is None or partial_qty is None
        try:
            info = current_position(client, symbol)
            amount = info["amount"]
            entry = info["entry"]
        except BinanceAPIError as exc:
            if _is_rate_limit_reason(str(exc)):
                # Testnet 共享 IP 封禁: 冷却等待解禁, 不撞墙也不计入弃守计数.
                _guard_rate_limit_wait(poll_seconds)
                continue
            _exhausted = budget.record()
            logger.warning(
                "Stop watchdog poll failed for %s (attempt %d): %s",
                symbol,
                budget.count,
                exc,
            )
            if _exhausted:
                logger.error(
                    "Stop watchdog gave up polling %s after repeated API errors; "
                    "record kept for restart resume",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if (side == "BUY" and amount < 0) or (side == "SELL" and amount > 0):
            return  # not our position anymore
        if amount == 0:
            # 仓位已平: 撤残留部分单/TP2 单后移除记录(撤单幂等, -2011 视为已清).
            tp_algo_id = str(record.get("tp_algo_id") or "")
            if tp_algo_id:
                try:
                    client.cancel_algo_order(client_algo_id=tp_algo_id)
                except BinanceAPIError as exc:
                    if not _is_missing_algo_order_error(exc):
                        _exhausted = budget.record()
                        logger.error(
                            "Stop watchdog residual TP cancel failed for %s (%s): %s",
                            symbol,
                            tp_algo_id,
                            exc,
                        )
                        if _exhausted:
                            return
                        time.sleep(poll_seconds)
                        continue
            _drop_guard(symbol)
            logger.info("Stop watchdog: %s position closed; record dropped", symbol)
            return
        terminal = (
            bool(record.get("moved"))
            or bool(record.get("partial_done"))
            or (not legacy and abs(amount) < qty)
        )
        if not terminal:
            # 未到保本/TP1 触发点: guard 与 runner 只在自己的触发点核验止损, 触发
            # 前若 resting STOP 已在交易所端消失, 持仓整段无保护(09-07 事故)。
            # 慢频复核, 消失即按原止损价补挂。
            unmoved_ticks += 1
            if unmoved_ticks >= _UNMOVED_STOP_VERIFY_TICKS:
                unmoved_ticks = 0
                if entry is not None and _positive_decimal(record.get("stop0")) is not None:
                    with _manager_lock(symbol):
                        fresh = _read_guard(symbol)
                        if fresh is None:
                            return
                        current_stop = str(fresh.get("stop_algo_id") or "")
                        if not current_stop:
                            return
                        status, _rehung_id, note = _ensure_protective_stop(
                            client,
                            symbol=symbol,
                            record=fresh,
                            entry=entry,
                            stage_tp2=False,
                            floor_pct=floor_pct,
                        )
                    if status == "error":
                        _exhausted = budget.record()
                        logger.error(
                            "Stop watchdog pre-move verify/re-hang failed for %s (%s): %s",
                            symbol,
                            current_stop,
                            note,
                        )
                        if _exhausted:
                            logger.error(
                                "Stop watchdog gave up polling %s after repeated API "
                                "errors before the stop move; record kept for restart resume",
                                symbol,
                            )
                            return
                    else:
                        budget.reset()
                        if status == "rehung":
                            logger.warning(
                                "Stop watchdog: %s pre-move recorded stop %s was gone; %s",
                                symbol,
                                current_stop,
                                note,
                            )
            time.sleep(poll_seconds)
            continue
        if entry is None:
            return  # 拿不到入场价, 保本价无从谈起
        with _manager_lock(symbol):
            fresh = _read_guard(symbol)
            if fresh is None:
                return
            current_stop = str(fresh.get("stop_algo_id") or "")
            if not current_stop:
                return
            status, _rehung_id, note = _ensure_protective_stop(
                client,
                symbol=symbol,
                record=fresh,
                entry=entry,
                stage_tp2=True,
                floor_pct=floor_pct,
            )
        if status == "error":
            _exhausted = budget.record()
            logger.error(
                "Stop watchdog verify/re-hang failed for %s (%s): %s",
                symbol,
                current_stop,
                note,
            )
            if _exhausted:
                logger.error(
                    "Stop watchdog gave up for %s after repeated errors; "
                    "record kept for restart resume",
                    symbol,
                )
                return
            time.sleep(poll_seconds)
            continue
        if status == "rehung":
            logger.warning(
                "Stop watchdog: %s recorded stop %s was gone; %s",
                symbol,
                current_stop,
                note,
            )
        budget.reset()
        time.sleep(poll_seconds)
