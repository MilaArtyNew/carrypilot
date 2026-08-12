"""
Telegram bot: signals, approvals, and management commands.
"""
import asyncio
import html as _html
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes,
)

from core.scanner import Opportunity
from core.executor import TradeExecutor, TradeResult
from core.position_tracker import PositionTracker, PairState
from core.scanner import OpportunityScanner
from core.live_ledger import LiveLedger
from bot.messages import (
    format_opportunity, format_position, format_trade_result,
    format_emergency, format_paper_log, format_paper_stats, format_live_stats,
)
from exchanges.base import ExchangeBase
from utils import get_logger

log = get_logger("telegram")

SIGNAL_COOLDOWN_SEC = 300       # 5 min between signals for the same symbol
MAX_POSITIONS_PER_EXCHANGE = 3  # max simultaneous legs on one exchange
_DATA_DIR = Path(os.getenv("DATA_DIR", "/home/gpt/funding-arb-bot/data"))
PENDING_SIGNALS_PATH = _DATA_DIR / "pending_signals.json"
_IL_TZ = ZoneInfo("Asia/Jerusalem")
TRADING_HOUR_START = 8   # 08:00 Israel
TRADING_HOUR_END   = 19  # 19:00 Israel


def _is_trading_hours() -> bool:
    return TRADING_HOUR_START <= datetime.now(_IL_TZ).hour < TRADING_HOUR_END


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: int,
        executor: TradeExecutor,
        tracker: PositionTracker,
        scanner: OpportunityScanner,
        exchanges: dict[str, ExchangeBase],
        margin_usd: Decimal,
        leverage: int,
        paper_mode: bool = False,
        live_ledger: Optional[LiveLedger] = None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.executor = executor
        self.tracker = tracker
        self.scanner = scanner
        self.exchanges = exchanges
        self.margin_usd = margin_usd
        self.leverage = leverage
        self.paper_mode = paper_mode
        self.live_ledger = live_ledger
        self._paused = os.getenv("SIGNALS_PAUSED_ON_START", "false").lower() == "true"
        self._disabled_exchanges: set[str] = set()
        self._pending: dict[str, Opportunity] = {}
        self._pending_msg: dict[str, int] = self._load_pending_msgs()  # key → message_id, persisted (for /skipall)
        self._signal_sent_at: dict[str, float] = {}  # symbol → unix ts of last signal
        self.app: Optional[Application] = None

    def _leg_done(self, order) -> bool:
        return bool(order and (order.status == "filled" or "No open position" in (order.error or "")))

    def _settle_close_tracking(self, pair: PairState, result: TradeResult) -> str:
        """Update tracker after a close attempt.

        Fully closed pairs leave the pair tracker. If exactly one leg is done, the
        remaining leg becomes an explicit unhedged leg so the monitor won't spam
        pair-level emergency close attempts every cycle.
        """
        short_done = self._leg_done(result.short_order)
        long_done = self._leg_done(result.long_order)
        if short_done and long_done:
            self.tracker.remove_pair(pair.symbol)
            self._signal_sent_at[pair.symbol] = time.time()
            return "fully_closed"
        if short_done != long_done:
            legs = {}
            if not short_done and pair.short_pos:
                legs[pair.short_exchange] = pair.short_pos
            if not long_done and pair.long_pos:
                legs[pair.long_exchange] = pair.long_pos
            if legs:
                self.tracker.set_unhedged(pair.symbol, legs)
                self._signal_sent_at[pair.symbol] = time.time()
                return "partial_unhedged"
        return "still_open"

    async def _notify_partial_unhedged(self, symbol: str):
        leg = self.tracker.get_unhedged_leg(symbol)
        if not leg:
            return
        leg_exchange, leg_pos = leg
        await self.send(
            f"⚠️ <b>{symbol} partially closed</b>\n"
            f"Remaining naked leg: {leg_pos.side.upper()} {leg_pos.qty} on {leg_exchange}.\n"
            f"Use /close {symbol} to retry closing this leg."
        )

    async def send(self, text: str, reply_markup=None):
        return await self.app.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    @staticmethod
    def _load_pending_msgs() -> dict[str, int]:
        try:
            if PENDING_SIGNALS_PATH.exists():
                return {k: int(v) for k, v in json.loads(PENDING_SIGNALS_PATH.read_text()).items()}
        except Exception:
            log.warning("failed to load pending_signals.json", exc_info=True)
        return {}

    def _save_pending_msgs(self):
        try:
            PENDING_SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
            PENDING_SIGNALS_PATH.write_text(json.dumps(self._pending_msg))
        except Exception:
            log.warning("failed to save pending_signals.json", exc_info=True)

    # ── Opportunity signal ────────────────────────────────────────────────────

    def _exchange_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pair in self.tracker.get_all():
            counts[pair.short_exchange] = counts.get(pair.short_exchange, 0) + 1
            counts[pair.long_exchange]  = counts.get(pair.long_exchange, 0) + 1
        return counts

    async def _supersede_old_signal(self, key: str):
        """If a pending signal for this key already has a live message (e.g. the
        same pair fired again after cooldown), dismiss the stale one so it can't
        be orphaned/left un-skippable."""
        old_mid = self._pending_msg.pop(key, None)
        if old_mid:
            try:
                await self.app.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=old_mid, text="🔄 Superseded by newer signal."
                )
            except Exception:
                pass

    async def on_opportunity(self, opp: Opportunity):
        import time
        if self._paused:
            return
        if not _is_trading_hours():
            return
        if opp.short_exchange in self._disabled_exchanges or opp.long_exchange in self._disabled_exchanges:
            return
        if self.tracker.get(opp.symbol):
            return
        # Double-check ledger in paper mode — tracker could be empty after restart
        if self.paper_mode and self.executor.paper_ledger:
            if self.executor.paper_ledger.get_open(opp.symbol):
                return
        # Per-exchange position cap
        counts = self._exchange_counts()
        if counts.get(opp.short_exchange, 0) >= MAX_POSITIONS_PER_EXCHANGE:
            return
        if counts.get(opp.long_exchange, 0) >= MAX_POSITIONS_PER_EXCHANGE:
            return
        # Cooldown: don't spam the same symbol within 5 min
        last = self._signal_sent_at.get(opp.symbol, 0)
        if time.time() - last < SIGNAL_COOLDOWN_SEC:
            return
        self._signal_sent_at[opp.symbol] = time.time()

        key = f"{opp.symbol}:{opp.short_exchange}:{opp.long_exchange}"
        await self._supersede_old_signal(key)
        self._pending[key] = opp

        text = format_opportunity(opp, self.margin_usd, self.leverage, paper=self.paper_mode)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{key}"),
                InlineKeyboardButton("⏭ Skip",    callback_data=f"skip:{key}"),
            ],
            [InlineKeyboardButton("🔍 Details", callback_data=f"details:{key}")],
        ])
        msg = await self.send(text, reply_markup=keyboard)
        self._pending_msg[key] = msg.message_id
        self._save_pending_msgs()

    async def on_position_alert(self, symbol: str, msg: str):
        await self.send(msg)

    async def on_auto_close(self, symbol: str, reason: str):
        pair = self.tracker.get(symbol)
        if not pair:
            return
        await self.send(f"⚙️ <b>Auto-close {symbol}</b>\n{reason}")
        result = await self.executor.close_pair(symbol, pair.short_exchange, pair.long_exchange)

        closed_trade = None
        if self.paper_mode and self.executor.paper_ledger:
            recent = [t for t in self.executor.paper_ledger.get_all_closed() if t.symbol == symbol and t.realized_pnl]
            if recent:
                closed_trade = recent[-1]
        if result.success and self.live_ledger and result.short_order and result.long_order:
            closed_trade = self.live_ledger.record_close(
                symbol=symbol,
                short_close=result.short_order.price,
                long_close=result.long_order.price,
                reason=reason,
            )
            self.live_ledger.note_close(symbol)

        state = self._settle_close_tracking(pair, result)
        if state in {"fully_closed", "partial_unhedged"} and not result.success and self.live_ledger:
            self.live_ledger.note_close(symbol)
        await self.send(format_trade_result(result, action="closed (auto)", paper=self.paper_mode, trade=closed_trade))
        if state == "partial_unhedged":
            await self._notify_partial_unhedged(symbol)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass  # query may have expired; continue anyway
        data = query.data

        if data.startswith("approve:"):
            await self._handle_approve(data[8:], query)
        elif data.startswith("skip:"):
            self._pending.pop(data[5:], None)
            self._pending_msg.pop(data[5:], None)
            self._save_pending_msgs()
            await query.edit_message_text("⏭ Skipped.")
        elif data.startswith("details:"):
            opp = self._pending.get(data[8:])
            if opp:
                await query.edit_message_text(
                    f"Balances:\nSHORT ({opp.short_exchange}): ${opp.short_balance:.2f}\n"
                    f"LONG  ({opp.long_exchange}):  ${opp.long_balance:.2f}\n"
                    f"SHORT price: {opp.short_price}\nLONG price: {opp.long_price}",
                    parse_mode="HTML",
                )
        elif data.startswith("closeleg:"):
            await self._handle_close_leg(data[9:], query)
        elif data.startswith("close:"):
            await self._handle_close(data[6:], query)
        elif data == "closeall:confirm":
            await self._handle_closeall(query)
        elif data == "closeall:cancel":
            await query.edit_message_text("❌ Cancelled.")
        elif data.startswith("ex_toggle:"):
            name = data[10:]
            if name in self._disabled_exchanges:
                self._disabled_exchanges.discard(name)
            else:
                self._disabled_exchanges.add(name)
            await self._send_exchanges_menu(query.message, edit=True)

    async def cmd_skipall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        n = await self._skip_all_pending()
        if n:
            await update.message.reply_text(f"⏭ Skipped all pending signals ({n}).")
        else:
            await update.message.reply_text("Nothing to skip.")

    async def _skip_all_pending(self) -> int:
        """Dismiss every not-yet-decided signal message, including ones sent
        before a bot restart (message ids are persisted to disk)."""
        self._pending.clear()
        msg_ids = list(self._pending_msg.values())
        self._pending_msg.clear()
        self._save_pending_msgs()
        for mid in msg_ids:
            try:
                await self.app.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=mid, text="⏭ Skipped (all)."
                )
            except Exception:
                pass  # message may already be edited/too old to touch
        return len(msg_ids)

    async def _handle_approve(self, key: str, query):
        if self._paused:
            self._pending.pop(key, None)
            self._pending_msg.pop(key, None)
            self._save_pending_msgs()
            try:
                await query.edit_message_text("⏸ Signals are paused. This pending signal was cancelled; no order was sent.")
            except Exception:
                await self.send("⏸ Signals are paused. Pending approve ignored; no order was sent.")
            return

        opp = self._pending.get(key)
        if not opp:
            parts = key.split(":")
            if len(parts) == 3:
                symbol, short_ex, long_ex = parts
                await query.edit_message_text(f"🔄 Bot restarted — rescanning {symbol} again...")
                try:
                    opps = await self.scanner.scan()
                    opp = next(
                        (o for o in opps
                         if o.symbol == symbol and o.short_exchange == short_ex and o.long_exchange == long_ex),
                        None,
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ Scan error: {e}")
                    return
                if not opp:
                    await query.edit_message_text(f"❌ {symbol} is no longer valid — wait for a new signal.")
                    return
            else:
                await query.edit_message_text("❌ Trade expired — wait for a new signal.")
                return

        try:
            await query.edit_message_text("🔄 Re-checking conditions...")
        except Exception:
            pass

        try:
            valid, reason = await self._recheck(opp)
            if not valid:
                self._pending.pop(key, None)
                self._pending_msg.pop(key, None)
                self._save_pending_msgs()
                await self.send(f"❌ Trade cancelled: {reason}")
                return

            mode_tag = "📄 [PAPER] " if self.paper_mode else ""
            await self.send(f"🚀 {mode_tag}Opening {opp.symbol}...")
            result = await self.executor.open_pair(opp)
            self._pending.pop(key, None)
            self._pending_msg.pop(key, None)
            self._save_pending_msgs()

            if result.success:
                short_entry = result.short_order.price
                long_entry = result.long_order.price
                self.tracker.add_pair(PairState(
                    symbol=opp.symbol,
                    short_exchange=opp.short_exchange,
                    long_exchange=opp.long_exchange,
                    qty=result.qty,
                    short_entry=short_entry,
                    long_entry=long_entry,
                ))
                if self.live_ledger:
                    self.live_ledger.record_open(
                        symbol=opp.symbol,
                        short_exchange=opp.short_exchange,
                        long_exchange=opp.long_exchange,
                        qty=result.qty,
                        short_entry=short_entry,
                        long_entry=long_entry,
                        spread=opp.spread,
                    )
                    self.live_ledger.note_open(opp.symbol, time.time())
                await self.send(format_trade_result(result, paper=self.paper_mode))
            else:
                short_ok = result.short_order and result.short_order.status == "filled"
                long_ok = result.long_order and result.long_order.status == "filled"
                if not self.paper_mode and (short_ok or long_ok):
                    await self.send(format_emergency(
                        opp.symbol, opp.short_exchange, opp.long_exchange, short_ok, long_ok
                    ))
                else:
                    await self.send(format_trade_result(result, paper=self.paper_mode))
        except Exception as e:
            log.error(f"_handle_approve unhandled exception for {opp.symbol}: {e}", exc_info=True)
            self._pending.pop(key, None)
            self._pending_msg.pop(key, None)
            self._save_pending_msgs()
            await self.send(f"❌ Unexpected error opening {opp.symbol}: {_html.escape(str(e)[:500])}")

    async def _recheck(self, opp: Opportunity) -> tuple[bool, str]:
        from utils import bid_ask_spread, minutes_to_funding
        import time as _time

        age_s = _time.time() - opp.fetched_at

        # Fresh signal (< 30s) — use cached scanner data, no extra API calls
        if age_s < 30:
            spread = opp.short_rate - opp.long_rate
            if spread < Decimal("0.0003"):
                return False, f"Spread dropped to {spread:.4%}"
            ba_short = bid_ask_spread(opp.short_bid, opp.short_ask)
            ba_long = bid_ask_spread(opp.long_bid, opp.long_ask)
            if ba_short > self.scanner.max_ba_spread or ba_long > self.scanner.max_ba_spread:
                return False, "Bid/ask spread is too wide"
            mins = opp.minutes_to_funding - age_s / 60
            if mins < 15:
                return False, f"Time to funding left: {mins:.0f} min"
            if not self.paper_mode and (opp.short_balance <= 0 or opp.long_balance <= 0):
                return False, "Insufficient balance"
            if self.tracker.get(opp.symbol):
                return False, "Position for this symbol is already open"
            return True, ""

        # Stale signal — re-fetch from exchanges with retry
        short_ex = self.exchanges[opp.short_exchange]
        long_ex = self.exchanges[opp.long_exchange]
        last_exc = None
        for attempt in range(2):
            try:
                short_fr, long_fr = await asyncio.gather(
                    short_ex.get_funding_rate(opp.symbol),
                    long_ex.get_funding_rate(opp.symbol),
                )
                new_spread = short_fr.rate - long_fr.rate
                if new_spread < Decimal("0.0003"):
                    return False, f"Spread dropped to {new_spread:.4%}"
                old_avg = (opp.short_price + opp.long_price) / 2
                new_avg = (short_fr.mark_price + long_fr.mark_price) / 2
                if old_avg > 0:
                    drift = abs(new_avg - old_avg) / old_avg
                    if drift > Decimal("0.005"):
                        return False, f"Price drifted by {drift:.2%}"
                ba_short = bid_ask_spread(short_fr.bid, short_fr.ask)
                ba_long = bid_ask_spread(long_fr.bid, long_fr.ask)
                if ba_short > self.scanner.max_ba_spread or ba_long > self.scanner.max_ba_spread:
                    return False, "Bid/ask spread is too wide"
                mins = minutes_to_funding(short_fr.next_funding_ts)
                if mins < 15:
                    return False, f"Time to funding left: {mins:.0f} min"
                if not self.paper_mode:
                    short_balance = await short_ex.get_balance()
                    long_balance = await long_ex.get_balance()
                    if short_balance <= 0 or long_balance <= 0:
                        return False, "Insufficient balance"
                if self.tracker.get(opp.symbol):
                    return False, "Position for this symbol is already open"
                return True, ""
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    log.warning(f"_recheck {opp.symbol}: transient error, retrying in 3s: {e}")
                    await asyncio.sleep(3)
        return False, f"Re-check error: {_html.escape(str(last_exc)[:300])}"

    async def _handle_close(self, symbol: str, query):
        pair = self.tracker.get(symbol)
        if not pair:
            await query.edit_message_text(f"❌ No open position for {symbol}")
            return
        await query.edit_message_text(f"🔄 Closing {symbol}...")
        result = await self.executor.close_pair(symbol, pair.short_exchange, pair.long_exchange)

        closed_trade = None
        if self.paper_mode and self.executor.paper_ledger:
            recent = [
                t for t in self.executor.paper_ledger.get_all_closed()
                if t.symbol == symbol and t.realized_pnl
            ]
            if recent:
                closed_trade = recent[-1]
        if result.success and self.live_ledger and result.short_order and result.long_order:
            closed_trade = self.live_ledger.record_close(
                symbol=symbol,
                short_close=result.short_order.price,
                long_close=result.long_order.price,
                reason="manual",
            )
            self.live_ledger.note_close(symbol)

        state = self._settle_close_tracking(pair, result)
        if state in {"fully_closed", "partial_unhedged"} and not result.success and self.live_ledger:
            self.live_ledger.note_close(symbol)
        await self.send(format_trade_result(result, action="closed", paper=self.paper_mode, trade=closed_trade))
        if state == "partial_unhedged":
            await self._notify_partial_unhedged(symbol)

    async def _handle_close_leg(self, symbol: str, query):
        leg = self.tracker.get_unhedged_leg(symbol)
        if not leg:
            await query.edit_message_text(f"❌ No naked leg for {symbol}")
            return
        leg_exchange, _ = leg
        await query.edit_message_text(f"🔄 Closing naked {symbol} leg on {leg_exchange}...")
        result = await self.executor.close_leg(symbol, leg_exchange)

        done = result.status == "filled" or "No open position" in (result.error or "")
        if done:
            self.tracker.clear_unhedged(symbol)
            if self.live_ledger:
                # Drop the stale open-time entry so the symbol doesn't carry an old age
                self.live_ledger.note_close(symbol)
            price = f" @ {result.price}" if result.status == "filled" else ""
            await self.send(f"✅ Naked {symbol} leg closed on {leg_exchange}{price}")
        else:
            await self.send(
                f"❌ Failed to close naked {symbol} leg on {leg_exchange}: "
                f"{_html.escape(str(result.error)[:200])}"
            )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pairs = self.tracker.get_all()
        mode = "📄 PAPER MODE\n\n" if self.paper_mode else ""
        if not pairs and not self.tracker.get_unhedged():
            await update.message.reply_html(f"{mode}No open positions.")
            return
        lines = [f"{mode}<b>Open positions:</b>\n"] if pairs else [mode]
        for p in pairs:
            lines.append(format_position(p))
            lines.append("")
        lines.extend(self._unhedged_lines())
        await update.message.reply_html("\n".join(lines))

    def _unhedged_lines(self) -> list[str]:
        """Naked legs are outside the tracker — surface them so they don't sit unnoticed."""
        unhedged = self.tracker.get_unhedged()
        if not unhedged:
            return []
        lines = ["⚠️ <b>Unhedged legs</b> (close with /close SYMBOL):"]
        for symbol, legs in unhedged.items():
            for exchange, pos in legs.items():
                lines.append(f"{symbol}: {pos.side.upper()} {pos.qty} on {exchange} @ {pos.entry_price}")
        return lines

    async def cmd_opportunities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Scanning...")
        opps = await self.scanner.scan()
        opps = [o for o in opps if not self.tracker.get(o.symbol)]
        if not opps:
            await update.message.reply_text("No suitable setups found.")
            return
        for opp in opps[:5]:
            key = f"{opp.symbol}:{opp.short_exchange}:{opp.long_exchange}"
            await self._supersede_old_signal(key)
            self._pending[key] = opp
            text = format_opportunity(opp, self.margin_usd, self.leverage, paper=self.paper_mode)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{key}"),
                    InlineKeyboardButton("⏭ Skip",    callback_data=f"skip:{key}"),
                ],
                [InlineKeyboardButton("🔍 Details", callback_data=f"details:{key}")],
            ])
            msg = await update.message.reply_html(text, reply_markup=keyboard)
            self._pending_msg[key] = msg.message_id
            self._save_pending_msgs()

    async def cmd_balances(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        lines = ["<b>Balances:</b>"]
        for name, ex in self.exchanges.items():
            if ex.read_only:
                lines.append(f"{name}: (monitoring only)")
                continue
            try:
                bal = await ex.get_balance()
                lines.append(f"{name}: ${bal:.2f}")
            except Exception as e:
                lines.append(f"{name}: error ({_html.escape(str(e)[:200])})")
        await update.message.reply_html("\n".join(lines))

    async def cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if not args:
            await update.message.reply_text("Usage: /close SYMBOL\nExample: /close SOL")
            return
        symbol = args[0].upper()
        pair = self.tracker.get(symbol)
        if not pair:
            leg = self.tracker.get_unhedged_leg(symbol)
            if leg:
                leg_exchange, leg_pos = leg
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"⚠️ Close naked leg on {leg_exchange}", callback_data=f"closeleg:{symbol}",
                    ),
                ]])
                await update.message.reply_text(
                    f"{symbol} has no pair — only a naked leg left:\n"
                    f"{leg_pos.side.upper()} {leg_pos.qty} on {leg_exchange} @ {leg_pos.entry_price}",
                    reply_markup=keyboard,
                )
                return
            await update.message.reply_text(f"No open position for {symbol}")
            return
        mode_tag = " [PAPER]" if self.paper_mode else ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Close {symbol}{mode_tag}", callback_data=f"close:{symbol}"),
        ]])
        await update.message.reply_text(
            f"Close {symbol}?\nSHORT: {pair.short_exchange} | LONG: {pair.long_exchange}",
            reply_markup=keyboard,
        )

    async def cmd_log(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.paper_mode or not self.executor.paper_ledger:
            await update.message.reply_text("Log is available only in paper mode.")
            return
        trades = self.executor.paper_ledger.get_all_closed()
        await update.message.reply_html(format_paper_log(trades))

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if self.paper_mode and self.executor.paper_ledger:
            stats = self.executor.paper_ledger.stats()
            await update.message.reply_html(format_paper_stats(stats))
        elif self.live_ledger:
            stats = self.live_ledger.stats()
            recent = self.live_ledger.recent_closed(10)
            await update.message.reply_html(format_live_stats(stats, recent))
        else:
            await update.message.reply_text("Statistics are unavailable.")

    async def _handle_closeall(self, query):
        import time
        pairs = self.tracker.get_all()
        if not pairs:
            await query.edit_message_text("No open positions.")
            return
        await query.edit_message_text(f"🔄 Closing {len(pairs)} positions...")
        lines = []
        for pair in pairs:
            result = await self.executor.close_pair(pair.symbol, pair.short_exchange, pair.long_exchange)
            state = self._settle_close_tracking(pair, result)
            if state in {"fully_closed", "partial_unhedged"}:
                if self.live_ledger:
                    if result.success and result.short_order and result.long_order:
                        self.live_ledger.record_close(
                            symbol=pair.symbol,
                            short_close=result.short_order.price,
                            long_close=result.long_order.price,
                            reason="closeall",
                        )
                    self.live_ledger.note_close(pair.symbol)
                if state == "fully_closed":
                    lines.append(f"✅ {pair.symbol} closed")
                else:
                    lines.append(f"⚠️ {pair.symbol}: partially closed; naked leg moved to /close {pair.symbol}")
            else:
                lines.append(f"❌ {pair.symbol}: {result.error or 'error'}")
        await self.send("Close-all result:\n" + "\n".join(lines))

    def _exchanges_keyboard(self) -> InlineKeyboardMarkup:
        rows = []
        for name, ex in self.exchanges.items():
            if ex.read_only:
                continue
            enabled = name not in self._disabled_exchanges
            label = f"✅ {name}" if enabled else f"🔴 {name} (off)"
            rows.append([InlineKeyboardButton(label, callback_data=f"ex_toggle:{name}")])
        return InlineKeyboardMarkup(rows)

    async def _send_exchanges_menu(self, message, edit: bool = False):
        disabled = self._disabled_exchanges
        text = "<b>Venues:</b>\n"
        for name, ex in self.exchanges.items():
            if ex.read_only:
                text += f"  {name}: 👁 monitoring only\n"
            elif name in disabled:
                text += f"  {name}: 🔴 disabled\n"
            else:
                text += f"  {name}: ✅ active\n"
        text += "\nClick a venue to enable/disable it:"
        kb = self._exchanges_keyboard()
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.reply_html(text, reply_markup=kb)

    async def cmd_exchanges(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self._send_exchanges_menu(update.message)

    async def cmd_closeall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pairs = self.tracker.get_all()
        if not pairs:
            await update.message.reply_text("No open positions.")
            return
        symbols = ", ".join(p.symbol for p in pairs)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Close all ({len(pairs)}): {symbols}", callback_data="closeall:confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="closeall:cancel"),
        ]])
        await update.message.reply_text(f"Close all positions?\n{symbols}", reply_markup=keyboard)

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = True
        skipped = await self._skip_all_pending()
        suffix = f" Pending signals cancelled: {skipped}." if skipped else ""
        await update.message.reply_text(f"⏸ Signals paused.{suffix}")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = False
        await update.message.reply_text("▶️ Signals resumed.")

    async def cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        mode = "📄 PAPER" if self.paper_mode else "🔴 LIVE"
        now_il = datetime.now(_IL_TZ).strftime("%H:%M")
        trading = "✅ active" if _is_trading_hours() else f"🌙 paused (window {TRADING_HOUR_START}:00–{TRADING_HOUR_END}:00 IL)"
        await update.message.reply_html(
            f"<b>Settings:</b>\n"
            f"Mode: <b>{mode}</b>\n"
            f"Margin: ${self.margin_usd}\n"
            f"Leverage: {self.leverage}x\n"
            f"Position: ${self.margin_usd * self.leverage}\n"
            f"Venues: {', '.join(self.exchanges.keys())}\n"
            f"IL time: {now_il} — {trading}"
        )

    async def send_startup(self):
        mode = "📄 PAPER" if self.paper_mode else "🔴 LIVE"
        await self.app.bot.set_my_commands([
            BotCommand("status",        "Open positions"),
            BotCommand("close",         "Close one position: /close SYMBOL"),
            BotCommand("closeall",      "Close all positions"),
            BotCommand("stats",         "PnL and trade stats"),
            BotCommand("exchanges",     "Enable / disable venues"),
            BotCommand("pause",         "Pause scanning"),
            BotCommand("resume",        "Resume scanning"),
            BotCommand("balances",      "Exchange balances"),
            BotCommand("opportunities", "Scan market"),
            BotCommand("skipall",       "Skip all pending signals"),
            BotCommand("settings",      "Bot settings"),
        ])
        text = f"🤖 Bot started [{mode}]\nOld buttons are invalid — wait for new signals."
        unhedged = self._unhedged_lines()
        if unhedged:
            text += "\n\n" + "\n".join(unhedged)
        await self.send(text)

    def build(self) -> Application:
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("status",        self.cmd_status))
        self.app.add_handler(CommandHandler("opportunities", self.cmd_opportunities))
        self.app.add_handler(CommandHandler("balances",      self.cmd_balances))
        self.app.add_handler(CommandHandler("close",         self.cmd_close))
        self.app.add_handler(CommandHandler("closeall",      self.cmd_closeall))
        self.app.add_handler(CommandHandler("skipall",       self.cmd_skipall))
        self.app.add_handler(CommandHandler("exchanges",     self.cmd_exchanges))
        self.app.add_handler(CommandHandler("log",           self.cmd_log))
        self.app.add_handler(CommandHandler("stats",         self.cmd_stats))
        self.app.add_handler(CommandHandler("pause",         self.cmd_pause))
        self.app.add_handler(CommandHandler("resume",        self.cmd_resume))
        self.app.add_handler(CommandHandler("settings",      self.cmd_settings))
        self.app.add_handler(CallbackQueryHandler(self._callback))
        return self.app
