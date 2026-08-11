"""
Tracks open delta-neutral pairs and their combined PnL/state.
In paper mode: calculates unrealized PnL from current mark prices without calling get_position().
"""
import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from exchanges.base import ExchangeBase, Position
from utils import get_logger

log = get_logger("position_tracker")


@dataclass
class RestoreReport:
    """Result of the startup scan — tells the caller whether it is safe to reconcile the ledger."""
    hedged: list[str] = field(default_factory=list)
    unhedged: dict[str, dict[str, Position]] = field(default_factory=dict)
    failed_exchanges: list[str] = field(default_factory=list)

    @property
    def scan_complete(self) -> bool:
        """True only if every exchange answered for every symbol. If not, a missing position
        may be an API failure rather than a real absence — never reconcile on a partial scan."""
        return not self.failed_exchanges

    @property
    def live_symbols(self) -> set[str]:
        return set(self.hedged) | set(self.unhedged)


@dataclass
class PairState:
    symbol: str
    short_exchange: str
    long_exchange: str
    qty: Decimal
    short_entry: Decimal
    long_entry: Decimal
    short_pos: Optional[Position] = None
    long_pos: Optional[Position] = None
    opened_at: float = field(default_factory=time.time)  # unix timestamp

    @property
    def age_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600

    @property
    def combined_pnl(self) -> Decimal:
        short_pnl = self.short_pos.unrealized_pnl if self.short_pos else Decimal(0)
        long_pnl = self.long_pos.unrealized_pnl if self.long_pos else Decimal(0)
        return short_pnl + long_pnl

    @property
    def is_healthy(self) -> bool:
        return self.short_pos is not None and self.long_pos is not None


