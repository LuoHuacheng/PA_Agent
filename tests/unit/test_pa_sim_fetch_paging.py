"""Paging contract for the account-data fetch (userTrades history)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tools._pa_sim_common import fetch_account_data


class _FakeClient:
    """Serves one full page then a short page; records the params it was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _request(self, method, path, params, signed=False):
        if path == '/fapi/v1/allOrders':
            return []
        self.calls.append(dict(params))
        if 'fromId' not in params:
            return [
                {'id': i, 'orderId': 1, 'side': 'BUY', 'qty': '1', 'price': '10',
                 'time': 1_000 + i, 'realizedPnl': '0', 'commission': '0.01'}
                for i in range(1, 1001)
            ]
        # second page: short -> stop
        return [
            {'id': 1000 + i, 'orderId': 1, 'side': 'SELL', 'qty': '1', 'price': '11',
             'time': 2_000 + i, 'realizedPnl': '1', 'commission': '0.01'}
            for i in range(1, 3)
        ]

    def income_history(self, start_ms=None):  # pragma: no cover - trivial
        return []


def test_usertrades_paging_starts_with_start_time_then_advances_by_from_id() -> None:
    client = _FakeClient()
    with tempfile.TemporaryDirectory() as tmp:
        fetch_account_data(client, ['ADAUSDT'], 500, Path(tmp))
        trades = json.loads((Path(tmp) / 'user_trades.json').read_text(encoding='utf-8'))
    assert len(trades['ADAUSDT']) == 1002          # both pages captured
    assert client.calls[0].get('startTime') == 500  # history must be anchored
    assert 'fromId' not in client.calls[0]
    assert client.calls[1].get('fromId') == 1001    # advance past the max id