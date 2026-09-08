"""Trade pnl report stat must share the repo-wide 0-net-is-loss rule (A5)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from trade_pnl_report import net, stat  # noqa: E402


def _trade(realized: float, fees: float = 0.0, *, closed=True) -> dict:
    return {"sym": "X", "realized": realized, "fees": fees,
            "closed_at": 1000 if closed else None}


def test_net_zero_counts_as_loss():
    ts = [_trade(5.0), _trade(-2.0), _trade(0.0)]
    g = stat(ts, {})
    assert g["closed"] == 3
    assert g["win"] == 1 and g["loss"] == 2
    assert g["winrate"] == 1 / 3 * 100


def test_open_rows_stay_out_of_win_rate():
    ts = [_trade(5.0), _trade(0.0, closed=False)]
    g = stat(ts, {})
    assert g["closed"] == 1 and g["open"] == 1
    assert g["win"] == 1 and g["loss"] == 0
    assert g["winrate"] == 100.0


def test_net_matches_legacy_formula():
    assert net(_trade(3.0, -0.1)) == 2.9