class PositionTracker:
    def __init__(self, exchanges: dict[str, ExchangeBase], paper_ledger=None):
        self.exchanges = exchanges
        self.paper_ledger = paper_ledger
        self._pairs: dict[str, PairState] = {}
        # "symbol:exchange" → consecutive cycle count where API returned None
        self._missing_counts: dict[str, int] = {}
        # symbol → {exchange → position} for legs found without a hedge on startup
        self._unhedged: dict[str, dict[str, Position]] = {}

    def add_pair(self, state: PairState):
        self._pairs[state.symbol] = state
        log.info(f"Tracking: {state.symbol} ({state.short_exchange}/{state.long_exchange})")

    def remove_pair(self, symbol: str):
        self._pairs.pop(symbol, None)
        log.info(f"Stopped tracking: {symbol}")

    def get_all(self) -> list[PairState]:
        return list(self._pairs.values())

    def get(self, symbol: str) -> Optional[PairState]:
        return self._pairs.get(symbol)

    def get_unhedged(self) -> dict[str, dict[str, Position]]:
        """symbol → {exchange → position} for naked legs seen at startup."""
        return dict(self._unhedged)

    def get_unhedged_leg(self, symbol: str) -> Optional[tuple[str, Position]]:
        """(exchange, position) of a naked leg, or None. Only meaningful for single-leg leftovers."""
        legs = self._unhedged.get(symbol)
        if not legs:
            return None
        return next(iter(legs.items()))

    def clear_unhedged(self, symbol: str):
        if self._unhedged.pop(symbol, None) is not None:
            log.info(f"Unhedged leg cleared: {symbol}")

    async def refresh(self):
        if self.paper_ledger is not None:
            await self._refresh_paper()
        else:
            await self._refresh_live()

    async def _refresh_paper(self):
        for symbol, pair in list(self._pairs.items()):
            trade = self.paper_ledger.get_open(symbol)
            if not trade:
                continue
            try:
                short_ex = self.exchanges.get(pair.short_exchange)
                long_ex = self.exchanges.get(pair.long_exchange)
                short_fr, long_fr = await asyncio.gather(
                    short_ex.get_funding_rate(symbol),
                    long_ex.get_funding_rate(symbol),
                )
                qty = Decimal(trade.qty)
                short_mark = short_fr.mark_price
                long_mark = long_fr.mark_price
                pair.short_pos = Position(
                    exchange=pair.short_exchange, symbol=symbol, side="short",
                    qty=qty, entry_price=Decimal(trade.short_entry), mark_price=short_mark,
                    unrealized_pnl=(Decimal(trade.short_entry) - short_mark) * qty,
                    margin=Decimal(0), liquidation_price=Decimal(0),
                )
                pair.long_pos = Position(
                    exchange=pair.long_exchange, symbol=symbol, side="long",
                    qty=qty, entry_price=Decimal(trade.long_entry), mark_price=long_mark,
                    unrealized_pnl=(long_mark - Decimal(trade.long_entry)) * qty,
                    margin=Decimal(0), liquidation_price=Decimal(0),
                )
            except Exception as e:
                # Keep existing positions on refresh failure — don't null them out
                log.warning(f"Paper refresh {symbol} failed (keeping last state): {e}")

    async def restore_from_exchanges(self) -> RestoreReport:
        """On startup: query all exchanges and rebuild tracker from open positions."""
        report = RestoreReport()
        # symbol → {exchange_name → position}
        by_symbol: dict[str, dict[str, "Position"]] = {}
        for name, ex in self.exchanges.items():
            if ex.read_only:
                continue
            try:
                # Try to get positions for all known symbols
                from exchanges.base import ExchangeBase
                symbols = getattr(ex, "_product_map", None)
                if symbols is not None:
                    await ex._ensure_symbol_map()
                    check_syms = list(ex._product_map.keys()) if hasattr(ex, "_product_map") else []
                else:
                    import sys
                    ex_module = sys.modules.get(type(ex).__module__)
                    ex_map = getattr(ex_module, "SYMBOL_MAP", None)
                    if ex_map:
                        check_syms = list(ex_map.keys())
                    else:
                        from exchanges.kraken import SYMBOL_MAP as KRAKEN_MAP
                        check_syms = list(KRAKEN_MAP.keys())
                errors = 0
                for sym in check_syms:
                    try:
                        pos = await ex.get_position(sym)
                        if pos:
                            by_symbol.setdefault(sym, {})[name] = pos
                    except Exception:
                        errors += 1
                if errors:
                    # Partial scan: some symbols never answered, so "no position" is not trustworthy
                    log.warning(f"Restore: {name} failed on {errors}/{len(check_syms)} symbols")
                    report.failed_exchanges.append(name)
            except Exception as e:
                log.warning(f"Restore: error scanning {name}: {e}")
                report.failed_exchanges.append(name)

        for symbol, positions in by_symbol.items():
            shorts = {n: p for n, p in positions.items() if p.side == "short"}
            longs  = {n: p for n, p in positions.items() if p.side == "long"}
            if not shorts or not longs:
                log.warning(f"Restore: {symbol} has unhedged position — {positions}")
                self._unhedged[symbol] = positions
                report.unhedged[symbol] = positions
                continue
            short_name, short_pos = next(iter(shorts.items()))
            long_name,  long_pos  = next(iter(longs.items()))
            state = PairState(
                symbol=symbol,
                short_exchange=short_name,
                long_exchange=long_name,
                qty=short_pos.qty,
                short_entry=short_pos.entry_price,
                long_entry=long_pos.entry_price,
                short_pos=short_pos,
                long_pos=long_pos,
            )
            self.add_pair(state)
            report.hedged.append(symbol)
            log.info(f"Restored live position: {symbol} SHORT={short_name} LONG={long_name}")

        return report

    async def _refresh_live(self):
        for symbol, pair in list(self._pairs.items()):
            short_ex = self.exchanges.get(pair.short_exchange)
            long_ex = self.exchanges.get(pair.long_exchange)
            if not short_ex or not long_ex:
                continue
            try:
                short_pos, long_pos = await asyncio.gather(
                    short_ex.get_position(symbol),
                    long_ex.get_position(symbol),
                    return_exceptions=True,
                )
                # Exception → API error, keep last known state (don't null the leg)
                if isinstance(short_pos, Exception):
                    log.warning(f"{symbol} short refresh error (keeping last state): {short_pos}")
                    short_pos = pair.short_pos
                if isinstance(long_pos, Exception):
                    log.warning(f"{symbol} long refresh error (keeping last state): {long_pos}")
                    long_pos = pair.long_pos

                # None → position not found on exchange.
                # Require 2 consecutive None before treating as truly gone
                # (guards against transient API glitches on 01/Kraken).
                short_pos = self._guard_none(symbol, pair.short_exchange, short_pos, pair.short_pos)
                long_pos  = self._guard_none(symbol, pair.long_exchange,  long_pos,  pair.long_pos)

                pair.short_pos = short_pos
                pair.long_pos = long_pos
                if not pair.is_healthy:
                    log.error(f"ALERT: {symbol} pair is unhealthy! short={short_pos} long={long_pos}")
            except Exception as e:
                log.error(f"Error refreshing {symbol}: {e}")

    def _guard_none(
        self,
        symbol: str,
        exchange: str,
        new_pos: Optional[Position],
        last_pos: Optional[Position],
    ) -> Optional[Position]:
        """Return None only after 2 consecutive None results from the exchange API."""
        key = f"{symbol}:{exchange}"
        if new_pos is None:
            count = self._missing_counts.get(key, 0) + 1
            self._missing_counts[key] = count
            if count < 2:
                log.warning(f"{symbol} {exchange}: position not found (attempt {count}/2), keeping last state")
                return last_pos  # keep last known state for this cycle
            log.warning(f"{symbol} {exchange}: position not found 2 cycles in a row — treating as gone")
            return None
        else:
            self._missing_counts.pop(key, None)  # reset counter when position is found
            return new_pos
