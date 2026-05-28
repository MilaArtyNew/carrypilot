"""
Nado DEX on Ink L2 (EVM).
Auth: linked signer private key via nado-protocol SDK.
SDK is synchronous — wrapped with run_in_executor for async compatibility.
"""
import asyncio
import time
from decimal import Decimal
from functools import partial
from typing import Optional

from .base import ExchangeBase, FundingRate, OrderResult, Position

X18 = Decimal("1000000000000000000")  # 1e18

try:
    from nado_protocol.client import NadoClientMode, create_nado_client  # noqa: F401
    from nado_protocol.engine_client.types.execute import PlaceMarketOrderParams  # noqa: F401
    from nado_protocol.utils.execute import MarketOrderParams  # noqa: F401
    from nado_protocol.utils.subaccount import SubaccountParams  # noqa: F401
    from nado_protocol.utils.bytes32 import subaccount_to_hex  # noqa: F401
    _SDK_AVAILABLE = True
except ImportError:
    NadoClientMode = None  # type: ignore
    create_nado_client = None  # type: ignore
    PlaceMarketOrderParams = None  # type: ignore
    MarketOrderParams = None  # type: ignore
    SubaccountParams = None  # type: ignore
    subaccount_to_hex = None  # type: ignore
    _SDK_AVAILABLE = False


def _x18_to_decimal(val: str) -> Decimal:
    return Decimal(str(val)) / X18


