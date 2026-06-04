"""
01.xyz — perpetuals DEX on N1 blockchain (Nord engine).
Auth: Solana Ed25519 keypair (base58). Uses protobuf + REST API.
Mainnet: https://zo-mainnet.n1.xyz

Encoding (schema.proto):
  raw = varint(len(payload)) | payload
  msg = raw | sign(raw)

Signature schemes:
  user_sign(raw)    = ed25519_sign(hex(raw))   — CreateSession only
  session_sign(raw) = ed25519_sign(raw)         — all trade actions
"""
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import aiohttp
import base58 as _base58
import schema_pb2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .base import ExchangeBase, FundingRate, OrderResult, Position
from utils import get_logger

log = get_logger("01")

BASE_URL = "https://zo-mainnet.n1.xyz"

# our symbol → 01.xyz market symbol
SYMBOL_MAP = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "SUI": "SUIUSD",
    "APT": "APTUSD",
    "NEAR": "NEARUSD",
    "ARB": "ARBUSD",
    "AAVE": "AAVEUSD",
}


def _load_keypair(key_b58: str) -> tuple[Ed25519PrivateKey, bytes]:
    raw = _base58.b58decode(key_b58)
    priv = Ed25519PrivateKey.from_private_bytes(raw[:32])
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub


def _user_sign(priv: Ed25519PrivateKey, raw: bytes) -> bytes:
    """Sign hex(raw) with user key — used for CreateSession."""
    return priv.sign(raw.hex().encode())


def _session_sign(priv: Ed25519PrivateKey, raw: bytes) -> bytes:
    """Sign raw bytes with session key — used for all trade actions."""
    return priv.sign(raw)


def _encode_varint(n: int) -> bytes:
    result = []
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            result.append(bits | 0x80)
        else:
            result.append(bits)
            break
    return bytes(result)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, offset


def _length_delimit(action: schema_pb2.Action) -> bytes:
    payload = action.SerializeToString()
    return _encode_varint(len(payload)) + payload


def _parse_receipt(resp_bytes: bytes) -> schema_pb2.Receipt:
    length, offset = _decode_varint(resp_bytes, 0)
    receipt = schema_pb2.Receipt()
    receipt.ParseFromString(resp_bytes[offset:offset + length])
    return receipt


