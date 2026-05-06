import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import aiohttp

from .base import ExchangeBase, FundingRate, OrderResult, Position

BASE_URL  = "https://futures.kraken.com/derivatives/api/v3"
_API_PATH = "/api/v3"  # path used in signature (strip /derivatives from full URI)

# Kraken perp symbol map: our internal name → Kraken symbol
# ETH excluded: Kraken requires ETH collateral for PF_ETHUSD, account holds USDT only
SYMBOL_MAP = {
    "BTC":      "PF_XBTUSD",
    "SOL":      "PF_SOLUSD",
    "XRP":      "PF_XRPUSD",
    "DOGE":     "PF_DOGEUSD",
    "ADA":      "PF_ADAUSD",
    "AVAX":     "PF_AVAXUSD",
    "LINK":     "PF_LINKUSD",
    # Added: overlap with Nado
    "SUI":      "PF_SUIUSD",
    "NEAR":     "PF_NEARUSD",
    "ARB":      "PF_ARBUSD",
    "BNB":      "PF_BNBUSD",
    "LTC":      "PF_LTCUSD",
    "BCH":      "PF_BCHUSD",
    "AAVE":     "PF_AAVEUSD",
    "UNI":      "PF_UNIUSD",
    "ENA":      "PF_ENAUSD",
    "JUP":      "PF_JUPUSD",
    "HYPE":     "PF_HYPEUSD",
    "ONDO":     "PF_ONDOUSD",
    "TAO":      "PF_TAOUSD",
    "PENGU":    "PF_PENGUUSD",
    "AXS":      "PF_AXSUSD",
    "BERA":     "PF_BERAUSD",
    "VIRTUAL":  "PF_VIRTUALUSD",
    "FARTCOIN": "PF_FARTCOINUSD",
    "WLFI":     "PF_WLFIUSD",
    "XMR":      "PF_XMRUSD",
    "ZEC":      "PF_ZECUSD",
    "ZRO":      "PF_ZROUSD",
    # Added: overlap with 01.xyz
    "APT":      "PF_APTUSD",
}
REVERSE_MAP = {v: k for k, v in SYMBOL_MAP.items()}


def _nonce() -> str:
    return str(int(time.time() * 100_000_000))  # 100-nanosecond precision (Kraken Futures requirement)


def _sign(api_secret: str, sign_endpoint: str, post_data: str, nonce: str) -> str:
    # sign_endpoint is /api/v3/... (full path minus /derivatives)
    message = (post_data + nonce + sign_endpoint).encode("utf-8")
    hashed = hashlib.sha256(message).digest()
    secret_bytes = base64.b64decode(api_secret)
    return base64.b64encode(hmac.new(secret_bytes, hashed, hashlib.sha512).digest()).decode()


