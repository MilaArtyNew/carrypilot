"""
Live trade ledger — persists closed live trades to JSON.
PnL = price delta (short_entry-short_close + long_close-long_entry)
    + estimated funding (spread × notional × funding_events).
"""
import json
import time
from dataclasses import dataclass, asdict
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils import get_logger

log = get_logger("live_ledger")

DEFAULT_PATH = Path("/data/live_trades.json")
FUNDING_INTERVAL_HOURS = Decimal("8")


@dataclass
class LiveTrade:
    trade_id: str
    symbol: str
    short_exchange: str
    long_exchange: str
    qty: str
    short_entry: str
    long_entry: str
    spread_at_open: str          # funding spread at time of open
    opened_at: str
    status: str                  # "open" | "closed"
    closed_at: Optional[str] = None
    short_close: Optional[str] = None
    long_close: Optional[str] = None
    price_pnl: Optional[str] = None
    funding_pnl: Optional[str] = None
    realized_pnl: Optional[str] = None
    close_reason: Optional[str] = None


class LiveLedger:
    def __init__(self, path: Path = DEFAULT_PATH):
        self._path = path
        self._trades: dict[str, LiveTrade] = {}
        self._open_index: dict[str, str] = {}  # symbol → trade_id
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            for t in json.loads(self._path.read_text()):
                trade = LiveTrade(**{k: v for k, v in t.items() if k in LiveTrade.__dataclass_fields__})
                self._trades[trade.trade_id] = trade
                if trade.status == "open":
                    self._open_index[trade.symbol] = trade.trade_id
            log.info(f"Loaded {len(self._trades)} live trades ({len(self._open_index)} open)")
        except Exception as e:
            log.warning(f"Could not load live trades: {e}")

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([asdict(t) for t in self._trades.values()], indent=2))
        except Exception as e:
            log.error(f"Failed to save live trades: {e}")

    def record_open(
        self,
        symbol: str,
        short_exchange: str,
        long_exchange: str,
        qty: Decimal,
        short_entry: Decimal,
        long_entry: Decimal,
        spread: Decimal,
    ) -> LiveTrade:
        trade_id = f"{symbol}-{int(time.time())}"
        trade = LiveTrade(
            trade_id=trade_id,
            symbol=symbol,
            short_exchange=short_exchange,
            long_exchange=long_exchange,
            qty=str(qty),
            short_entry=str(short_entry),
            long_entry=str(long_entry),
            spread_at_open=str(spread),
            opened_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            status="open",
        )
        self._trades[trade_id] = trade
        self._open_index[symbol] = trade_id
        self._save()
        log.info(f"[LIVE] Opened {symbol} qty={qty} short@{short_entry:.4f} long@{long_entry:.4f}")
        return trade

    def record_close(
        self,
        symbol: str,
        short_close: Decimal,
        long_close: Decimal,
        reason: str = "manual",
    ) -> Optional[LiveTrade]:
        trade_id = self._open_index.pop(symbol, None)
        if not trade_id:
            return None
        trade = self._trades[trade_id]
        qty = Decimal(trade.qty)

        price_pnl = (Decimal(trade.short_entry) - short_close) * qty \
                  + (long_close - Decimal(trade.long_entry)) * qty

        avg_entry = (Decimal(trade.short_entry) + Decimal(trade.long_entry)) / 2
        notional = qty * avg_entry
        hours_held = self._hours_since(trade.opened_at)
        funding_events = Decimal(str(hours_held)) / FUNDING_INTERVAL_HOURS
        funding_pnl = Decimal(trade.spread_at_open) * notional * funding_events

        total_pnl = price_pnl + funding_pnl

        trade.status = "closed"
        trade.closed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        trade.short_close = str(short_close)
        trade.long_close = str(long_close)
        trade.price_pnl = str(round(price_pnl, 6))
        trade.funding_pnl = str(round(funding_pnl, 6))
        trade.realized_pnl = str(round(total_pnl, 6))
        trade.close_reason = reason
        self._save()
        log.info(
            f"[LIVE] Closed {symbol} price={price_pnl:.4f} funding≈{funding_pnl:.4f} "
            f"total={total_pnl:.4f} USD ({hours_held:.1f}h)"
        )
        return trade

    @staticmethod
    def _hours_since(opened_at_str: str) -> float:
        try:
            opened = datetime.strptime(opened_at_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - opened).total_seconds() / 3600
        except Exception:
            return 0.0

    def stats(self) -> dict:
        closed = [t for t in self._trades.values() if t.status == "closed" and t.realized_pnl]
        pnls = [Decimal(t.realized_pnl) for t in closed]
        price_pnls = [Decimal(t.price_pnl) for t in closed if t.price_pnl]
        funding_pnls = [Decimal(t.funding_pnl) for t in closed if t.funding_pnl]
        wins = sum(1 for p in pnls if p > 0)
        n = len(pnls)
        return {
            "open": len(self._open_index),
            "closed": n,
            "total_pnl": sum(pnls) if pnls else Decimal(0),
            "total_price_pnl": sum(price_pnls) if price_pnls else Decimal(0),
            "total_funding_pnl": sum(funding_pnls) if funding_pnls else Decimal(0),
            "wins": wins,
            "losses": n - wins,
            "win_rate": f"{wins/n*100:.0f}%" if n else "—",
        }

    def recent_closed(self, n: int = 10) -> list[LiveTrade]:
        closed = [t for t in self._trades.values() if t.status == "closed"]
        return sorted(closed, key=lambda t: t.closed_at or "", reverse=True)[:n]