class ZeroOneExchange(ExchangeBase):
    name = "01"
    read_only = False

    def __init__(self, private_key_b58: str):
        self._priv, self._pub = _load_keypair(private_key_b58)
        self._pubkey_b58 = _base58.b58encode(self._pub).decode()
        self._session_priv: Optional[Ed25519PrivateKey] = None
        self._session_id: Optional[int] = None
        self._session_expiry: int = 0
        self._account_id: Optional[int] = None
        self._markets: dict = {}  # ex_symbol → {market_id, price_decimals, size_decimals}
        self._http: Optional[aiohttp.ClientSession] = None

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    async def _client(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            })
        return self._http

    async def _get(self, path: str, params: dict = None) -> dict:
        import json as _json
        c = await self._client()
        async with c.get(BASE_URL + path, params=params,
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            text = await r.text()
            if text.lstrip().startswith("<"):
                raise RuntimeError(f"01.xyz Cloudflare block ({path})")
            return _json.loads(text)

    async def _get_text(self, path: str) -> str:
        c = await self._client()
        async with c.get(BASE_URL + path, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            text = await r.text()
            if text.lstrip().startswith("<"):
                raise RuntimeError(f"01.xyz Cloudflare block ({path})")
            return text

    async def _post_action(self, msg: bytes) -> schema_pb2.Receipt:
        c = await self._client()
        async with c.post(
            BASE_URL + "/action",
            data=msg,
            headers={"Content-Type": "application/octet-stream"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            r.raise_for_status()
            return _parse_receipt(await r.read())

    async def _server_ts(self) -> int:
        return int(await self._get_text("/timestamp"))

    # ── Market info ────────────────────────────────────────────────────────────

    async def _ensure_markets(self):
        if self._markets:
            return
        data = await self._get("/info")
        for m in data.get("markets", []):
            sym = m["symbol"]
            self._markets[sym] = {
                "market_id": int(m["marketId"]),
                "price_decimals": int(m["priceDecimals"]),
                "size_decimals": int(m["sizeDecimals"]),
            }

    # ── Account + session ──────────────────────────────────────────────────────

    async def _lookup_account(self):
        """Fetch account_id and reuse existing valid session if available."""
        data = await self._get(f"/user/{self._pubkey_b58}")
        ids = data.get("accountIds", [])
        if ids:
            self._account_id = ids[0]

        now = int(time.time())
        sessions = data.get("sessions", {})
        for sid_str, sinfo in sessions.items():
            try:
                expiry = int(datetime.fromisoformat(
                    sinfo["expiry"].replace("Z", "+00:00")
                ).timestamp())
            except Exception:
                continue
            # Reuse if our main key is the session key (common setup)
            if expiry > now + 3600 and sinfo.get("pubkey") == self._pubkey_b58:
                self._session_id = int(sid_str)
                self._session_priv = self._priv
                self._session_expiry = expiry
                log.info(f"01.xyz reusing session {self._session_id} (expires {sinfo['expiry']})")
                break

    async def _create_session(self):
        """Create a new 24h session with a fresh ephemeral keypair."""
        session_priv = Ed25519PrivateKey.generate()
        session_pub = session_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        server_ts = await self._server_ts()
        expiry = server_ts + 86400

        action = schema_pb2.Action()
        action.current_timestamp = server_ts
        action.create_session.user_pubkey = bytes(self._pub)
        action.create_session.session_pubkey = session_pub
        action.create_session.expiry_timestamp = expiry

        raw = _length_delimit(action)
        sig = _user_sign(self._priv, raw)
        receipt = await self._post_action(raw + sig)

        which = receipt.WhichOneof("kind")
        if which == "err":
            raise RuntimeError(f"01.xyz CreateSession error: {receipt.err}")
        if which != "create_session_result":
            raise RuntimeError(f"01.xyz unexpected receipt: {which}")

        self._session_id = receipt.create_session_result.session_id
        self._session_priv = session_priv
        self._session_expiry = expiry
        log.info(f"01.xyz new session created: {self._session_id}")

    async def _ensure_session(self):
        if self._session_id and self._session_expiry > int(time.time()) + 300:
            return
        # Session expired or missing — reset stale state so _create_session is called
        self._session_id = None
        self._session_priv = None
        if self._account_id is None:
            await self._lookup_account()
        if not self._session_id:
            await self._create_session()

    # ── ExchangeBase interface ─────────────────────────────────────────────────

    async def _fetch_symbol(self, our_sym: str, ex_sym: str, balance: Decimal) -> FundingRate | None:
        """Fetch stats + orderbook for one symbol in parallel."""
        m = self._markets.get(ex_sym)
        if not m:
            return None
        try:
            stats, ob = await asyncio.gather(
                self._get(f"/market/{m['market_id']}/stats"),
                self._get(f"/market/{m['market_id']}/orderbook"),
            )
            perp = stats.get("perpStats", {})
            mark = Decimal(str(perp.get("mark_price") or stats.get("indexPrice", 0)))
            rate_1h = Decimal(str(perp.get("funding_rate", 0)))
            rate_8h = rate_1h * Decimal("8")
            next_time_str = perp.get("next_funding_time", "")
            try:
                next_ts = int(datetime.fromisoformat(
                    next_time_str.replace("Z", "+00:00")
                ).timestamp()) * 1000
            except Exception:
                next_ts = 0
            bid = Decimal(str(ob["bids"][0][0])) if ob.get("bids") else Decimal(0)
            ask = Decimal(str(ob["asks"][0][0])) if ob.get("asks") else Decimal(0)
            return FundingRate(
                exchange=self.name, symbol=our_sym,
                rate=rate_8h, rate_raw=rate_1h, interval_hours=1.0,
                next_funding_ts=next_ts, mark_price=mark,
                bid=bid, ask=ask, available_balance=balance,
            )
        except Exception as e:
            log.debug(f"01.xyz {our_sym} fetch error: {e}")
            return None

    async def get_all_funding_rates(self) -> list[FundingRate]:
        await self._ensure_markets()
        if self._account_id is None:
            await self._lookup_account()
        balance = await self.get_balance()

        # Fetch all symbols in parallel
        tasks = [
            self._fetch_symbol(our_sym, ex_sym, balance)
            for our_sym, ex_sym in SYMBOL_MAP.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, FundingRate)]

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        await self._ensure_markets()
        if self._account_id is None:
            await self._lookup_account()
        balance = await self.get_balance()
        ex_sym = SYMBOL_MAP.get(symbol)
        if not ex_sym:
            raise ValueError(f"01.xyz: unknown symbol {symbol}")
        result = await self._fetch_symbol(symbol, ex_sym, balance)
        if result is None:
            raise ValueError(f"01.xyz: failed to fetch {symbol}")
        return result

    async def get_balance(self) -> Decimal:
        if self._account_id is None:
            await self._lookup_account()
        if self._account_id is None:
            return Decimal(0)
        try:
            data = await self._get(f"/account/{self._account_id}")
            balances = data.get("balances", [])
            # Sum all USDC-equivalent balances
            return Decimal(str(sum(b.get("amount", 0) for b in balances)))
        except Exception:
            return Decimal(0)

    async def get_qty_step(self, symbol: str) -> Decimal:
        await self._ensure_markets()
        ex_sym = SYMBOL_MAP.get(symbol)
        if not ex_sym or ex_sym not in self._markets:
            return Decimal("0.001")
        return Decimal(10) ** -self._markets[ex_sym]["size_decimals"]

    async def get_position(self, symbol: str) -> Optional[Position]:
        if self._account_id is None:
            await self._lookup_account()
        if self._account_id is None:
            return None
        await self._ensure_markets()
        ex_sym = SYMBOL_MAP.get(symbol)
        if not ex_sym:
            return None
        try:
            data = await self._get(f"/account/{self._account_id}")
            m = self._markets.get(ex_sym, {})
            market_id = m.get("market_id")
            for pos in data.get("positions", []):
                if pos.get("marketId") != market_id:
                    continue
                perp = pos.get("perp", {})
                size = Decimal(str(abs(perp.get("baseSize", 0))))
                if size == 0:
                    return None
                entry = Decimal(str(perp.get("price", 0)))
                is_long = perp.get("isLong", True)
                pnl = Decimal(str(
                    perp.get("tradingPnl", 0) +
                    perp.get("fundingPaymentPnl", 0) +
                    perp.get("sizePricePnl", 0)
                ))
                return Position(
                    exchange=self.name,
                    symbol=symbol,
                    side="long" if is_long else "short",
                    qty=size,
                    entry_price=entry,
                    mark_price=Decimal(0),
                    unrealized_pnl=pnl,
                    margin=Decimal(0),
                    liquidation_price=Decimal(0),
                )
        except Exception as e:
            log.debug(f"01.xyz get_position error: {e}")
        return None

    async def place_market_order(self, symbol: str, side: str, qty: Decimal) -> OrderResult:
        try:
            await self._ensure_session()
            await self._ensure_markets()
        except Exception as e:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error=f"Session error: {e}")

        ex_sym = SYMBOL_MAP.get(symbol)
        if not ex_sym or ex_sym not in self._markets:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error=f"Unknown symbol {symbol}")

        m = self._markets[ex_sym]
        market_id = m["market_id"]
        price_dec = m["price_decimals"]
        size_dec = m["size_decimals"]

        try:
            ob = await self._get(f"/market/{market_id}/orderbook")
            if side == "sell":
                best_price = Decimal(str(ob["bids"][0][0]))
                # IOC sell: price 1% below best bid to ensure fill
                limit_price = best_price * Decimal("0.99")
                proto_side = schema_pb2.ASK
            else:
                best_price = Decimal(str(ob["asks"][0][0]))
                # IOC buy: price 1% above best ask to ensure fill
                limit_price = best_price * Decimal("1.01")
                proto_side = schema_pb2.BID
        except Exception as e:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error=f"Orderbook fetch error: {e}")

        price_raw = int(limit_price * Decimal(10) ** price_dec)
        size_raw = int(qty * Decimal(10) ** size_dec)

        if size_raw == 0:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error="Size rounds to zero")

        try:
            server_ts = await self._server_ts()

            action = schema_pb2.Action()
            action.current_timestamp = server_ts
            action.place_order.session_id = self._session_id
            action.place_order.market_id = market_id
            action.place_order.side = proto_side
            action.place_order.fill_mode = schema_pb2.IMMEDIATE_OR_CANCEL
            action.place_order.is_reduce_only = False
            action.place_order.price = price_raw
            action.place_order.size = size_raw

            raw = _length_delimit(action)
            sig = _session_sign(self._session_priv, raw)
            receipt = await self._post_action(raw + sig)
        except Exception as e:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error=f"Order error: {e}")

        which = receipt.WhichOneof("kind")
        if which == "err":
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error=f"Engine error: {receipt.err}")

        result = receipt.trade_or_place
        fills = result.fills

        if not fills:
            # IOC with no fills = no liquidity at our price
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side=side, qty=qty, price=Decimal(0),
                               status="failed", error="IOC: no fills (insufficient liquidity)")

        total_size_raw = sum(f.size for f in fills)
        avg_price_raw = sum(f.price * f.size for f in fills) / total_size_raw
        fill_price = Decimal(str(avg_price_raw)) / Decimal(10) ** price_dec
        fill_qty = Decimal(str(total_size_raw)) / Decimal(10) ** size_dec
        order_id = str(fills[0].order_id) if fills else ""

        log.info(f"01.xyz {side} {fill_qty} {symbol} @ {fill_price}")
        return OrderResult(
            exchange=self.name, symbol=symbol,
            order_id=order_id,
            side=side, qty=fill_qty, price=fill_price,
            status="filled",
        )

    async def close_position(self, symbol: str) -> OrderResult:
        # Retry get_position up to 3 times — 01.xyz API sometimes transiently
        # returns None even when a position exists, which would leave the SHORT
        # open while the other leg closes, causing silent accumulation.
        pos = None
        for attempt in range(3):
            pos = await self.get_position(symbol)
            if pos:
                break
            if attempt < 2:
                log.warning(f"01 close_position: {symbol} not found (attempt {attempt + 1}/3), retrying...")
                await asyncio.sleep(1)
        if not pos:
            return OrderResult(exchange=self.name, symbol=symbol, order_id="",
                               side="", qty=Decimal(0), price=Decimal(0),
                               status="failed", error="No open position")
        close_side = "sell" if pos.side == "long" else "buy"
        return await self.place_market_order(symbol, close_side, pos.qty)

    async def ping(self) -> float:
        import time as _time
        t0 = _time.monotonic()
        await self._get_text("/timestamp")
        return (_time.monotonic() - t0) * 1000

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()