class KrakenExchange(ExchangeBase):
    name = "kraken"
    read_only = False

    def __init__(self, api_key: str, api_secret: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._precision_cache: dict[str, int] = {}  # kraken_sym → contractValueTradePrecision

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(self, path: str, params: dict = None) -> dict:
        session = await self._session_()
        url = BASE_URL + path
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def _post_auth(self, path: str, data: dict = None) -> dict:
        session = await self._session_()
        data = data or {}
        post_data = urllib.parse.urlencode(data)
        nonce = _nonce()
        signature = _sign(self._api_secret, _API_PATH + path, post_data, nonce)
        headers = {
            "APIKey": self._api_key,
            "Nonce": nonce,
            "Authent": signature,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        url = BASE_URL + path
        async with session.post(url, data=post_data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def _get_auth(self, path: str, params: dict = None) -> dict:
        session = await self._session_()
        query = urllib.parse.urlencode(params) if params else ""
        nonce = _nonce()
        signature = _sign(self._api_secret, _API_PATH + path, query, nonce)
        headers = {
            "APIKey": self._api_key,
            "Nonce": nonce,
            "Authent": signature,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        url = BASE_URL + path
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def get_all_funding_rates(self) -> list[FundingRate]:
        data = await self._get("/tickers")
        balance = await self.get_balance()
        results = []
        for t in data.get("tickers", []):
            symbol_raw = t.get("symbol", "")
            if symbol_raw not in REVERSE_MAP:
                continue
            symbol = REVERSE_MAP[symbol_raw]
            rate_raw = Decimal(str(t.get("fundingRate", 0)))
            # Kraken funds hourly (fundingRateCoefficient=24), normalize to 8h
            interval_hours = 1.0
            rate_8h = rate_raw * Decimal("8")
            next_ts = int(t.get("nextFundingRateTime", 0))
            if next_ts == 0:
                # Kraken sometimes omits this — approximate next hour boundary
                next_ts = (((int(time.time()) // 3600) + 1) * 3600) * 1000
            mark = Decimal(str(t.get("markPrice") or t.get("last", 0)))
            bid = Decimal(str(t.get("bid", 0)))
            ask = Decimal(str(t.get("ask", 0)))
            results.append(FundingRate(
                exchange=self.name,
                symbol=symbol,
                rate=rate_8h,
                rate_raw=rate_raw,
                interval_hours=interval_hours,
                next_funding_ts=next_ts,
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
        raise ValueError(f"Symbol {symbol} not found on Kraken")

    async def get_balance(self) -> Decimal:
        data = await self._get_auth("/accounts")
        accounts = data.get("accounts", {})
        flex = accounts.get("flex", {})
        return Decimal(str(flex.get("availableMargin", 0)))

    async def _get_precision_map(self) -> dict[str, int]:
        # contractValueTradePrecision: decimal places allowed in order size.
        # All PF_ contracts: 1 contract = 1 unit of base asset.
        # BTC precision=4 → step=0.0001 BTC; DOGE precision=0 → step=1 DOGE.
        if self._precision_cache:
            return self._precision_cache
        data = await self._get("/instruments")
        for i in data.get("instruments", []):
            sym = i.get("symbol", "")
            if sym in REVERSE_MAP:
                self._precision_cache[sym] = int(i.get("contractValueTradePrecision", 0))
        return self._precision_cache

    async def get_qty_step(self, symbol: str) -> Decimal:
        kraken_sym = SYMBOL_MAP.get(symbol, "")
        pmap = await self._get_precision_map()
        precision = pmap.get(kraken_sym, 0)
        return Decimal(10) ** (-precision)

    async def get_position(self, symbol: str) -> Optional[Position]:
        data = await self._get_auth("/openpositions")
        kraken_sym = SYMBOL_MAP.get(symbol)
        for pos in data.get("openPositions", []):
            if pos.get("symbol") == kraken_sym:
                side = "long" if pos["side"] == "long" else "short"
                mark = Decimal(str(pos.get("markPrice", 0)))
                contracts = Decimal(str(pos["size"]))
                # 1 contract = 1 base asset unit — no conversion needed
                return Position(
                    exchange=self.name,
                    symbol=symbol,
                    side=side,
                    qty=contracts,
                    entry_price=Decimal(str(pos["price"])),
                    mark_price=mark,
                    unrealized_pnl=Decimal(str(pos.get("unrealizedFunding", 0))),
                    margin=Decimal(str(pos.get("initialMargin", 0))),
                    liquidation_price=Decimal(str(pos.get("liquidationThreshold", 0))),
                )
        return None

    async def place_market_order(self, symbol: str, side: str, qty_asset: Decimal) -> OrderResult:
        """qty_asset is in base asset units. 1 Kraken PF_ contract = 1 base unit."""
        kraken_sym = SYMBOL_MAP.get(symbol)
        if not kraken_sym:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="", side=side,
                               qty=qty_asset, price=Decimal(0), status="failed",
                               error=f"Unknown symbol {symbol}")
        try:
            pmap = await self._get_precision_map()
            precision = pmap.get(kraken_sym, 0)
            step = Decimal(10) ** (-precision)
            contracts = (qty_asset / step).to_integral_value(rounding=ROUND_DOWN) * step
            if contracts <= 0:
                return OrderResult(exchange=self.name, symbol=symbol, order_id="", side=side,
                                   qty=qty_asset, price=Decimal(0), status="failed",
                                   error=f"0 contracts for qty={qty_asset} precision={precision}")
            data = await self._post_auth("/sendorder", {
                "orderType": "mkt",
                "symbol": kraken_sym,
                "side": "buy" if side == "buy" else "sell",
                "size": str(contracts),
            })
            if data.get("result") == "error":
                return OrderResult(exchange=self.name, symbol=symbol, order_id="", side=side,
                                   qty=qty_asset, price=Decimal(0), status="failed",
                                   error=data.get("error", "unknown Kraken error"))
            status = data.get("sendStatus", {})
            order_id = status.get("order_id", "")
            filled = status.get("status", "")
            fill_price = Decimal(str(status.get("price", 0)))
            err = None if filled == "placed" else f"sendStatus={filled}"
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id=order_id,
                side=side, qty=contracts, price=fill_price,
                status="filled" if filled == "placed" else "failed",
                error=err,
            )
        except Exception as e:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="", side=side,
                               qty=qty_asset, price=Decimal(0), status="failed", error=str(e))

    async def close_position(self, symbol: str) -> OrderResult:
        # Close using raw contract size from API (not converted qty)
        data = await self._get_auth("/openpositions")
        kraken_sym = SYMBOL_MAP.get(symbol)
        for pos in data.get("openPositions", []):
            if pos.get("symbol") == kraken_sym:
                contracts = Decimal(str(pos["size"]))
                close_side = "sell" if pos["side"] == "long" else "buy"
                mark = Decimal(str(pos.get("markPrice", 0)))
                api_data = await self._post_auth("/sendorder", {
                    "orderType": "mkt",
                    "symbol": kraken_sym,
                    "side": close_side,
                    "size": str(contracts),
                })
                status = api_data.get("sendStatus", {})
                return OrderResult(
                    exchange=self.name, symbol=symbol, order_id=status.get("order_id", ""),
                    side=close_side, qty=contracts,
                    price=Decimal(str(status.get("price", 0))) or mark,
                    status="filled" if status.get("status") == "placed" else status.get("status", "failed"),
                )
        return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                           side="", qty=Decimal(0), price=Decimal(0),
                           status="failed", error="No open position")

    async def ping(self) -> float:
        t0 = time.monotonic()
        await self._get("/instruments")
        return (time.monotonic() - t0) * 1000

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
