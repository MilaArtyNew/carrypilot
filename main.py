"""
Entry point — wires everything together and starts the bot.
"""
import asyncio
import os
from decimal import Decimal

from dotenv import load_dotenv

from exchanges import ExtendedExchange, NadoExchange, ZeroOneExchange, VariationalExchange
from core.scanner import OpportunityScanner
from core.executor import TradeExecutor
from core.position_tracker import PositionTracker
from core.monitor import FundingMonitor
from core.paper_ledger import PaperLedger
from core.live_ledger import LiveLedger
from bot.telegram_bot import TelegramBot
from utils import get_logger

load_dotenv()
log = get_logger("main")


def build_exchanges() -> dict:
    exchanges = {}

    ext_key = os.getenv("EXTENDED_API_KEY")
    ext_priv = os.getenv("EXTENDED_PRIVATE_KEY")
    ext_pub = os.getenv("EXTENDED_PUBLIC_KEY")
    if ext_key and ext_priv and ext_pub:
        exchanges["extended"] = ExtendedExchange(ext_key, ext_priv, ext_pub)
        log.info("Extended: configured")
    else:
        log.warning("Extended: API keys missing, skipping")

    nado_key = os.getenv("NADO_PRIVATE_KEY")
    nado_wallet = os.getenv("NADO_WALLET_ADDRESS")
    if nado_key and nado_wallet:
        exchanges["nado"] = NadoExchange(nado_key, nado_wallet)
        log.info("Nado: configured")
    else:
        log.warning("Nado: NADO_PRIVATE_KEY or NADO_WALLET_ADDRESS missing — skip")

    zero_one_key = os.getenv("ZERO_ONE_PRIVATE_KEY")
    if zero_one_key:
        exchanges["01"] = ZeroOneExchange(zero_one_key)
        log.info("01.xyz: configured")
    else:
        log.warning("01.xyz: private key missing — skip")

    exchanges["variational"] = VariationalExchange()
    log.info("Variational: read-only monitor configured")

    return exchanges


async def main():
    exchanges = build_exchanges()
    if len([e for e in exchanges.values() if not e.read_only]) < 2:
        log.error("Need at least 2 trading exchanges. Check your .env keys.")
        return

    paper_mode = os.getenv("PAPER_TRADING", "false").lower() == "true"
    paper_ledger = PaperLedger() if paper_mode else None
    live_ledger = None if paper_mode else LiveLedger()
    if paper_mode:
        log.info("*** PAPER TRADING MODE — no real orders will be placed ***")

    margin = Decimal(os.getenv("POSITION_MARGIN_USD", "5"))
    leverage = int(os.getenv("LEVERAGE", "2"))

    scanner = OpportunityScanner(
        exchanges=list(exchanges.values()),
        min_spread=Decimal(os.getenv("MIN_FUNDING_SPREAD", "0.0003")),
        min_net_profit=Decimal(os.getenv("MIN_NET_PROFIT", "0.00015")),
        min_minutes_to_funding=float(os.getenv("MIN_MINUTES_TO_FUNDING", "15")),
        max_bid_ask_spread=Decimal(os.getenv("MAX_BID_ASK_SPREAD", "0.0005")),
        paper_mode=paper_mode,
    )

    executor = TradeExecutor(
        exchanges=exchanges,
        margin_usd=margin,
        leverage=leverage,
        paper_ledger=paper_ledger,
    )
    tracker = PositionTracker(exchanges=exchanges, paper_ledger=paper_ledger)

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    if not tg_token or not tg_chat:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env")
        return

    tg_bot = TelegramBot(
        token=tg_token,
        chat_id=tg_chat,
        executor=executor,
        tracker=tracker,
        scanner=scanner,
        exchanges=exchanges,
        margin_usd=margin,
        leverage=leverage,
        paper_mode=paper_mode,
        live_ledger=live_ledger,
    )
    app = tg_bot.build()

    monitor = FundingMonitor(
        scanner=scanner,
        tracker=tracker,
        on_opportunity=tg_bot.on_opportunity,
        on_position_alert=tg_bot.on_position_alert,
        on_auto_close=tg_bot.on_auto_close,
        margin_usd=margin,
        scan_interval=int(os.getenv("SCAN_INTERVAL", "30")),
        paper_mode=paper_mode,
    )

    # Restore open positions into tracker so bot doesn't re-signal them after restart
    if paper_ledger:
        from core.position_tracker import PairState
        for trade in paper_ledger.get_all_open():
            tracker.add_pair(PairState(
                symbol=trade.symbol,
                short_exchange=trade.short_exchange,
                long_exchange=trade.long_exchange,
                qty=Decimal(trade.qty),
                short_entry=Decimal(trade.short_entry),
                long_entry=Decimal(trade.long_entry),
            ))
            log.info(f"Restored paper position: {trade.symbol} ({trade.short_exchange}/{trade.long_exchange})")
    else:
        report = await tracker.restore_from_exchanges()
        if live_ledger:
            # Phantom "open" records pile up after failed closes — drop them, but only when
            # every exchange answered, otherwise an API outage would wipe live records
            if report.scan_complete:
                live_ledger.reconcile_open(report.live_symbols)
            else:
                log.warning(
                    f"Ledger reconcile skipped — incomplete scan: {', '.join(report.failed_exchanges)}"
                )
            position_times = live_ledger.get_all_position_times()
            for pair in tracker.get_all():
                if pair.symbol in position_times:
                    pair.opened_at = position_times[pair.symbol]
                    log.info(f"Restored open time for {pair.symbol}: {pair.opened_at:.0f}")

    log.info("Starting funding arb bot...")
    async with app:
        await app.start()
        await tg_bot.send_startup()
        monitor.start()
        log.info("Bot running. Press Ctrl+C to stop.")
        await app.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            monitor.stop()
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
