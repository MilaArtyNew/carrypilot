"""
Main monitoring loop — scans for opportunities and monitors open positions.

Auto-close conditions (checked every 30s):
  1. Unhealthy: one leg missing from exchange → alert + close (live mode only)
  2. Stop-loss: any leg unrealized P&L < -50% of margin → close
  3. Timer: random per-position target in [4, 5] hours AND >5 min to next funding → close
"""
import asyncio
import random
import time
from decimal import Decimal

from core.scanner import OpportunityScanner, Opportunity
from core.position_tracker import PositionTracker, PairState
from utils import get_logger, minutes_to_funding

log = get_logger("monitor")

MIN_MINUTES_TO_FUNDING_FOR_CLOSE = 5.0
CLOSE_WINDOW_MIN_HOURS = 4.0
CLOSE_WINDOW_MAX_HOURS = 5.0
OPEN_GRACE_SECONDS = 120  # positions may not surface on exchange APIs immediately after order fill


class FundingMonitor:
    def __init__(
        self,
        scanner: OpportunityScanner,
        tracker: PositionTracker,
        on_opportunity,      # async (opp: Opportunity)
        on_position_alert,   # async (symbol: str, msg: str)
        on_auto_close,       # async (symbol: str, reason: str)
        margin_usd: Decimal,
        scan_interval: int = 30,
        stop_loss_pct: Decimal = Decimal("0.5"),
        paper_mode: bool = False,
    ):
        self.scanner = scanner
        self.tracker = tracker
        self.on_opportunity = on_opportunity
        self.on_position_alert = on_position_alert
        self.on_auto_close = on_auto_close
        self.margin_usd = margin_usd
        self.scan_interval = scan_interval
        self.stop_loss_pct = stop_loss_pct
        self.paper_mode = paper_mode
        self._running = False
        self._seen: set[str] = set()
        self._closing: set[str] = set()
        # Per-position random close target (hours), assigned on first check
        self._close_targets: dict[str, float] = {}

    def start(self):
        self._running = True
        asyncio.create_task(self._scan_loop())
        asyncio.create_task(self._position_loop())

    def stop(self):
        self._running = False

    async def _scan_loop(self):
        while self._running:
            try:
                opps = await self.scanner.scan()
                for opp in opps:
                    key = f"{opp.symbol}:{opp.short_exchange}:{opp.long_exchange}"
                    if key not in self._seen:
                        self._seen.add(key)
                        if self.tracker.get(opp.symbol):
                            continue  # already have an open position for this symbol
                        log.info(f"New opportunity: {opp.symbol} spread={opp.spread:.4%} net={opp.net_profit:.4%}")
                        await self.on_opportunity(opp)
                valid_keys = {f"{o.symbol}:{o.short_exchange}:{o.long_exchange}" for o in opps}
                self._seen &= valid_keys
            except Exception as e:
                log.error(f"Scan loop error: {e}")
            await asyncio.sleep(self.scan_interval)

    async def _position_loop(self):
        while self._running:
            try:
                await self.tracker.refresh()
                for pair in self.tracker.get_all():
                    if pair.symbol in self._closing:
                        continue
                    await self._check_pair(pair)
            except Exception as e:
                log.error(f"Position loop error: {e}")
            await asyncio.sleep(30)

    async def _check_pair(self, pair: PairState):
        # 1. Unhealthy: one or both legs missing — skip in paper mode (can't lose a leg)
        if not self.paper_mode and not pair.is_healthy:
            age_s = pair.age_hours * 3600
            if age_s < OPEN_GRACE_SECONDS:
                log.debug(f"{pair.symbol}: unhealthy but only {age_s:.0f}s old — waiting for position to surface")
                return
            missing = []
            if not pair.short_pos:
                missing.append(f"SHORT ({pair.short_exchange})")
            if not pair.long_pos:
                missing.append(f"LONG ({pair.long_exchange})")
            reason = f"leg missing: {', '.join(missing)}"
            log.error(f"EMERGENCY {pair.symbol}: {reason}")
            await self._trigger_close(pair.symbol, f"🚨 Emergency close — {reason}")
            return

        if not pair.is_healthy:
            return  # paper mode: refresh failed, skip this cycle

        short_pnl = pair.short_pos.unrealized_pnl
        long_pnl = pair.long_pos.unrealized_pnl
        sl_threshold = -(self.margin_usd * self.stop_loss_pct)

        # 2. Stop-loss: any leg down > 50% of margin
        if short_pnl < sl_threshold or long_pnl < sl_threshold:
            worst = min(short_pnl, long_pnl)
            reason = f"stop-loss: one leg is down {worst:.4f} USD (limit {sl_threshold:.2f} USD)"
            log.warning(f"STOP-LOSS {pair.symbol}: {reason}")
            await self._trigger_close(pair.symbol, f"🛑 Stop-loss — {reason}")
            return

        # 3. Time-based close: random target per position in [4, 5] hours
        if pair.symbol not in self._close_targets:
            target = random.uniform(CLOSE_WINDOW_MIN_HOURS, CLOSE_WINDOW_MAX_HOURS)
            self._close_targets[pair.symbol] = target
            log.info(f"{pair.symbol}: close target set to {target:.2f}h")

        age_h = pair.age_hours
        target_h = self._close_targets[pair.symbol]
        if age_h >= target_h:
            mins = await self._minutes_to_next_funding(pair)
            if mins is None:
                return
            if mins > MIN_MINUTES_TO_FUNDING_FOR_CLOSE:
                reason = f"timer: position open for {age_h:.1f}h (target {target_h:.1f}h), time to funding {mins:.0f} min"
                log.info(f"TIMER CLOSE {pair.symbol}: {reason}")
                await self._trigger_close(pair.symbol, f"⏱ Timer close — {reason}")
            else:
                log.info(f"{pair.symbol}: timer triggered, but time to funding is {mins:.0f} min — waiting")

    async def _minutes_to_next_funding(self, pair: PairState) -> float | None:
        """Get minutes to next funding on the short leg exchange."""
        try:
            short_ex = self.tracker.exchanges.get(pair.short_exchange)
            if not short_ex:
                return None
            fr = await short_ex.get_funding_rate(pair.symbol)
            return minutes_to_funding(fr.next_funding_ts)
        except Exception as e:
            log.warning(f"Could not get funding time for {pair.symbol}: {e}")
            return None

    async def _trigger_close(self, symbol: str, reason: str):
        self._closing.add(symbol)
        self._close_targets.pop(symbol, None)  # remove target on close
        try:
            await self.on_auto_close(symbol, reason)
        finally:
            self._closing.discard(symbol)
