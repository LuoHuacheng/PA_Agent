"""VergeX detail.json 加载器: 读取 backtest_snapshot.paired_trades 并归一化为成对成交记录."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PairTrade:
    """单笔成对成交(开仓-平仓)归一化后的记录."""

    open_ms: int
    close_ms: int
    side: int  # 1 多 / -1 空
    symbol: str
    entry: float
    exit: float
    qty: float
    pnl_net: float
    pnl_gross: float
    fee: float
    holding_s: int
    close_reason: str
    open_reason: str


@dataclass(frozen=True)
class VergexDump:
    """一份 VergeX 策略详情 dump 的归一化视图."""

    slug: str
    name: str
    total_trades: int
    win_rate: float
    trades: tuple[PairTrade, ...]


def _side(raw) -> int:
    """映射 raw side 为 1(多)或 -1(空)."""
    return 1 if str(raw).strip().lower() in ("long", "buy", "1") else -1


def _symbol(raw) -> str:
    """归一化 symbol: strip 后大写, 不以 USDT 结尾则追加(如 ETH -> ETHUSDT)."""
    sym = str(raw).strip().upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    return sym


def load_dump(detail_path: Path, *, slug: str | None = None) -> VergexDump:
    """加载 detail.json, 返回按 open_ms 升序的 VergexDump(win_rate 由 trades 计算)."""
    path = Path(detail_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    trades = tuple(
        sorted(
            (
                PairTrade(
                    open_ms=int(row["open_timestamp"]),
                    close_ms=int(row["close_timestamp"]),
                    side=_side(row.get("side")),
                    symbol=_symbol(row["symbol"]),
                    entry=float(row["entry_price"]),
                    exit=float(row["exit_price"]),
                    qty=float(row["quantity"]),
                    pnl_net=float(row["pnl_net"]),
                    pnl_gross=float(row["pnl_gross"]),
                    fee=float(row["total_fee"]),
                    holding_s=int(row["holding_seconds"]),
                    close_reason=str(row.get("close_reason", "")),
                    open_reason=str(row.get("open_reason", "")),
                )
                for row in doc.get("backtest_snapshot", {}).get("paired_trades", [])
            ),
            key=lambda t: t.open_ms,
        )
    )
    wins = sum(1 for t in trades if t.pnl_net > 0)
    return VergexDump(
        slug=slug if slug is not None else path.parent.name,
        name=doc.get("name") or "",
        total_trades=doc.get("total_trades", len(trades)),
        win_rate=wins / len(trades) if trades else 0.0,
        trades=trades,
    )
