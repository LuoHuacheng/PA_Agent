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


def test_symbol_normalized_to_usdt_perp():
    import tempfile

    doc = {"name": "t", "total_trades": 1, "backtest_snapshot": {"paired_trades": [
        {"action": "CLOSE_LONG", "side": "LONG", "symbol": "ETH",
         "entry_price": 100, "exit_price": 101, "quantity": 1, "total_fee": 0.1,
         "open_timestamp": 1700000000000, "close_timestamp": 1700000900000,
         "holding_seconds": 9000, "pnl_net": 1, "pnl_gross": 1.1,
         "open_fee": 0.05, "close_fee": 0.05, "slippage": 0,
         "open_reason": "r", "close_reason": "take_profit", "trade_key": "k"}]}}
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "demo" / "detail.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(doc), encoding="utf-8")
        dump = load_dump(p)
        assert dump.trades[0].symbol == "ETHUSDT"
        assert dump.slug == "demo"
