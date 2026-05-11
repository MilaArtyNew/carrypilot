import html
from decimal import Decimal
from core.scanner import Opportunity
from core.position_tracker import PairState


def format_opportunity(opp: Opportunity, margin_usd: Decimal, leverage: int, paper: bool = False) -> str:
    from utils.calculations import calc_qty
    avg_price = (opp.short_price + opp.long_price) / 2
    from decimal import Decimal as D
    qty = calc_qty(margin_usd, leverage, avg_price, D("0.001"))
    usd_short = qty * opp.short_price
    usd_long = qty * opp.long_price

    paper_tag = "📄 <b>[PAPER]</b> " if paper else ""
    return (
        f"{paper_tag}🔍 <b>Funding-связка найдена</b>\n\n"
        f"Монета: <b>{opp.symbol}</b>\n"
        f"SHORT: <b>{opp.short_exchange.upper()}</b>\n"
        f"LONG: <b>{opp.long_exchange.upper()}</b>\n\n"
        f"<b>Funding:</b>\n"
        f"{opp.short_exchange}: {opp.short_rate:.4%}\n"
        f"{opp.long_exchange}: {opp.long_rate:.4%}\n\n"
        f"Спред: <b>{opp.spread:.4%}</b>\n"
        f"Net: ~<b>{opp.net_profit:.4%}</b>\n\n"
        f"<b>Размер:</b>\n"
        f"{qty} {opp.symbol} / {qty} {opp.symbol}\n"
        f"~${usd_short:.0f} / ~${usd_long:.0f}\n\n"
        f"Плечо: до {leverage}x\n"
        f"До funding: {opp.minutes_to_funding:.0f} мин"
    )


def format_position(pair: PairState) -> str:
    pnl = pair.combined_pnl
    pnl_sign = "+" if pnl >= 0 else ""
    lines = [
        f"<b>{pair.symbol}</b>",
        f"SHORT: {pair.short_exchange} | qty={pair.qty}",
        f"LONG:  {pair.long_exchange} | qty={pair.qty}",
        f"PnL: {pnl_sign}{pnl:.4f} USD",
    ]
    if pair.short_pos:
        lines.append(f"SHORT entry: {pair.short_pos.entry_price}")
    if pair.long_pos:
        lines.append(f"LONG  entry: {pair.long_pos.entry_price}")
    return "\n".join(lines)


def format_trade_result(result, action: str = "открыта", paper: bool = False, trade=None) -> str:
    paper_tag = "📄 [PAPER] " if paper else ""
    if result.success:
        short_price = result.short_order.price if result.short_order else "?"
        long_price = result.long_order.price if result.long_order else "?"
        text = (
            f"✅ {paper_tag}<b>Сделка {action}</b>\n\n"
            f"Монета: <b>{result.symbol}</b>\n"
            f"SHORT {result.short_exchange}: {short_price}\n"
            f"LONG  {result.long_exchange}: {long_price}\n"
            f"Qty: {result.qty}"
        )
        if trade is not None and trade.realized_pnl:
            pnl = Decimal(trade.realized_pnl)
            sign = "+" if pnl >= 0 else ""
            text += f"\nRealized PnL: <b>{sign}{pnl:.4f} USD</b>"
            if trade.price_pnl and trade.funding_pnl:
                p = Decimal(trade.price_pnl)
                f_ = Decimal(trade.funding_pnl)
                text += (
                    f"\n  ├ Ценовой delta: {'+' if p>=0 else ''}{p:.4f}"
                    f"\n  └ Funding: +{f_:.4f}"
                )
        return text
    return (
        f"❌ {paper_tag}<b>Ошибка {action} {result.symbol}</b>\n"
        f"{html.escape(str(result.error or 'Unknown error'))}"
    )


