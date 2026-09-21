"""快照滞后导致的"仓位已平"误判回归 + 计划入场价浮点失配回归。

事故(2026-09-21 16:33 ETHUSDT): 限价入场成交后 0.4s, 共享账户快照仍是成交前
的空仓(WS 在线时轮询周期 300s, 快照 360s 内都算"新鲜"), TP runner 与 stop
watchdog 据此判定仓位已平, 撤掉刚挂的 TP1 并丢弃 guard 记录; 16:41 对账收养
时又因交易所 entryPrice "2678.7000000000003" 与计划 "2678.7" 精确不等而恢复
不到止盈计划, 最终持仓裸剩一张止损。
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from pa_agent.trading import binance_usdm_testnet as but
from pa_agent.trading import position_manager as pm
from pa_agent.trading.binance_usdm_testnet import BinanceAPIError


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        but,
        "_RUNTIME_STATE_PATH",
        str(tmp_path / "binance_usdm_state.json"),
    )


# ── _prices_match / _recover_plan ────────────────────────────────────────────

def test_prices_match_absorbs_exchange_float_noise() -> None:
    assert pm._prices_match(Decimal("2678.7"), Decimal("2678.7000000000003")) is True
    assert pm._prices_match(Decimal("2678.7"), Decimal("2678.7")) is True


def test_prices_match_rejects_a_real_different_level() -> None:
    assert pm._prices_match(Decimal("2678.7"), Decimal("2678.8")) is False
    assert pm._prices_match(Decimal("0"), Decimal("1")) is False


def _write_plan_csv(directory, symbol: str, rows: list[list[str]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{symbol}_30m.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "record_time",
                "symbol",
                "order_direction",
                "order_type",
                "entry_price",
                "stop_loss_price",
                "take_profit_price",
                "take_profit_price_2",
            ]
        )
        writer.writerows(rows)


def test_recover_plan_matches_noisy_exchange_entry(tmp_path, monkeypatch) -> None:
    """交易所返回的浮点噪声入场价必须仍能匹配到计划(否则收养仓没有止盈腿)。"""
    directory = tmp_path / "trade_records"
    stamp = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    _write_plan_csv(
        directory,
        "ETHUSDT",
        [[stamp, "ETHUSDT", "做多", "限价单", "2678.7", "2654.38", "2716.62", "2751.65"]],
    )
    monkeypatch.setattr(pm, "_TRADE_RECORDS_DIR", directory)

    plan = pm._recover_plan("ETHUSDT", "BUY", Decimal("2678.7000000000003"))
    assert plan is not None, "浮点噪声不得让计划恢复静默失败"
    assert plan["target"] == Decimal("2716.62")
    assert plan["target2"] == Decimal("2751.65")
    # 容差只吸收噪声, 不得把邻近的真实价位张冠李戴
    assert pm._recover_plan("ETHUSDT", "BUY", Decimal("2680.0")) is None


# ── _rest_confirms_flat ──────────────────────────────────────────────────────

class _RestClient:
    def __init__(self, amount: str) -> None:
        self._amount = Decimal(amount)

    def position_info(self, symbol: str) -> dict:
        return {"amount": self._amount, "entry": Decimal("100")}


class _RestErrorClient:
    def position_info(self, symbol: str) -> dict:
        raise BinanceAPIError("Binance network error: test outage")


def test_rest_confirms_flat_only_on_a_real_zero() -> None:
    assert pm._rest_confirms_flat(_RestClient("0"), "ETHUSDT") is True
    assert pm._rest_confirms_flat(_RestClient("0.164"), "ETHUSDT") is False


def test_rest_confirms_flat_fails_closed_on_read_error() -> None:
    # 读不到就按"未确认"处理, 绝不触发不可逆清理
    assert pm._rest_confirms_flat(_RestErrorClient(), "ETHUSDT") is False


# ── 看护循环: 未确认的"已平"不得触发撤单/删记录 ──────────────────────────────

class _SnapshotFlatClient:
    """position_info 依次吐出具仓位, 模拟"直连才知道仓还在"。"""

    def __init__(self, rest_amounts: list[str]) -> None:
        self.calls: list[tuple] = []
        self._rest = [Decimal(a) for a in rest_amounts]

    def position_info(self, symbol: str) -> dict:
        self.calls.append(("position_info", symbol))
        amount = self._rest.pop(0) if self._rest else Decimal("0")
        return {"amount": amount, "entry": Decimal("100")}

    def cancel_algo_order(self, *, client_algo_id: str) -> None:
        self.calls.append(("cancel_protection", client_algo_id))

    def place_close_algo_order(self, **kwargs: object) -> None:
        self.calls.append(("protection", kwargs))

    def mark_price(self, symbol: str) -> Decimal:
        self.calls.append(("mark_price", symbol))
        return Decimal("100")


def _tp_record() -> None:
    but._register_guard(
        "ETHUSDT",
        {
            "stop_algo_id": "pa-sl-316c340ef64b40d8995ad537",
            "tp_algo_id": "pa-tp-ae031f2904694897a9ccddbf",
            "stop0": "2654.38",
            "target": "2716.62",
            "target2": "2751.65",
            "qty": "0.164",
            "partial_qty": "0.082",
            "side": "BUY",
            "conf": None,
            "ts": 0.0,
            "moved": False,
            "partial_done": False,
            "adopted": True,
        },
    )


def _first_cancel_index(client: _SnapshotFlatClient) -> int:
    return next(
        index
        for index, call in enumerate(client.calls)
        if call[0] == "cancel_protection"
    )


def test_tp_runner_keeps_tp1_until_rest_confirms_flat(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(but.time, "sleep", sleeps.append)
    # 共享快照一律返回空仓(成交前的旧快照), 直连 REST 才看得到真实仓位
    monkeypatch.setattr(
        pm, "current_position", lambda client, symbol: {"amount": Decimal("0"), "entry": None}
    )
    _tp_record()
    client = _SnapshotFlatClient(["0.164", "0"])
    but._tp_runner_loop(client=client, symbol="ETHUSDT", poll_seconds=1.0)

    cancel_index = _first_cancel_index(client)
    rest_before_cancel = [c for c in client.calls[:cancel_index] if c[0] == "position_info"]
    assert rest_before_cancel, "撤 TP1 前必须先直连复核仓位"
    cancels = [c[1] for c in client.calls if c[0] == "cancel_protection"]
    assert cancels == ["pa-tp-ae031f2904694897a9ccddbf"]
    assert but._read_guard("ETHUSDT") is None, "只有确认平仓后才允许删除记录"


def test_tp_runner_never_cancels_on_an_unconfirmed_flat(monkeypatch) -> None:
    """REST 始终说有仓: 快照怎么空都不能撤 TP1 或丢记录。"""
    sleeps: list[float] = []
    monkeypatch.setattr(but.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        pm, "current_position", lambda client, symbol: {"amount": Decimal("0"), "entry": None}
    )

    class _BoundedClient(_SnapshotFlatClient):
        def position_info(self, symbol: str) -> dict:
            if len([c for c in self.calls if c[0] == "position_info"]) >= 3:
                # 非 BinanceAPIError 才不会被 _rest_confirms_flat 吞掉, 用作跳出
                # 无限轮询的哨兵(循环本身没有其他出口)。
                raise RuntimeError("test: stop the loop")
            return super().position_info(symbol)

    _tp_record()
    client = _BoundedClient(["0.164"] * 5)
    with pytest.raises(RuntimeError):
        but._tp_runner_loop(client=client, symbol="ETHUSDT", poll_seconds=1.0)

    assert [c for c in client.calls if c[0] == "cancel_protection"] == []
    assert but._read_guard("ETHUSDT") is not None, "记录必须留着重启 resume"


def test_stop_watchdog_keeps_record_until_rest_confirms_flat(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(but.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        pm, "current_position", lambda client, symbol: {"amount": Decimal("0"), "entry": None}
    )
    _tp_record()
    but._patch_guard("ETHUSDT", partial_done=True)
    client = _SnapshotFlatClient(["0.164", "0"])
    but._stop_watchdog_loop(client=client, symbol="ETHUSDT", poll_seconds=1.0)

    cancel_index = _first_cancel_index(client)
    rest_before_cancel = [c for c in client.calls[:cancel_index] if c[0] == "position_info"]
    assert rest_before_cancel, "删记录前必须先直连复核仓位"
    assert but._read_guard("ETHUSDT") is None