class NadoExchange(ExchangeBase):
    name = "nado"
    read_only = False

    def __init__(self, private_key: str, wallet_address: str):
        if not _SDK_AVAILABLE:
            raise RuntimeError("nado-protocol not installed. Run: pip install nado-protocol")
        self._private_key = private_key
        self._wallet_address = wallet_address  # main wallet (subaccount owner)
        self._client = None
        self._product_map: dict[str, int] = {}   # symbol → product_id
        self._id_map: dict[int, str] = {}         # product_id → symbol
        self._loop = asyncio.get_event_loop()

    def _get_client(self):
        if self._client is None:
            self._client = create_nado_client(NadoClientMode.MAINNET, self._private_key)
        return self._client

    def _subaccount(self) -> str:
        return subaccount_to_hex(SubaccountParams(
            subaccount_owner=self._wallet_address,
            subaccount_name="default",
        ))

    async def _run(self, fn, *args, **kwargs):
        """Run a synchronous SDK call in a thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    async def _ensure_symbol_map(self):
        if self._product_map:
            return
        client = self._get_client()
        data = await self._run(client.market.get_all_product_symbols)
        symbols = data.symbols if hasattr(data, "symbols") else data
        for sym in symbols:
            # Symbol format: "BTC-PERP", "ETH-PERP", etc.
            base = sym.symbol.split("-")[0].upper()
            pid = int(sym.product_id)
            self._product_map[base] = pid
            self._id_map[pid] = base

    def _symbol_to_id(self, symbol: str) -> int:
        pid = self._product_map.get(symbol.upper())
        if pid is None:
            raise ValueError(f"Unknown symbol {symbol} on Nado")
        return pid

    async def _fetch_nado_prices(self, client, pid: int) -> tuple[Decimal, Decimal, Decimal]:
        """Fetch bid/ask/mark for one product in parallel-safe way."""
        try:
            prices, mark_data = await asyncio.gather(
                self._run(client.market.get_latest_market_price, pid),
                self._run(client.perp.get_prices, pid),
            )
            bid = _x18_to_decimal(prices.bid_x18)
            ask = _x18_to_decimal(prices.ask_x18)
            mark = _x18_to_decimal(mark_data.mark_price_x18)
            return bid, ask, mark
        except Exception:
            return Decimal(0), Decimal(0), Decimal(0)

    async def get_all_funding_rates(self) -> list[FundingRate]:
        await self._ensure_symbol_map()
        client = self._get_client()
        balance = await self.get_balance()

        product_ids = list(self._id_map.keys())
        rates_data = await self._run(client.market.get_perp_funding_rates, product_ids)

        # Fetch prices for all symbols in parallel instead of sequentially
        pid_list = [int(pid_str) for pid_str in rates_data.keys()]
        price_results = await asyncio.gather(
            *[self._fetch_nado_prices(client, pid) for pid in pid_list]
        )

        now = int(time.time())
        next_hour = ((now // 3600) + 1) * 3600
        results = []
        for (pid_str, fr), (bid, ask, mark) in zip(rates_data.items(), price_results):
            pid = int(pid_str)
            symbol = self._id_map.get(pid)
            if not symbol:
                continue
            rate = _x18_to_decimal(fr.funding_rate_x18)
            # Nado funds hourly — normalize to 8h
            rate_8h = rate * Decimal("8")
            results.append(FundingRate(
                exchange=self.name,
                symbol=symbol,
                rate=rate_8h,
                rate_raw=rate,
                interval_hours=1.0,
                next_funding_ts=next_hour * 1000,
                mark_price=mark,
                bid=bid,
                ask=ask,
                available_balance=balance,
            ))
        return results

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        rates = await self.get_all_funding_rates()
        for r in rates:
            if r.symbol == symbol:
                return r
        raise ValueError(f"Symbol {symbol} not found on Nado")

    async def get_balance(self) -> Decimal:
        client = self._get_client()
        subaccount = self._subaccount()
        data = await self._run(client.subaccount.get_engine_subaccount_summary, subaccount)
        # healths[0] = initial health = available for new positions
        if data.healths:
            return _x18_to_decimal(data.healths[0].health)
        return Decimal(0)

    async def get_qty_step(self, symbol: str) -> Decimal:
        await self._ensure_symbol_map()
        pid = self._symbol_to_id(symbol)
        client = self._get_client()
        data = await self._run(client.market.get_all_engine_markets)
        for p in data.perp_products:
            if p.product_id == pid:
                return _x18_to_decimal(p.book_info.size_increment)
        return Decimal("0.001")

    async def get_position(self, symbol: str) -> Optional[Position]:
        await self._ensure_symbol_map()
        pid = self._symbol_to_id(symbol)
        client = self._get_client()
        subaccount = self._subaccount()
        data = await self._run(client.subaccount.get_engine_subaccount_summary, subaccount)
        for pb in data.perp_balances:
            if pb.product_id == pid:
                qty_raw = _x18_to_decimal(pb.balance.amount)
                if qty_raw == 0:
                    return None
                side = "long" if qty_raw > 0 else "short"
                qty = abs(qty_raw)
                mark_data = await self._run(client.perp.get_prices, pid)
                mark = _x18_to_decimal(mark_data.mark_price_x18)
                # v_quote_balance is the notional entry value (negative = cost paid)
                vq = _x18_to_decimal(pb.balance.v_quote_balance)
                entry_price = abs(vq / qty) if qty != 0 else Decimal(0)
                pnl = vq + qty_raw * mark
                return Position(
                    exchange=self.name,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    entry_price=entry_price,
                    mark_price=mark,
                    unrealized_pnl=pnl,
                    margin=Decimal(0),       # cross-margin, no per-position margin
                    liquidation_price=Decimal(0),
                )
        return None

    async def place_market_order(self, symbol: str, side: str, qty: Decimal) -> OrderResult:
        await self._ensure_symbol_map()
        pid = self._symbol_to_id(symbol)
        client = self._get_client()
        # Positive amount = long (buy), negative = short (sell)
        amount_x18 = int(qty * X18) * (1 if side == "buy" else -1)
        params = PlaceMarketOrderParams(
            product_id=pid,
            market_order=MarketOrderParams(
                sender=SubaccountParams(
                    subaccount_owner=self._wallet_address,
                    subaccount_name="default",
                ),
                amount=amount_x18,
            ),
        )
        try:
            result = await self._run(client.market.place_market_order, params)
            ok = result.status == "success"
            return OrderResult(
                exchange=self.name, symbol=symbol,
                order_id=getattr(result.data, "digest", "") if result.data else "",
                side=side, qty=qty,
                price=Decimal(0),  # fill price not returned directly
                status="filled" if ok else "failed",
                error=None if ok else str(result),
            )
        except Exception as e:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error=str(e))

    async def close_position(self, symbol: str) -> OrderResult:
        pos = await self.get_position(symbol)
        if not pos:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side="", qty=Decimal(0), price=Decimal(0),
                               status="failed", error="No open position")
        close_side = "sell" if pos.side == "long" else "buy"
        return await self.place_market_order(symbol, close_side, pos.qty)

    async def ping(self) -> float:
        client = self._get_client()
        t0 = time.monotonic()
        await self._run(client.market.get_all_product_symbols)
        return (time.monotonic() - t0) * 1000
