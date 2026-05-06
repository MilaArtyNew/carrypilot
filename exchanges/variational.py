"""
Variational Omni — Arbitrum DEX.
READ-ONLY: trading API not yet available.
Only used for funding rate monitoring and signal detection.
"""
import time
from decimal import Decimal

from typing import Optional

import aiohttp

from .base import ExchangeBase, FundingRate, OrderResult, Position

# Only public endpoint available
STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"


class VariationalExchange(ExchangeBase):
    name = "variational"
    read_only = True

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_all_funding_rates(self) -> list[FundingRate]:
        session = await self._session_()
        async with session.get(STATS_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            data = await r.json()

        now = int(time.time())
        results = []
        for item in data.get("listings", []):
            symbol = item.get("ticker", "").upper()
            if not symbol:
                continue

            # funding_rate is already a decimal fraction (e.g. 0.037347 = 3.73%)
            rate = Decimal(str(item.get("funding_rate", 0)))

            interval_s = int(item.get("funding_interval_s", 28800))
            next_ts = ((now // interval_s) + 1) * interval_s * 1000  # ms

            mark = Decimal(str(item.get("mark_price", 0)))
            quotes = item.get("quotes", {}).get("base", {})
            bid = Decimal(str(quotes.get("bid", 0)))
            ask = Decimal(str(quotes.get("ask", 0)))

            interval_hours = interval_s / 3600
            rate_8h = rate * Decimal("8") / Decimal(str(interval_hours))
            results.append(FundingRate(
                exchange=self.name,
                symbol=symbol,
                rate=rate_8h,
                rate_raw=rate,
                interval_hours=interval_hours,
                next_funding_ts=next_ts,
                mark_price=mark,
                bid=bid,
                ask=ask,
                available_balance=Decimal(0),
            ))
        return results

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        rates = await self.get_all_funding_rates()
        for r in rates:
            if r.symbol == symbol:
                return r
        raise ValueError(f"Symbol {symbol} not found on Variational")

    # --- Not supported (read-only) ---

    async def get_balance(self) -> Decimal:
        raise NotImplementedError("Variational is read-only")

    async def get_qty_step(self, symbol: str) -> Decimal:
        raise NotImplementedError("Variational is read-only")

    async def get_position(self, symbol: str) -> Optional[Position]:
        raise NotImplementedError("Variational is read-only")

    async def place_market_order(self, symbol: str, side: str, qty: Decimal) -> OrderResult:
        raise NotImplementedError("Variational trading API not yet available")

    async def close_position(self, symbol: str) -> OrderResult:
        raise NotImplementedError("Variational is read-only")

    async def ping(self) -> float:
        t0 = time.monotonic()
        session = await self._session_()
        async with session.get(STATS_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
        return (time.monotonic() - t0) * 1000

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
