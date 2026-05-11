"""
Paper trading ledger — simulates trades, persists to JSON.
No real orders are placed. Prices taken from exchange mark prices at execution time.
PnL = price delta (delta-neutral ≈ 0) + funding payments (spread × notional × funding_events).
"""
import json
import time
from dataclasses import dataclass, asdict
from decimal import Decimal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_IL_TZ = ZoneInfo("Asia/Jerusalem")
_TS_FMT = "%Y-%m-%d %H:%M:%S %Z"
from pathlib import Path
from typing import Optional

from core.scanner import Opportunity
from utils import get_logger

log = get_logger("paper")

LOG_FILE = Path("/home/gpt/funding-arb-bot/paper_trades.json")

FUNDING_INTERVAL_HOURS = Decimal("8")  # standard 8h funding cycle


@dataclass
class PaperTrade:
    trade_id: str
    symbol: str
    short_exchange: str
    long_exchange: str
    qty: str
    short_entry: str
    long_entry: str
    spread_at_open: str
    opened_at: str
    status: str            # "open" | "closed"
    closed_at: Optional[str] = None
    short_close: Optional[str] = None
    long_close: Optional[str] = None
    price_pnl: Optional[str] = None    # USD: pure delta movement
    funding_pnl: Optional[str] = None  # USD: simulated funding payments
    realized_pnl: Optional[str] = None # USD: price_pnl + funding_pnl


class PaperLedger:
    def __init__(self, log_file: Path = LOG_FILE):
        self._log_file = log_file
        self._trades: dict[str, PaperTrade] = {}
        self._open_index: dict[str, str] = {}   # symbol → trade_id
        self._load()

    def _load(self):
        if not self._log_file.exists():
            return
        try:
            for t in json.loads(self._log_file.read_text()):
                # Tolerate old records that lack new fields
                t.setdefault("price_pnl", t.get("realized_pnl"))
                t.setdefault("funding_pnl", None)
                trade = PaperTrade(**t)
                self._trades[trade.trade_id] = trade
                if trade.status == "open":
                    # If duplicate open for same symbol — keep the latest (by trade_id timestamp)
                    existing_id = self._open_index.get(trade.symbol)
                    if existing_id and existing_id > trade.trade_id:
                        # existing is newer — close the older zombie
                        self._trades[trade.trade_id].status = "closed"
                        self._trades[trade.trade_id].closed_at = "zombie — duplicate open on load"
                        log.warning(f"Zombie closed on load: {trade.trade_id}")
                    else:
                        if existing_id:
                            # this one is newer — close the previous zombie
                            self._trades[existing_id].status = "closed"
                            self._trades[existing_id].closed_at = "zombie — duplicate open on load"
                            log.warning(f"Zombie closed on load: {existing_id}")
                        self._open_index[trade.symbol] = trade.trade_id
            log.info(f"Loaded {len(self._trades)} paper trades ({len(self._open_index)} open)")
            self._save()  # persist zombie fixes + new field defaults
        except Exception as e:
            log.warning(f"Could not load paper trades: {e}")

    def _save(self):
        data = [asdict(t) for t in self._trades.values()]
        self._log_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def open(self, opp: Opportunity, qty: Decimal, short_price: Decimal, long_price: Decimal) -> PaperTrade:
        trade_id = f"{opp.symbol}-{int(time.time())}"
        trade = PaperTrade(
            trade_id=trade_id,
            symbol=opp.symbol,
            short_exchange=opp.short_exchange,
            long_exchange=opp.long_exchange,
            qty=str(qty),
            short_entry=str(short_price),
            long_entry=str(long_price),
            spread_at_open=str(opp.spread),
            opened_at=datetime.now(_IL_TZ).strftime(_TS_FMT),
            status="open",
        )
        self._trades[trade_id] = trade
        self._open_index[opp.symbol] = trade_id
        self._save()
        log.info(f"[PAPER] Opened {opp.symbol} qty={qty} short@{short_price:.2f} long@{long_price:.2f}")
        return trade

    def close(self, symbol: str, short_close: Decimal, long_close: Decimal) -> Optional[PaperTrade]:
        trade_id = self._open_index.pop(symbol, None)
        if not trade_id:
            return None
        trade = self._trades[trade_id]
        qty = Decimal(trade.qty)

        # Price delta (delta-neutral ≈ 0, residual from slippage/divergence)
        price_pnl = (Decimal(trade.short_entry) - short_close) * qty \
                  + (long_close - Decimal(trade.long_entry)) * qty

        # Funding payments: spread × notional × funding_events
        # notional = qty × avg_entry_price
        avg_entry = (Decimal(trade.short_entry) + Decimal(trade.long_entry)) / 2
        notional = qty * avg_entry
        hours_held = self._hours_since(trade.opened_at)
        funding_events = Decimal(str(hours_held)) / FUNDING_INTERVAL_HOURS
        funding_pnl = Decimal(trade.spread_at_open) * notional * funding_events

        total_pnl = price_pnl + funding_pnl

        trade.status = "closed"
        trade.closed_at = datetime.now(_IL_TZ).strftime(_TS_FMT)
        trade.short_close = str(short_close)
        trade.long_close = str(long_close)
        trade.price_pnl = str(round(price_pnl, 6))
        trade.funding_pnl = str(round(funding_pnl, 6))
        trade.realized_pnl = str(round(total_pnl, 6))
        self._save()
        log.info(
            f"[PAPER] Closed {symbol} "
            f"price_pnl={price_pnl:.4f} funding_pnl={funding_pnl:.4f} "
            f"({hours_held:.1f}h × {Decimal(trade.spread_at_open):.4%} spread) "
            f"total={total_pnl:.4f} USD"
        )
        return trade

    @staticmethod
    def _hours_since(opened_at_str: str) -> float:
        try:
            for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S UTC"):
                try:
                    opened = datetime.strptime(opened_at_str, fmt)
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    return (datetime.now(timezone.utc) - opened).total_seconds() / 3600
                except ValueError:
                    continue
        except Exception:
            pass
        return 0.0

    def get_open(self, symbol: str) -> Optional[PaperTrade]:
        tid = self._open_index.get(symbol)
        return self._trades.get(tid) if tid else None

    def get_all_open(self) -> list[PaperTrade]:
        return [self._trades[tid] for tid in self._open_index.values() if tid in self._trades]

    def get_all_closed(self) -> list[PaperTrade]:
        return [t for t in self._trades.values() if t.status == "closed"]

    def stats(self) -> dict:
        closed = self.get_all_closed()
        real = [t for t in closed if t.realized_pnl and "zombie" not in (t.closed_at or "")]
        pnls = [Decimal(t.realized_pnl) for t in real]
        price_pnls = [Decimal(t.price_pnl) for t in real if t.price_pnl]
        funding_pnls = [Decimal(t.funding_pnl) for t in real if t.funding_pnl]
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        n = len(pnls)
        return {
            "open": len(self._open_index),
            "closed": n,
            "total_pnl": total_pnl,
            "total_price_pnl": sum(price_pnls),
            "total_funding_pnl": sum(funding_pnls),
            "wins": wins,
            "losses": n - wins,
            "win_rate": f"{wins/n*100:.0f}%" if n else "—",
        }
