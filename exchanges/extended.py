"""
Extended exchange (StarkX / StarkNet L2) connector.
API: https://api.starknet.extended.exchange/api/v1
Signing: fast-stark-crypto
"""
import math
import secrets
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional

import aiohttp

from .base import ExchangeBase, FundingRate, OrderResult, Position
from fast_stark_crypto import get_order_msg_hash, sign as stark_sign

BASE_URL = "https://api.starknet.extended.exchange/api/v1"
COLLATERAL_ID = 1           # "0x1"
COLLATERAL_RESOLUTION = 1_000_000
TAKER_FEE = Decimal("0.0005")
MARKET_SLIPPAGE = Decimal("0.0075")

# StarkNet mainnet signing domain (from official SDK config.py)
DOMAIN_NAME = "Perpetuals"
DOMAIN_VERSION = "v0"
DOMAIN_CHAIN_ID = "SN_MAIN"
DOMAIN_REVISION = "1"

# Internal symbol → Extended market name
SYMBOL_MAP: dict[str, str] = {
    "BTC":      "BTC-USD",
    "ETH":      "ETH-USD",
    "SOL":      "SOL-USD",
    "XRP":      "XRP-USD",
    "DOGE":     "DOGE-USD",
    "ADA":      "ADA-USD",
    "AVAX":     "AVAX-USD",
    "LINK":     "LINK-USD",
    "SUI":      "SUI-USD",
    "NEAR":     "NEAR-USD",
    "ARB":      "ARB-USD",
    "BNB":      "BNB-USD",
    "LTC":      "LTC-USD",
    "BCH":      "BCH-USD",
    "AAVE":     "AAVE-USD",
    "UNI":      "UNI-USD",
    "ENA":      "ENA-USD",
    "JUP":      "JUP-USD",
    "HYPE":     "HYPE-USD",
    "ONDO":     "ONDO-USD",
    "TAO":      "TAO-USD",
    "APT":      "APT-USD",
    "OP":       "OP-USD",
    "WIF":      "WIF-USD",
    "PENGU":    "PENGU-USD",
    "BERA":     "BERA-USD",
    "VIRTUAL":  "VIRTUAL-USD",
    "FARTCOIN": "FARTCOIN-USD",
    "XMR":      "XMR-USD",
    "ZEC":      "ZEC-USD",
    "ZRO":      "ZRO-USD",
    "TRUMP":    "TRUMP-USD",
    "TRX":      "TRX-USD",
    "TON":      "TON-USD",
    "DOT":      "DOT-USD",
    "NEAR":     "NEAR-USD",
    "SNX":      "SNX-USD",
    "CRV":      "CRV-USD",
    "STRK":     "STRK-USD",
    "POPCAT":   "POPCAT-USD",
}
REVERSE_MAP = {v: k for k, v in SYMBOL_MAP.items()}


