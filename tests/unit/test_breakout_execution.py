"""突破单(STOP_MARKET)自动执行路径的单元测试.

覆盖 2026-09-19 新增的 `_execute_breakout_signal`:
- 默认开关关闭时保持"人工队列"行为(以及 dry_run 影子样本);
- 开启后挂 STOP_MARKET, 触发价做跳动对齐(多单向上取整, 不会退回极值内侧);
- 三价几何 / entry-anchored 止损下限 / 半仓灰度;
- 挂单后复用 `_watch_limit_entry` 成交后补挂保护。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from pa_agent.config.settings import Settings
from pa_agent.trading import binance_usdm_testnet
from pa_agent.trading.binance_usdm_testnet import execute_market_signal
from tests.unit.test_binance_usdm_testnet import (
    FakeClient,
    _long_decision,
    _pending_state,
    _settings,
    _stub_watcher_thread,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        binance_usdm_testnet,
        "_RUNTIME_STATE_PATH",
        str(tmp_path / "binance_usdm_testnet_state.json"),
    )


def _breakout_settings(*, enabled: bool = True, dry_run: bool = False) -> Settings:
    settings = _settings(dry_run=dry_run)
    settings.binance_usdm_testnet.breakout_order_enabled = enabled
    # 这些用例只验证执行层几何/挂单行为, 关掉 trader's equation 让断言聚焦。
    settings.binance_usdm_testnet.require_trader_equation = False
    return settings


def _long_breakout(entry: object = 101) -> dict:
    """做多突破单: 触发价 101 在 mark(100) 上方, 止损 90 / 止盈 120."""
    return _long_decision() | {
        "order_type": "突破单",
        "entry_basis_bar": "K1",
        "entry_basis_extreme": "high",
        "entry_price": entry,
    }


def _short_breakout(entry: object = 99) -> dict:
    return {
        "order_type": "突破单",
        "order_direction": "做空",
        "entry_price": entry,
        "entry_basis_bar": "K1",
        "entry_basis_extreme": "low",
        "stop_loss_price": 110,
        "take_profit_price": 80,
        "estimated_win_rate": 70,
    }


def test_breakout_disabled_falls_back_to_manual_queue(monkeypatch) -> None:
    client = FakeClient()
    started = _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _long_breakout(), _breakout_settings(enabled=False), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "skipped", result.reason
    assert "manual review" in result.reason
    assert client.calls == []
    assert started == []


def test_breakout_dry_run_reports_shadow_sample(monkeypatch) -> None:
    """dry_run 下执行层不发请求, 但结果必须能被监控层认出是"突破单影子"."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _long_breakout(), _breakout_settings(enabled=False, dry_run=True), client=client
    )

    assert result.status == "dry_run"
    assert "breakout" in result.reason
    assert client.calls == []


def test_breakout_places_stop_market_entry_and_tracks_pending(monkeypatch) -> None:
    client = FakeClient()
    started = _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _long_breakout(), _breakout_settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "pending", result.reason
    orders = [call[1] for call in client.calls if call[0] == "stop_market_entry"]
    assert len(orders) == 1
    assert orders[0]["side"] == "BUY"
    assert orders[0]["stop_price"] == Decimal("101.0")
    # 触发前不能挂保护单: 持仓还不存在。
    assert "protection" not in [call[0] for call in client.calls]
    # 触发价必须严格在 mark 上方, 否则等于立即市价成交。
    assert "armed" in result.reason
    # 挂单必须落 pending 并布好成交 watcher, 否则成交即裸仓。
    pending = _pending_state()
    assert pending["BTCUSDT"]["kind"] == "breakout"
    assert pending["BTCUSDT"]["client_id"] == orders[0]["client_id"]
    assert started and started[0]["client_id"] == orders[0]["client_id"]


