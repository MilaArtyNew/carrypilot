"""
Telegram bot: сигналы, approve, команды управления.
"""
import asyncio
from datetime import datetime
from decimal import Decimal
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
        self._paused = False
        self._disabled_exchanges: set[str] = set()
        self._pending: dict[str, Opportunity] = {}
        self._signal_sent_at: dict[str, float] = {}  # symbol → unix ts of last signal
        self.app: Optional[Application] = None

    async def send(self, text: str, reply_markup=None):
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    # ── Opportunity signal ────────────────────────────────────────────────────

    def _exchange_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pair in self.tracker.get_all():
            counts[pair.short_exchange] = counts.get(pair.short_exchange, 0) + 1
            counts[pair.long_exchange]  = counts.get(pair.long_exchange, 0) + 1
        return counts

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
        self._pending[key] = opp

        text = format_opportunity(opp, self.margin_usd, self.leverage, paper=self.paper_mode)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{key}"),
                InlineKeyboardButton("⏭ Skip",    callback_data=f"skip:{key}"),
            ],
            [InlineKeyboardButton("🔍 Details", callback_data=f"details:{key}")],
        ])
        await self.send(text, reply_markup=keyboard)

    async def on_position_alert(self, symbol: str, msg: str):
        await self.send(msg)

    async def on_auto_close(self, symbol: str, reason: str):
        pair = self.tracker.get(symbol)
        if not pair:
            return
        await self.send(f"⚙️ <b>Автозакрытие {symbol}</b>\n{reason}")
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

        already_gone = (
            not result.success
            and result.error
            and "No open position" in result.error
        )
        if result.success or already_gone:
            self.tracker.remove_pair(symbol)
            self._signal_sent_at[symbol] = time.time()  # cooldown after close
            if already_gone and self.live_ledger:
                self.live_ledger.note_close(symbol)
        await self.send(format_trade_result(result, action="закрыта (авто)", paper=self.paper_mode, trade=closed_trade))

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
            await query.edit_message_text("⏭ Пропущено.")
        elif data.startswith("details:"):
            opp = self._pending.get(data[8:])
            if opp:
                await query.edit_message_text(
                    f"Балансы:\nSHORT ({opp.short_exchange}): ${opp.short_balance:.2f}\n"
                    f"LONG  ({opp.long_exchange}):  ${opp.long_balance:.2f}\n"
                    f"Цена SHORT: {opp.short_price}\nЦена LONG: {opp.long_price}",
                    parse_mode="HTML",
                )
        elif data.startswith("close:"):
            await self._handle_close(data[6:], query)
        elif data == "closeall:confirm":
            await self._handle_closeall(query)
        elif data == "closeall:cancel":
            await query.edit_message_text("❌ Отменено.")
        elif data.startswith("ex_toggle:"):
            name = data[10:]
            if name in self._disabled_exchanges:
                self._disabled_exchanges.discard(name)
            else:
                self._disabled_exchanges.add(name)
            await self._send_exchanges_menu(query.message, edit=True)

    async def _handle_approve(self, key: str, query):
        opp = self._pending.get(key)
        if not opp:
            parts = key.split(":")
            if len(parts) == 3:
                symbol, short_ex, long_ex = parts
                await query.edit_message_text(f"🔄 Бот перезапускался — сканирую {symbol} заново...")
                try:
                    opps = await self.scanner.scan()
                    opp = next(
                        (o for o in opps
                         if o.symbol == symbol and o.short_exchange == short_ex and o.long_exchange == long_ex),
                        None,
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ Ошибка сканирования: {e}")
                    return
                if not opp:
                    await query.edit_message_text(f"❌ {symbol} больше не актуален — жди новый сигнал.")
                    return
            else:
                await query.edit_message_text("❌ Сделка устарела — жди новый сигнал.")
                return

        try:
            await query.edit_message_text("🔄 Повторная проверка условий...")
        except Exception:
            pass

        valid, reason = await self._recheck(opp)
        if not valid:
            self._pending.pop(key, None)
            await self.send(f"❌ Сделка отменена: {reason}")
            return

        mode_tag = "📄 [PAPER] " if self.paper_mode else ""
        await self.send(f"🚀 {mode_tag}Открываю {opp.symbol}...")
        result = await self.executor.open_pair(opp)
        self._pending.pop(key, None)

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

    async def _recheck(self, opp: Opportunity) -> tuple[bool, str]:
        from utils import bid_ask_spread, minutes_to_funding
        try:
            short_ex = self.exchanges[opp.short_exchange]
            long_ex = self.exchanges[opp.long_exchange]

            await asyncio.gather(short_ex.ping(), long_ex.ping())

            short_fr = await short_ex.get_funding_rate(opp.symbol)
            long_fr = await long_ex.get_funding_rate(opp.symbol)

            new_spread = short_fr.rate - long_fr.rate
            if new_spread < Decimal("0.0003"):
                return False, f"Спред упал до {new_spread:.4%}"

            old_avg = (opp.short_price + opp.long_price) / 2
            new_avg = (short_fr.mark_price + long_fr.mark_price) / 2
            if old_avg > 0:
                drift = abs(new_avg - old_avg) / old_avg
                if drift > Decimal("0.005"):
                    return False, f"Цена ушла на {drift:.2%}"

            ba_short = bid_ask_spread(short_fr.bid, short_fr.ask)
            ba_long = bid_ask_spread(long_fr.bid, long_fr.ask)
            if ba_short > self.scanner.max_ba_spread or ba_long > self.scanner.max_ba_spread:
                return False, "Bid/ask спред слишком широкий"

            mins = minutes_to_funding(short_fr.next_funding_ts)
            if mins < 15:
                return False, f"До funding осталось {mins:.0f} мин"

            if not self.paper_mode:
                short_balance = await short_ex.get_balance()
                long_balance = await long_ex.get_balance()
                if short_balance <= 0 or long_balance <= 0:
                    return False, "Недостаточно баланса"

            if self.tracker.get(opp.symbol):
                return False, "Позиция по этой монете уже открыта"

            return True, ""
        except Exception as e:
            return False, f"Ошибка проверки: {e}"

    async def _handle_close(self, symbol: str, query):
        pair = self.tracker.get(symbol)
        if not pair:
            await query.edit_message_text(f"❌ Нет открытой позиции по {symbol}")
            return
        await query.edit_message_text(f"🔄 Закрываю {symbol}...")
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

        already_gone = (
            not result.success
            and result.error
            and "No open position" in result.error
        )
        if result.success or already_gone:
            self.tracker.remove_pair(symbol)
            self._signal_sent_at[symbol] = time.time()  # cooldown after close
            if already_gone and self.live_ledger:
                self.live_ledger.note_close(symbol)
        await self.send(format_trade_result(result, action="закрыта", paper=self.paper_mode, trade=closed_trade))

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pairs = self.tracker.get_all()
        mode = "📄 PAPER MODE\n\n" if self.paper_mode else ""
        if not pairs:
            await update.message.reply_html(f"{mode}Нет открытых позиций.")
            return
        lines = [f"{mode}<b>Открытые позиции:</b>\n"]
        for p in pairs:
            lines.append(format_position(p))
            lines.append("")
        await update.message.reply_html("\n".join(lines))

    async def cmd_opportunities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Сканирую...")
        opps = await self.scanner.scan()
        if not opps:
            await update.message.reply_text("Подходящих связок не найдено.")
            return
        for opp in opps[:5]:
            key = f"{opp.symbol}:{opp.short_exchange}:{opp.long_exchange}"
            self._pending[key] = opp
            text = format_opportunity(opp, self.margin_usd, self.leverage, paper=self.paper_mode)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{key}"),
                    InlineKeyboardButton("⏭ Skip",    callback_data=f"skip:{key}"),
                ],
                [InlineKeyboardButton("🔍 Details", callback_data=f"details:{key}")],
            ])
            await update.message.reply_html(text, reply_markup=keyboard)

    async def cmd_balances(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        lines = ["<b>Балансы:</b>"]
        for name, ex in self.exchanges.items():
            if ex.read_only:
                lines.append(f"{name}: (только мониторинг)")
                continue
            try:
                bal = await ex.get_balance()
                lines.append(f"{name}: ${bal:.2f}")
            except Exception as e:
                lines.append(f"{name}: ошибка ({e})")
        await update.message.reply_html("\n".join(lines))

    async def cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if not args:
            await update.message.reply_text("Использование: /close SYMBOL\nПример: /close SOL")
            return
        symbol = args[0].upper()
        pair = self.tracker.get(symbol)
        if not pair:
            await update.message.reply_text(f"Нет открытой позиции по {symbol}")
            return
        mode_tag = " [PAPER]" if self.paper_mode else ""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Закрыть {symbol}{mode_tag}", callback_data=f"close:{symbol}"),
        ]])
        await update.message.reply_text(
            f"Закрыть {symbol}?\nSHORT: {pair.short_exchange} | LONG: {pair.long_exchange}",
            reply_markup=keyboard,
        )

    async def cmd_log(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.paper_mode or not self.executor.paper_ledger:
            await update.message.reply_text("Лог доступен только в paper mode.")
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
            await update.message.reply_text("Статистика недоступна.")

    async def _handle_closeall(self, query):
        import time
        pairs = self.tracker.get_all()
        if not pairs:
            await query.edit_message_text("Нет открытых позиций.")
            return
        await query.edit_message_text(f"🔄 Закрываю {len(pairs)} позиций...")
        lines = []
        for pair in pairs:
            result = await self.executor.close_pair(pair.symbol, pair.short_exchange, pair.long_exchange)
            if result.success:
                self.tracker.remove_pair(pair.symbol)
                self._signal_sent_at[pair.symbol] = time.time()
                if self.live_ledger:
                    if result.short_order and result.long_order:
                        self.live_ledger.record_close(
                            symbol=pair.symbol,
                            short_close=result.short_order.price,
                            long_close=result.long_order.price,
                            reason="closeall",
                        )
                    self.live_ledger.note_close(pair.symbol)
                lines.append(f"✅ {pair.symbol} закрыт")
            else:
                lines.append(f"❌ {pair.symbol}: {result.error or 'ошибка'}")
        await self.send("Результат закрытия всех позиций:\n" + "\n".join(lines))

    def _exchanges_keyboard(self) -> InlineKeyboardMarkup:
        rows = []
        for name, ex in self.exchanges.items():
            if ex.read_only:
                continue
            enabled = name not in self._disabled_exchanges
            label = f"✅ {name}" if enabled else f"🔴 {name} (откл)"
            rows.append([InlineKeyboardButton(label, callback_data=f"ex_toggle:{name}")])
        return InlineKeyboardMarkup(rows)

    async def _send_exchanges_menu(self, message, edit: bool = False):
        disabled = self._disabled_exchanges
        text = "<b>Биржи:</b>\n"
        for name, ex in self.exchanges.items():
            if ex.read_only:
                text += f"  {name}: 👁 только мониторинг\n"
            elif name in disabled:
                text += f"  {name}: 🔴 отключена\n"
            else:
                text += f"  {name}: ✅ активна\n"
        text += "\nНажми на биржу чтобы включить/отключить:"
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
            await update.message.reply_text("Нет открытых позиций.")
            return
        symbols = ", ".join(p.symbol for p in pairs)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Закрыть все ({len(pairs)}): {symbols}", callback_data="closeall:confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="closeall:cancel"),
        ]])
        await update.message.reply_text(f"Закрыть все позиции?\n{symbols}", reply_markup=keyboard)

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = True
        await update.message.reply_text("⏸ Сигналы остановлены.")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._paused = False
        await update.message.reply_text("▶️ Сигналы возобновлены.")

    async def cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        mode = "📄 PAPER" if self.paper_mode else "🔴 LIVE"
        now_il = datetime.now(_IL_TZ).strftime("%H:%M")
        trading = "✅ активно" if _is_trading_hours() else f"🌙 пауза (окно {TRADING_HOUR_START}:00–{TRADING_HOUR_END}:00 IL)"
        await update.message.reply_html(
            f"<b>Настройки:</b>\n"
            f"Режим: <b>{mode}</b>\n"
            f"Маржа: ${self.margin_usd}\n"
            f"Плечо: {self.leverage}x\n"
            f"Позиция: ${self.margin_usd * self.leverage}\n"
            f"Биржи: {', '.join(self.exchanges.keys())}\n"
            f"Время IL: {now_il} — {trading}"
        )

    async def send_startup(self):
        mode = "📄 PAPER" if self.paper_mode else "🔴 LIVE"
        await self.app.bot.set_my_commands([
            BotCommand("status",        "Открытые позиции"),
            BotCommand("closeall",      "Закрыть все позиции"),
            BotCommand("exchanges",     "Включить / отключить биржи"),
            BotCommand("pause",         "Остановить сканирование"),
            BotCommand("resume",        "Запустить сканирование"),
            BotCommand("balances",      "Балансы бирж"),
            BotCommand("opportunities", "Сканировать рынок"),
            BotCommand("settings",      "Настройки бота"),
        ])
        await self.send(f"🤖 Бот запущен [{mode}]\nСтарые кнопки недействительны — жди новые сигналы.")

    def build(self) -> Application:
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("status",        self.cmd_status))
        self.app.add_handler(CommandHandler("opportunities", self.cmd_opportunities))
        self.app.add_handler(CommandHandler("balances",      self.cmd_balances))
        self.app.add_handler(CommandHandler("close",         self.cmd_close))
        self.app.add_handler(CommandHandler("closeall",      self.cmd_closeall))
        self.app.add_handler(CommandHandler("exchanges",     self.cmd_exchanges))
        self.app.add_handler(CommandHandler("log",           self.cmd_log))
        self.app.add_handler(CommandHandler("stats",         self.cmd_stats))
        self.app.add_handler(CommandHandler("pause",         self.cmd_pause))
        self.app.add_handler(CommandHandler("resume",        self.cmd_resume))
        self.app.add_handler(CommandHandler("settings",      self.cmd_settings))
        self.app.add_handler(CallbackQueryHandler(self._callback))
        return self.app