class ExtendedExchange(ExchangeBase):
    name = "extended"
    read_only = False

    def __init__(self, api_key: str, private_key: str, public_key: str):
        self._api_key = api_key
        self._private_key = int(private_key, 16)
        self._public_key = int(public_key, 16)
        self._public_key_hex = public_key if public_key.startswith("0x") else "0x" + public_key
        self._vault_id: Optional[int] = None
        self._session: Optional[aiohttp.ClientSession] = None
        # internal_symbol → {synthetic_id, synthetic_resolution, qty_step, min_qty}
        self._market_cache: dict[str, dict] = {}

    # ─── HTTP helpers ────────────────────────────────────────────────────────

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _headers(self) -> dict:
        return {"X-Api-Key": self._api_key}

    async def _get(self, path: str, params: dict = None) -> dict:
        s = await self._sess()
        async with s.get(
            BASE_URL + path, params=params, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def _post(self, path: str, json_data: dict) -> dict:
        s = await self._sess()
        async with s.post(
            BASE_URL + path, json=json_data, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            r.raise_for_status()
            return await r.json()

    # ─── Lazy init ───────────────────────────────────────────────────────────

    async def _ensure_vault_id(self) -> int:
        if self._vault_id is not None:
            return self._vault_id
        data = await self._get("/user/accounts")
        for acc in data.get("data", []):
            if acc.get("accountIndex", -1) == 0:
                self._vault_id = int(acc["l2Vault"])
                return self._vault_id
        accounts = data.get("data", [])
        if accounts:
            self._vault_id = int(accounts[0]["l2Vault"])
            return self._vault_id
        raise RuntimeError("No Extended account found — check API key")

    async def _ensure_market_cache(self):
        if self._market_cache:
            return
        data = await self._get("/info/markets")
        for m in data.get("data", []):
            name = m.get("name", "")
            internal = REVERSE_MAP.get(name)
            if not internal:
                continue
            l2 = m.get("l2Config", {})
            tc = m.get("tradingConfig", {})
            self._market_cache[internal] = {
                "synthetic_id": int(l2["syntheticId"], 16),
                "synthetic_resolution": int(l2["syntheticResolution"]),
                "qty_step": Decimal(str(tc.get("minOrderSizeChange", "1"))),
                "min_qty": Decimal(str(tc.get("minOrderSize", "1"))),
                "price_step": Decimal(str(tc.get("minPriceChange", "0.00001"))),
            }

    # ─── Order signing ───────────────────────────────────────────────────────

    def _sign_order(
        self,
        vault_id: int,
        synthetic_id: int,
        synthetic_resolution: int,
        side: str,      # "BUY" or "SELL"
        qty: Decimal,
        price: Decimal,
    ) -> dict:
        """
        Compute StarkX order hash, sign it, return signing artifacts.
        BUY:  base_amount = +syn (receiving),  quote_amount = -col (paying)
        SELL: base_amount = -syn (giving),     quote_amount = +col (receiving)
        """
        nonce = secrets.randbelow(2 ** 32)

        # Order expires in 1h; signing window = order_expiry + 14-day buffer
        order_expiry_ts = int(time.time()) + 3600
        signing_expiry = math.ceil(order_expiry_ts + 14 * 24 * 3600)
        expiry_ms = order_expiry_ts * 1000

        is_buy = (side == "BUY")
        round_syn = ROUND_UP if is_buy else ROUND_DOWN
        round_col = ROUND_DOWN if is_buy else ROUND_UP

        syn_amount = int(
            (qty * synthetic_resolution).quantize(Decimal("1"), rounding=round_syn)
        )
        # col_amount must be computed exactly as the server does: floor(qty * price * resolution)
        col_amount = int(qty * price * COLLATERAL_RESOLUTION)
        fee_amount = int(
            (col_amount * TAKER_FEE).quantize(Decimal("1"), rounding=ROUND_UP)
        )

        base_amount = syn_amount if is_buy else -syn_amount
        quote_amount = -col_amount if is_buy else col_amount

        msg_hash = get_order_msg_hash(
            position_id=vault_id,
            base_asset_id=synthetic_id,
            base_amount=base_amount,
            quote_asset_id=COLLATERAL_ID,
            quote_amount=quote_amount,
            fee_amount=fee_amount,
            fee_asset_id=COLLATERAL_ID,
            expiration=signing_expiry,
            salt=nonce,
            user_public_key=self._public_key,
            domain_name=DOMAIN_NAME,
            domain_version=DOMAIN_VERSION,
            domain_chain_id=DOMAIN_CHAIN_ID,
            domain_revision=DOMAIN_REVISION,
        )
        r, s = stark_sign(private_key=self._private_key, msg_hash=msg_hash)
        return {
            "order_id": str(msg_hash),
            "nonce": nonce,
            "expiry_ms": expiry_ms,
            "r": hex(r),
            "s": hex(s),
        }

    # ─── ExchangeBase interface ──────────────────────────────────────────────

    async def get_all_funding_rates(self) -> list[FundingRate]:
        balance = await self.get_balance()
        data = await self._get("/info/markets")
        results = []
        for m in data.get("data", []):
            name = m.get("name", "")
            internal = REVERSE_MAP.get(name)
            if not internal:
                continue
            if not m.get("active") or m.get("status") != "ACTIVE":
                continue
            stats = m.get("marketStats", {})
            rate_raw = Decimal(str(stats.get("fundingRate", 0)))
            rate_8h = rate_raw * 8      # hourly → 8h equivalent
            next_ts = int(stats.get("nextFundingRate", 0))
            if next_ts == 0:
                next_ts = (((int(time.time()) // 3600) + 1) * 3600) * 1000
            mark = Decimal(str(stats.get("markPrice") or 0))
            bid = Decimal(str(stats.get("bidPrice") or 0))
            ask = Decimal(str(stats.get("askPrice") or 0))
            results.append(FundingRate(
                exchange=self.name,
                symbol=internal,
                rate=rate_8h,
                rate_raw=rate_raw,
                interval_hours=1.0,
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
        raise ValueError(f"Symbol {symbol} not found on Extended")

    async def get_balance(self) -> Decimal:
        try:
            data = await self._get("/user/balance")
            return Decimal(str(data.get("data", {}).get("availableForTrade", 0)))
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return Decimal(0)
            raise

    async def get_qty_step(self, symbol: str) -> Decimal:
        await self._ensure_market_cache()
        m = self._market_cache.get(symbol)
        return m["qty_step"] if m else Decimal("1")

    async def get_min_qty(self, symbol: str) -> Decimal:
        await self._ensure_market_cache()
        m = self._market_cache.get(symbol)
        return m["min_qty"] if m else Decimal("1")

    async def get_position(self, symbol: str) -> Optional[Position]:
        data = await self._get("/user/positions")
        ext_sym = SYMBOL_MAP.get(symbol)
        for pos in data.get("data", []):
            if pos.get("market") == ext_sym and pos.get("status") == "OPENED":
                side = "long" if pos["side"] == "LONG" else "short"
                size = Decimal(str(pos["size"]))
                entry = Decimal(str(pos["openPrice"]))
                mark = Decimal(str(pos["markPrice"]))
                pnl = Decimal(str(pos["unrealisedPnl"]))
                liq = Decimal(str(pos.get("liquidationPrice") or 0))
                # Approximate margin: position value / leverage
                value = Decimal(str(pos.get("value", 0)))
                lev = Decimal(str(pos.get("leverage") or 1)) or Decimal(1)
                margin = value / lev
                return Position(
                    exchange=self.name, symbol=symbol, side=side,
                    qty=size, entry_price=entry, mark_price=mark,
                    unrealized_pnl=pnl, margin=margin, liquidation_price=liq,
                )
        return None

    async def place_market_order(self, symbol: str, side: str, qty_asset: Decimal) -> OrderResult:
        """side: 'buy' | 'sell', qty_asset in base asset units."""
        await self._ensure_market_cache()
        vault_id = await self._ensure_vault_id()

        m = self._market_cache.get(symbol)
        if not m:
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id="", side=side,
                qty=qty_asset, price=Decimal(0), status="failed",
                error=f"Unknown symbol {symbol} on Extended",
            )

        step = m["qty_step"]
        qty = (qty_asset / step).to_integral_value(ROUND_DOWN) * step
        if qty < m["min_qty"]:
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id="", side=side,
                qty=qty_asset, price=Decimal(0), status="failed",
                error=f"qty {qty} < min_qty {m['min_qty']} for {symbol}",
            )

        # Mark price with slippage, rounded to price_step (must match what server expects)
        fr = await self.get_funding_rate(symbol)
        mark = fr.mark_price
        ext_side = "BUY" if side == "buy" else "SELL"
        price_step = m.get("price_step", Decimal("0.00001"))
        if ext_side == "BUY":
            sign_price = (mark * (1 + MARKET_SLIPPAGE)).quantize(price_step, rounding=ROUND_UP)
        else:
            sign_price = (mark * (1 - MARKET_SLIPPAGE)).quantize(price_step, rounding=ROUND_DOWN)

        try:
            sig = self._sign_order(
                vault_id=vault_id,
                synthetic_id=m["synthetic_id"],
                synthetic_resolution=m["synthetic_resolution"],
                side=ext_side,
                qty=qty,
                price=sign_price,
            )
        except Exception as e:
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id="", side=side,
                qty=qty_asset, price=Decimal(0), status="failed",
                error=f"Signing error: {e}",
            )

        body = {
            "id": sig["order_id"],
            "market": SYMBOL_MAP[symbol],
            "type": "MARKET",
            "side": ext_side,
            "qty": str(qty),
            "price": str(sign_price),
            "postOnly": False,
            "timeInForce": "IOC",
            "expiryEpochMillis": sig["expiry_ms"],
            "fee": str(TAKER_FEE),
            "nonce": str(sig["nonce"]),
            "selfTradeProtectionLevel": "ACCOUNT",
            "settlement": {
                "signature": {"r": sig["r"], "s": sig["s"]},
                "starkKey": self._public_key_hex,
                "collateralPosition": str(vault_id),
            },
        }

        try:
            resp = await self._post("/user/order", body)
            order_data = resp.get("data", {}) or {}
            order_id = str(order_data.get("id", sig["order_id"]))
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id=order_id,
                side=side, qty=qty, price=mark,
                status="filled",
            )
        except aiohttp.ClientResponseError as e:
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id="", side=side,
                qty=qty_asset, price=Decimal(0), status="failed",
                error=f"HTTP {e.status}: {e.message}",
            )
        except Exception as e:
            return OrderResult(
                exchange=self.name, symbol=symbol, order_id="", side=side,
                qty=qty_asset, price=Decimal(0), status="failed",
                error=str(e),
            )

    async def close_position(self, symbol: str) -> OrderResult:
        data = await self._get("/user/positions")
        ext_sym = SYMBOL_MAP.get(symbol)
        for pos in data.get("data", []):
            if pos.get("market") == ext_sym and pos.get("status") == "OPENED":
                size = Decimal(str(pos["size"]))
                close_side = "sell" if pos["side"] == "LONG" else "buy"
                return await self.place_market_order(symbol, close_side, size)
        return OrderResult(
            exchange=self.name, symbol=symbol, order_id="", side="",
            qty=Decimal(0), price=Decimal(0), status="failed",
            error="No open position",
        )

    async def ping(self) -> float:
        t0 = time.monotonic()
        await self._get("/info/markets")
        return (time.monotonic() - t0) * 1000

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