def test_breakout_trigger_rounds_up_to_tick(monkeypatch) -> None:
    """tick=0.1 时 101.05 必须向上取整成 101.1, 不能落在依据 K 极值内侧."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _long_breakout("101.05"), _breakout_settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "pending", result.reason
    orders = [call[1] for call in client.calls if call[0] == "stop_market_entry"]
    assert orders[0]["stop_price"] == Decimal("101.1")


def test_short_breakout_trigger_rounds_down_to_tick(monkeypatch) -> None:
    """做空镜像: 触发价向下取整, 保证严格低于依据 K 的低点."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _short_breakout("98.999"), _breakout_settings(), client=client
    )

    assert result.status == "pending", result.reason
    orders = [call[1] for call in client.calls if call[0] == "stop_market_entry"]
    assert orders[0]["stop_price"] == Decimal("98.9")
    assert orders[0]["side"] == "SELL"


def test_already_crossed_breakout_is_labelled(monkeypatch) -> None:
    """触发价已越过 mark: 结果仍然是挂单, 但日志要写明会立即成交."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _long_breakout(100), _breakout_settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "pending", result.reason
    assert "ALREADY CROSSED" in result.reason


def test_breakout_rejects_stop_on_the_wrong_side(monkeypatch) -> None:
    """做多突破单要求 stop < trigger < target; 止损跑到触发价上方必须拒单."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)
    decision = _long_breakout() | {"stop_loss_price": 101.5}

    result = execute_market_signal(
        decision, _breakout_settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "rejected", result.reason
    assert "stop < trigger < target" in result.reason
    assert "stop_market_entry" not in [call[0] for call in client.calls]


def test_breakout_rejects_stop_too_close_to_trigger(monkeypatch) -> None:
    """止损下限按触发价量, 不能按 mark 量 —— 否则正常突破单会被误拒."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)
    settings = _breakout_settings()
    settings.binance_usdm_testnet.min_stop_mode = "fixed"
    settings.binance_usdm_testnet.min_stop_distance_pct = 5.0
    # 触发价 101 / 止损 100 -> 距离 0.99% < 5% 下限。
    decision = _long_breakout() | {"stop_loss_price": 100}

    result = execute_market_signal(
        decision, settings, analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "rejected", result.reason
    assert "too close to trigger" in result.reason


def test_breakout_size_is_scaled_down(monkeypatch) -> None:
    """灰度: breakout_size_scale=0.5 时数量减半, 并向下取整到 LOT_SIZE."""
    _stub_watcher_thread(monkeypatch)
    full_client = FakeClient()
    full_settings = _breakout_settings()
    full_settings.binance_usdm_testnet.breakout_size_scale = 1.0
    baseline = execute_market_signal(
        _long_breakout(), full_settings, analysis_symbol="BTCUSDT", client=full_client
    )
    base_qty = next(
        c[1]["quantity"] for c in full_client.calls if c[0] == "stop_market_entry"
    )
    # 上一张还在挂 -> 先把 pending 清掉, 否则会被"换单"逻辑接管。
    binance_usdm_testnet._drop_pending("BTCUSDT")

    half_client = FakeClient()
    half_settings = _breakout_settings()
    half_settings.binance_usdm_testnet.breakout_size_scale = 0.5
    scaled = execute_market_signal(
        _long_breakout(), half_settings, analysis_symbol="BTCUSDT", client=half_client
    )
    scaled_qty = next(
        c[1]["quantity"] for c in half_client.calls if c[0] == "stop_market_entry"
    )

    assert scaled.status == "pending", scaled.reason
    assert baseline.status == "pending", baseline.reason
    assert scaled_qty < base_qty
    assert scaled_qty == (base_qty * Decimal("0.5")).quantize(Decimal("0.001"))
    assert "scaled" in scaled.reason


def test_breakout_requires_entry_price(monkeypatch) -> None:
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)
    decision = _long_breakout() | {"entry_price": None}

    result = execute_market_signal(
        decision, _breakout_settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status == "rejected", result.reason
    assert "requires entry_price" in result.reason


def test_breakout_order_type_is_no_longer_rejected_by_default(monkeypatch) -> None:
    """回归: 执行层不再把突破单一律判成"需人工复核"(那是本次要修的漏单)."""
    client = FakeClient()
    _stub_watcher_thread(monkeypatch)

    result = execute_market_signal(
        _long_breakout(), _breakout_settings(), analysis_symbol="BTCUSDT", client=client
    )

    assert result.status != "rejected"
