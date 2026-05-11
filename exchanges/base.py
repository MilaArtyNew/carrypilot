from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class FundingRate:
    exchange: str
    symbol: str
    rate: Decimal           # normalized: rate per 8h equivalent (fraction, e.g. 0.0008 = 0.08%)
    rate_raw: Decimal       # raw rate as returned by exchange API
    interval_hours: float   # exchange's native funding interval in hours
    next_funding_ts: int    # unix timestamp ms
    mark_price: Decimal
    bid: Decimal
    ask: Decimal
    available_balance: Decimal

    @property
    def rate_per_hour(self) -> Decimal:
        if self.interval_hours == 0:
            return Decimal(0)
        return self.rate_raw / Decimal(str(self.interval_hours))

    def normalize_to_8h(self) -> Decimal:
        """Convert raw rate to 8-hour equivalent for cross-exchange comparison."""
        return self.rate_per_hour * Decimal("8")


@dataclass
class Position:
    exchange: str
    symbol: str
    side: str               # "long" | "short"
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    margin: Decimal
    liquidation_price: Decimal


@dataclass
class OrderResult:
    exchange: str
    symbol: str
    order_id: str
    side: str
    qty: Decimal
    price: Decimal
    status: str             # "filled" | "partial" | "failed"
    error: Optional[str] = None


class ExchangeBase(ABC):
    name: str
    read_only: bool = False

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """Current funding rate + market data for symbol."""

    @abstractmethod
    async def get_all_funding_rates(self) -> list[FundingRate]:
        """All available perpetual markets."""

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Open position for symbol, None if no position."""

    @abstractmethod
    async def get_balance(self) -> Decimal:
        """Available margin balance in USD."""

    @abstractmethod
    async def get_qty_step(self, symbol: str) -> Decimal:
        """Minimum qty increment for symbol."""

    async def get_min_qty(self, symbol: str) -> Decimal:
        """Minimum order size for symbol. Override if exchange enforces it."""
        return Decimal(0)

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: str, qty: Decimal
    ) -> OrderResult:
        """Place market order. side: 'buy' | 'sell'."""

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        """Close open position for symbol."""

    @abstractmethod
    async def ping(self) -> float:
        """Health check. Returns latency in ms, raises on failure."""
