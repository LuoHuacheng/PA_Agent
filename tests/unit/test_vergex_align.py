from tools.vergex_align import align_report
from tools.vergex_loader import PairTrade, VergexDump


def _klines():
    # 3 根 15m K 线, 从 0 开始
    base = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    return [(base + i * 900_000, 100.0, 101.0, 99.0, 100.5) for i in range(3)]


def _dump(open_ms, close_ms, exit_px=99.0, reason="stop_loss"):
    tr = PairTrade(open_ms=open_ms, close_ms=close_ms, side=1, symbol="ETHUSDT",
                   entry=100.0, exit=exit_px, qty=1.0, pnl_net=-1.0, pnl_gross=-1.0,
                   fee=0.0, holding_s=900, close_reason=reason, open_reason="")
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


def test_chinese_stop_reason_and_partial_tp_excluded():
    base = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    rep_cn = align_report(_dump(base + 900_000 + 5_000, base + 2 * 900_000,
                                reason="[多头止损]4h收阴线跌破EMA21(3100)"), _klines())
    assert rep_cn["close_reason_groups"]["止损类"] == 1
    rep_pt = align_report(_dump(base + 900_000 + 5_000, base + 2 * 900_000,
                                reason="Partial TP LONG 1.0 at 3.5ATR=100, SL->cost"), _klines())
    assert rep_pt["stop_dist_atr"] is None  # Partial TP 行不算止损行
    assert rep_pt["close_reason_groups"].get("止损类") in (None, 0)


def test_dict_klines_selects_by_symbol():
    base = 1_700_000_000_000 - 1_700_000_000_000 % 900_000
    kl = {"ETHUSDT": _klines(), "ZECUSDT": [(base + i * 900_000, 50.0, 51.0, 49.0, 50.2) for i in range(3)]}
    rep = align_report(_dump(base + 900_000 + 5_000, base + 2 * 900_000), kl)
    assert rep["bar_close_entry_rate"] == 1.0
