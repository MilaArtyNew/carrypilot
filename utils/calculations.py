from decimal import Decimal, ROUND_DOWN


def calc_qty(margin_usd: Decimal, leverage: int, price: Decimal, qty_step: Decimal) -> Decimal:
    """Calculate position size rounded to exchange qty step."""
    notional = margin_usd * leverage
    raw_qty = notional / price
    steps = (raw_qty / qty_step).to_integral_value(rounding=ROUND_DOWN)
    return steps * qty_step


def calc_net_profit(
    funding_spread: Decimal,
    taker_fee_a: Decimal,
    taker_fee_b: Decimal,
    bid_ask_spread_a: Decimal,
    bid_ask_spread_b: Decimal,
    slippage_buffer: Decimal = Decimal("0.0002"),
) -> Decimal:
    fees = taker_fee_a + taker_fee_b
    spreads = bid_ask_spread_a + bid_ask_spread_b
    return funding_spread - fees - spreads - slippage_buffer


def bid_ask_spread(bid: Decimal, ask: Decimal) -> Decimal:
    if ask == 0:
        return Decimal(0)
    return (ask - bid) / ask


def minutes_to_funding(next_funding_ts: int) -> float:
    import time
    remaining = next_funding_ts / 1000 - time.time()
    return max(0.0, remaining / 60)