def format_paper_log(trades: list) -> str:
    if not trades:
        return "История пуста."
    lines = ["<b>История paper-сделок:</b>\n"]
    real = [t for t in trades if "zombie" not in (t.closed_at or "")]
    for t in reversed(real[-10:]):
        pnl = Decimal(t.realized_pnl) if t.realized_pnl else None
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            pnl_str = f"{sign}{pnl:.4f}$"
            if t.price_pnl and t.funding_pnl:
                p = Decimal(t.price_pnl)
                f_ = Decimal(t.funding_pnl)
                pnl_str += f" (цена {'+' if p>=0 else ''}{p:.4f} / funding +{f_:.4f})"
        else:
            pnl_str = "—"
        lines.append(
            f"<b>{t.symbol}</b> {t.short_exchange}↓/{t.long_exchange}↑ "
            f"qty={t.qty} | PnL: {pnl_str}\n"
            f"  Открыта: {t.opened_at}\n"
            f"  Закрыта: {t.closed_at or '—'}"
        )
    return "\n\n".join(lines)


def format_paper_stats(stats: dict) -> str:
    pnl = stats["total_pnl"]
    sign = "+" if pnl >= 0 else ""
    price_pnl = stats.get("total_price_pnl", Decimal(0))
    funding_pnl = stats.get("total_funding_pnl", Decimal(0))
    p_sign = "+" if price_pnl >= 0 else ""
    return (
        f"<b>📄 Paper trading статистика:</b>\n\n"
        f"Открытых позиций: {stats['open']}\n"
        f"Закрытых сделок: {stats['closed']}\n"
        f"Wins / Losses: {stats['wins']} / {stats['losses']}\n"
        f"Win rate: {stats['win_rate']}\n\n"
        f"Итого P&L: <b>{sign}{pnl:.4f} USD</b>\n"
        f"  ├ Ценовой delta: {p_sign}{price_pnl:.4f} USD\n"
        f"  └ Funding payments: +{funding_pnl:.4f} USD"
    )


def format_live_stats(stats: dict, recent: list) -> str:
    pnl = stats["total_pnl"]
    sign = "+" if pnl >= 0 else ""
    price_pnl = stats.get("total_price_pnl", Decimal(0))
    funding_pnl = stats.get("total_funding_pnl", Decimal(0))
    p_sign = "+" if price_pnl >= 0 else ""
    lines = [
        f"<b>📊 Live статистика:</b>\n",
        f"Открытых позиций: {stats['open']}",
        f"Закрытых сделок: {stats['closed']}",
        f"Wins / Losses: {stats['wins']} / {stats['losses']}",
        f"Win rate: {stats['win_rate']}\n",
        f"Итого P&L: <b>{sign}{pnl:.4f} USD</b>",
        f"  ├ Ценовой delta: {p_sign}{price_pnl:.4f} USD",
        f"  └ Funding (оценка): +{funding_pnl:.4f} USD",
    ]
    if recent:
        lines.append("\n<b>Последние сделки:</b>")
        for t in recent:
            pnl_t = Decimal(t.realized_pnl) if t.realized_pnl else None
            pnl_str = f"{'+' if pnl_t and pnl_t >= 0 else ''}{pnl_t:.4f}$" if pnl_t is not None else "—"
            reason = f" [{t.close_reason}]" if t.close_reason else ""
            lines.append(
                f"<b>{t.symbol}</b> {t.short_exchange}↓/{t.long_exchange}↑ "
                f"PnL: {pnl_str}{reason}\n"
                f"  {t.opened_at} → {t.closed_at or '—'}"
            )
    return "\n".join(lines)


def format_emergency(symbol: str, short_ex: str, long_ex: str, short_ok: bool, long_ok: bool) -> str:
    return (
        f"🚨 <b>ОШИБКА: одна нога не открылась</b>\n\n"
        f"Монета: {symbol}\n"
        f"SHORT ({short_ex}): {'✅ открыт' if short_ok else '❌ не открыт'}\n"
        f"LONG ({long_ex}): {'✅ открыт' if long_ok else '❌ не открыт'}\n\n"
        f"Попытка аварийного закрытия...\n"
        f"<b>Проверь вручную!</b>"
    )
