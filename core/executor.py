"""
TradeExecutor — opens and closes delta-neutral pairs.
In paper mode: simulates fills at current mark price, no real orders placed.
Critical rule (live only): if one leg fails, immediately close the other.
"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from exchanges.base import ExchangeBase, OrderResult
from core.scanner import Opportunity
from utils import calc_qty, get_logger

log = get_logger("executor")


@dataclass
class TradeResult:
    success: bool
    symbol: str
    short_exchange: str
    long_exchange: str
    short_order: Optional[OrderResult]
    long_order: Optional[OrderResult]
    qty: Decimal
    error: Optional[str] = None


class TradeExecutor:
    def __init__(
        self,
        exchanges: dict[str, ExchangeBase],
        margin_usd: Decimal,
        leverage: int,
        paper_ledger=None,
    ):
        self.exchanges = exchanges
        self.margin_usd = margin_usd
        self.leverage = leverage
        self.paper_ledger = paper_ledger

    @property
    def paper_mode(self) -> bool:
        return self.paper_ledger is not None

    async def open_pair(self, opp: Opportunity) -> TradeResult:
        if self.paper_mode:
            return await self._paper_open(opp)
        return await self._live_open(opp)

    async def close_pair(self, symbol: str, short_exchange: str, long_exchange: str) -> TradeResult:
        if self.paper_mode:
            return await self._paper_close(symbol, short_exchange, long_exchange)
        return await self._live_close(symbol, short_exchange, long_exchange)

    # ── Paper mode ────────────────────────────────────────────────────────────

    async def _paper_open(self, opp: Opportunity) -> TradeResult:
        short_ex = self.exchanges[opp.short_exchange]
        long_ex = self.exchanges[opp.long_exchange]

        short_price = opp.short_price or opp.short_ask
        long_price = opp.long_price or opp.long_bid
        avg_price = (short_price + long_price) / 2

        step_short = await short_ex.get_qty_step(opp.symbol)
        step_long = await long_ex.get_qty_step(opp.symbol)
        # Paper mode: use min step — we simulate fills, not bound by exchange lot sizes
        qty = calc_qty(self.margin_usd, self.leverage, avg_price, min(step_short, step_long))

        if qty <= 0:
            return TradeResult(
                success=False, symbol=opp.symbol,
                short_exchange=opp.short_exchange, long_exchange=opp.long_exchange,
                short_order=None, long_order=None, qty=qty,
                error="Qty is zero — проверь маржу и цену",
            )

        trade = self.paper_ledger.open(opp, qty, short_price, long_price)
        fake_order = lambda ex, side, price: OrderResult(
            exchange=ex, symbol=opp.symbol, order_id=trade.trade_id,
            side=side, qty=qty, price=price, status="filled",
        )
        return TradeResult(
            success=True, symbol=opp.symbol,
            short_exchange=opp.short_exchange, long_exchange=opp.long_exchange,
            short_order=fake_order(opp.short_exchange, "sell", short_price),
            long_order=fake_order(opp.long_exchange, "buy", long_price),
            qty=qty,
        )

    async def _paper_close(self, symbol: str, short_exchange: str, long_exchange: str) -> TradeResult:
        trade = self.paper_ledger.get_open(symbol)
        if not trade:
            return TradeResult(
                success=False, symbol=symbol,
                short_exchange=short_exchange, long_exchange=long_exchange,
                short_order=None, long_order=None, qty=Decimal(0),
                error="No open paper position",
            )

        # Get current mark prices for both exchanges
        short_ex = self.exchanges[short_exchange]
        long_ex = self.exchanges[long_exchange]
        try:
            short_fr, long_fr = await asyncio.gather(
                short_ex.get_funding_rate(symbol),
                long_ex.get_funding_rate(symbol),
            )
            short_close = short_fr.mark_price or short_fr.bid
            long_close = long_fr.mark_price or long_fr.ask
        except Exception as e:
            return TradeResult(
                success=False, symbol=symbol,
                short_exchange=short_exchange, long_exchange=long_exchange,
                short_order=None, long_order=None, qty=Decimal(0),
                error=f"Could not get close prices: {e}",
            )

        closed = self.paper_ledger.close(symbol, short_close, long_close)
        qty = Decimal(trade.qty)
        fake_order = lambda ex, side, price: OrderResult(
            exchange=ex, symbol=symbol, order_id=trade.trade_id,
            side=side, qty=qty, price=price, status="filled",
        )
        return TradeResult(
            success=True, symbol=symbol,
            short_exchange=short_exchange, long_exchange=long_exchange,
            short_order=fake_order(short_exchange, "buy", short_close),
            long_order=fake_order(long_exchange, "sell", long_close),
            qty=qty,
        )

    # ── Live mode ─────────────────────────────────────────────────────────────

    async def _live_open(self, opp: Opportunity) -> TradeResult:
        short_ex = self.exchanges[opp.short_exchange]
        long_ex = self.exchanges[opp.long_exchange]

        # Use max price so both legs get equal token qty and neither side exceeds notional
        max_price = max(opp.short_price, opp.long_price)
        step_short = await short_ex.get_qty_step(opp.symbol)
        step_long = await long_ex.get_qty_step(opp.symbol)
        qty = calc_qty(self.margin_usd, self.leverage, max_price, max(step_short, step_long))

        if qty <= 0:
            return TradeResult(
                success=False, symbol=opp.symbol,
                short_exchange=opp.short_exchange, long_exchange=opp.long_exchange,
                short_order=None, long_order=None, qty=qty,
                error="Calculated qty is zero — check margin and price",
            )

        log.info(f"Opening {opp.symbol}: SHORT {opp.short_exchange} / LONG {opp.long_exchange} | qty={qty}")

        short_task = short_ex.place_market_order(opp.symbol, "sell", qty)
        long_task = long_ex.place_market_order(opp.symbol, "buy", qty)
        short_result, long_result = await asyncio.gather(short_task, long_task, return_exceptions=True)

        if isinstance(short_result, Exception):
            short_result = OrderResult(
                exchange=opp.short_exchange, symbol=opp.symbol, order_id="",
                side="sell", qty=qty, price=Decimal(0), status="failed", error=str(short_result),
            )
        if isinstance(long_result, Exception):
            long_result = OrderResult(
                exchange=opp.long_exchange, symbol=opp.symbol, order_id="",
                side="buy", qty=qty, price=Decimal(0), status="failed", error=str(long_result),
            )

        short_ok = short_result.status == "filled"
        long_ok = long_result.status == "filled"

        if short_ok and long_ok:
            log.info(f"{opp.symbol} pair opened | qty={qty}")
            return TradeResult(
                success=True, symbol=opp.symbol,
                short_exchange=opp.short_exchange, long_exchange=opp.long_exchange,
                short_order=short_result, long_order=long_result, qty=qty,
            )

        log.error(f"EMERGENCY: {opp.symbol} one leg failed. short_ok={short_ok} long_ok={long_ok}")
        await self._emergency_close(opp.symbol, short_ex, long_ex, short_ok, long_ok)

        error = []
        if not short_ok:
            error.append(f"SHORT failed: {short_result.error}")
        if not long_ok:
            error.append(f"LONG failed: {long_result.error}")

        return TradeResult(
            success=False, symbol=opp.symbol,
            short_exchange=opp.short_exchange, long_exchange=opp.long_exchange,
            short_order=short_result, long_order=long_result, qty=qty,
            error=" | ".join(error),
        )

    async def _emergency_close(
        self, symbol, short_ex, long_ex, short_was_opened, long_was_opened
    ):
        tasks = []
        if short_was_opened:
            tasks.append(short_ex.close_position(symbol))
        if long_was_opened:
            tasks.append(long_ex.close_position(symbol))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error(f"Emergency close failed: {r}")

    async def _live_close(self, symbol: str, short_exchange: str, long_exchange: str) -> TradeResult:
        short_ex = self.exchanges[short_exchange]
        long_ex = self.exchanges[long_exchange]

        log.info(f"Closing {symbol}: {short_exchange} + {long_exchange}")
        short_task = short_ex.close_position(symbol)
        long_task = long_ex.close_position(symbol)
        short_result, long_result = await asyncio.gather(short_task, long_task, return_exceptions=True)

        if isinstance(short_result, Exception):
            short_result = OrderResult(
                exchange=short_exchange, symbol=symbol, order_id="",
                side="", qty=Decimal(0), price=Decimal(0), status="failed", error=str(short_result),
            )
        if isinstance(long_result, Exception):
            long_result = OrderResult(
                exchange=long_exchange, symbol=symbol, order_id="",
                side="", qty=Decimal(0), price=Decimal(0), status="failed", error=str(long_result),
            )

        success = short_result.status == "filled" and long_result.status == "filled"
        return TradeResult(
            success=success, symbol=symbol,
            short_exchange=short_exchange, long_exchange=long_exchange,
            short_order=short_result, long_order=long_result,
            qty=short_result.qty,
            error=None if success else f"short={short_result.error} long={long_result.error}",
        )
