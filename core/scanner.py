import time
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import Optional

from exchanges.base import ExchangeBase, FundingRate
from utils import calc_net_profit, bid_ask_spread, minutes_to_funding, get_logger

log = get_logger("scanner")

# Default taker fees per exchange (fraction, e.g. 0.0005 = 0.05%)
DEFAULT_FEES = {
    "extended": Decimal("0.0005"),
    "nado": Decimal("0.0004"),
    "01": Decimal("0.0005"),
    "variational": Decimal("0.0004"),
}


@dataclass
class Opportunity:
    symbol: str
    short_exchange: str
    long_exchange: str
    short_rate: Decimal
    long_rate: Decimal
    spread: Decimal
    net_profit: Decimal
    short_price: Decimal
    long_price: Decimal
    short_bid: Decimal
    short_ask: Decimal
    long_bid: Decimal
    long_ask: Decimal
    minutes_to_funding: float
    short_balance: Decimal
    long_balance: Decimal
    fetched_at: float = field(default_factory=time.time)


class OpportunityScanner:
    def __init__(
        self,
        exchanges: list[ExchangeBase],
        min_spread: Decimal,
        min_net_profit: Decimal,
        min_minutes_to_funding: float,
        max_bid_ask_spread: Decimal,
        paper_mode: bool = False,
        margin_usd: Decimal = Decimal("0"),
        margin_buffer_usd: Decimal = Decimal("0"),
    ):
        self.exchanges = {ex.name: ex for ex in exchanges}
        self.min_spread = min_spread
        self.min_net_profit = min_net_profit
        self.min_minutes = min_minutes_to_funding
        self.max_ba_spread = max_bid_ask_spread
        self.paper_mode = paper_mode
        self.margin_usd = margin_usd
        self.margin_buffer_usd = margin_buffer_usd

    async def scan(self) -> list[Opportunity]:
        # Fetch all funding rates in parallel
        import asyncio
        results = await asyncio.gather(
            *[ex.get_all_funding_rates() for ex in self.exchanges.values()],
            return_exceptions=True,
        )

        # Build symbol → {exchange: FundingRate}
        by_symbol: dict[str, dict[str, FundingRate]] = {}
        for i, (name, ex) in enumerate(self.exchanges.items()):
            data = results[i]
            if isinstance(data, Exception):
                log.warning(f"{name} fetch failed: {data}")
                continue
            for fr in data:
                by_symbol.setdefault(fr.symbol, {})[name] = fr

        MAX_RATE_8H = Decimal("0.05")  # sanity cap: >5% per 8h is anomalous, skip

        opportunities = []
        for symbol, rates_by_ex in by_symbol.items():
            if len(rates_by_ex) < 2:
                continue
            # Try all pairs
            ex_names = list(rates_by_ex.keys())
            for a, b in combinations(ex_names, 2):
                ra, rb = rates_by_ex[a], rates_by_ex[b]
                # Skip if either exchange is read-only (can't trade)
                if self.exchanges[a].read_only or self.exchanges[b].read_only:
                    continue
                # Skip if either rate looks anomalous
                if abs(ra.rate) > MAX_RATE_8H or abs(rb.rate) > MAX_RATE_8H:
                    log.debug(f"{symbol}: rate out of sanity range ({ra.rate:.4f}, {rb.rate:.4f}), skip")
                    continue
                opp = self._evaluate(symbol, ra, rb)
                if opp:
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.net_profit, reverse=True)
        return opportunities

    def _evaluate(
        self, symbol: str, ra: FundingRate, rb: FundingRate
    ) -> Optional[Opportunity]:
        # Determine which is short (higher rate) and which is long (lower rate)
        if ra.rate >= rb.rate:
            short_fr, long_fr = ra, rb
        else:
            short_fr, long_fr = rb, ra

        spread = short_fr.rate - long_fr.rate
        if spread < self.min_spread:
            return None

        # Skip pairs where mark price is missing — can't calculate position size
        if short_fr.mark_price <= 0 or long_fr.mark_price <= 0:
            log.debug(f"{symbol}: mark price missing on {short_fr.exchange} or {long_fr.exchange}, skip")
            return None

        ba_short = bid_ask_spread(short_fr.bid, short_fr.ask)
        ba_long = bid_ask_spread(long_fr.bid, long_fr.ask)

        # In paper mode skip bid/ask check when data is unavailable (bid=ask=0)
        if not self.paper_mode or (short_fr.bid > 0 and long_fr.bid > 0):
            if ba_short > self.max_ba_spread or ba_long > self.max_ba_spread:
                log.debug(f"{symbol}: bid/ask spread too wide ({ba_short:.4%}, {ba_long:.4%})")
                return None

        net = calc_net_profit(
            funding_spread=spread,
            taker_fee_a=DEFAULT_FEES.get(short_fr.exchange, Decimal("0.0005")),
            taker_fee_b=DEFAULT_FEES.get(long_fr.exchange, Decimal("0.0005")),
            bid_ask_spread_a=ba_short,
            bid_ask_spread_b=ba_long,
        )
        if net < self.min_net_profit:
            return None

        mins = minutes_to_funding(short_fr.next_funding_ts)
        if mins < self.min_minutes:
            log.debug(f"{symbol}: only {mins:.1f} min to funding, skipping")
            return None

        if not self.paper_mode:
            min_required = (self.margin_usd + self.margin_buffer_usd) if self.margin_usd > 0 else Decimal("0")
            if short_fr.available_balance < min_required or long_fr.available_balance < min_required:
                log.debug(
                    f"{symbol}: insufficient account health/margin "
                    f"short={short_fr.available_balance} long={long_fr.available_balance} required={min_required}"
                )
                return None

        return Opportunity(
            symbol=symbol,
            short_exchange=short_fr.exchange,
            long_exchange=long_fr.exchange,
            short_rate=short_fr.rate,
            long_rate=long_fr.rate,
            spread=spread,
            net_profit=net,
            short_price=short_fr.mark_price,
            long_price=long_fr.mark_price,
            short_bid=short_fr.bid,
            short_ask=short_fr.ask,
            long_bid=long_fr.bid,
            long_ask=long_fr.ask,
            minutes_to_funding=mins,
            short_balance=short_fr.available_balance,
            long_balance=long_fr.available_balance,
        )
