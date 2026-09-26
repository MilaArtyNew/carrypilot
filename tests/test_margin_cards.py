"""Regression probes for margin-aware Delta approval cards (no exchange orders)."""
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.telegram_bot import TelegramBot
from core.scanner import Opportunity
from exchanges.zero_one import ZeroOneExchange


def opportunity() -> Opportunity:
    return Opportunity(
        symbol="ETH", short_exchange="01", long_exchange="extended",
        short_rate=Decimal("0.001"), long_rate=Decimal("0"),
        spread=Decimal("0.001"), net_profit=Decimal("0.0002"),
        short_price=Decimal("2000"), long_price=Decimal("2000"),
        short_bid=Decimal("1999"), short_ask=Decimal("2001"),
        long_bid=Decimal("1999"), long_ask=Decimal("2001"),
        minutes_to_funding=60, short_balance=Decimal("30"),
        long_balance=Decimal("50"), fetched_at=time.time(),
    )


def telegram_stub(short_balance="0", long_balance="50"):
    bot = object.__new__(TelegramBot)
    bot._paused = False
    bot._disabled_exchanges = set()
    bot.tracker = SimpleNamespace(get=lambda symbol: None, get_all=lambda: [])
    bot.paper_mode = False
    bot.margin_usd = Decimal("10")
    bot.margin_buffer_usd = Decimal("5")
    bot.leverage = 2
    bot.exchanges = {
        "01": SimpleNamespace(get_balance=AsyncMock(return_value=Decimal(short_balance))),
        "extended": SimpleNamespace(get_balance=AsyncMock(return_value=Decimal(long_balance))),
    }
    bot._pending = {}
    bot._pending_msg = {}
    bot._signal_sent_at = {}
    bot.send = AsyncMock(return_value=SimpleNamespace(message_id=7))
    bot._save_pending_msgs = Mock()
    return bot


class ApprovalCardMarginTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_card_if_current_margin_is_below_requirement(self):
        bot = telegram_stub(short_balance="14.99")
        with patch("bot.telegram_bot._is_trading_hours", return_value=True):
            await bot.on_opportunity(opportunity())
        bot.send.assert_not_awaited()
        self.assertEqual(bot._pending, {})
        bot._save_pending_msgs.assert_not_called()

    async def test_no_card_if_margin_probe_fails_closed(self):
        bot = telegram_stub()
        bot.exchanges["01"].get_balance.side_effect = TimeoutError("account endpoint")
        with patch("bot.telegram_bot._is_trading_hours", return_value=True):
            await bot.on_opportunity(opportunity())
        bot.send.assert_not_awaited()
        self.assertEqual(bot._pending, {})

    async def test_sufficient_current_margin_emits_one_card(self):
        bot = telegram_stub(short_balance="15", long_balance="15")
        with patch("bot.telegram_bot._is_trading_hours", return_value=True):
            await bot.on_opportunity(opportunity())
        bot.send.assert_awaited_once()
        self.assertIn("ETH:01:extended", bot._pending)


class ZeroOneMarginTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_margin_subtracts_reserved_initial_margin(self):
        ex = object.__new__(ZeroOneExchange)
        ex._account_id = 1
        ex._get = AsyncMock(return_value={
            "balances": [{"token": "USDC", "amount": 50}],
            "margins": {"omf": "50", "imf": "49", "pon": "1000"},
            "positions": [{"marketId": 1}], "orders": [{"orderId": 2}],
        })
        self.assertEqual(await ex.get_balance(), Decimal("1"))

    async def test_missing_margin_fields_do_not_use_total_wallet_balance(self):
        ex = object.__new__(ZeroOneExchange)
        ex._account_id = 1
        ex._get = AsyncMock(return_value={"balances": [{"amount": 50}]})
        self.assertEqual(await ex.get_balance(), Decimal("0"))

    async def test_margin_is_never_negative(self):
        ex = object.__new__(ZeroOneExchange)
        ex._account_id = 1
        ex._get = AsyncMock(return_value={"margins": {"omf": "3", "imf": "8"}})
        self.assertEqual(await ex.get_balance(), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
