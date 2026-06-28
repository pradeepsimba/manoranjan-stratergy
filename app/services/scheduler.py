from __future__ import annotations

"""
Timing orchestrator — drives the trading session through its 5 phases:

  PRE_MARKET  → runs Gemini filter at 09:00
  WAIT_ZONE   → 09:15: Angel One auth, historical data load, WS subscribe
  ACTIVE      → 09:45: scan every completed 5-minute bar; place bracket orders
  CUTOFF      → 14:30: no new entries; Angel One OCO handles exits
  CLOSED      → 15:30: log daily summary, terminate session
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine.entry_engine import scan_stock
from app.engine.watchlist import build_watchlist, load_nse_universe
from app.models import Position, PositionStatus, TradingPhase
from app.services.gemini_filter import fetch_gemini_shortlist
from app.services.historical_data import (
    fetch_indicator_history,
    fetch_nifty_candles,
    fetch_today_candles,
)
from app.state import get_state

if TYPE_CHECKING:
    from app.services.angel_api import AngelAPIService
    from app.services.database import DatabaseService
    from app.services.market_data import MarketDataService
    from app.ws.dashboard_ws import DashboardWSManager

IST = ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    return datetime.now(IST)


def _next_occurrence(hour: int, minute: int) -> datetime:
    """Return next IST datetime when the clock hits HH:MM (today or tomorrow)."""
    now    = _now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def _seconds_until(hour: int, minute: int) -> float:
    return max(0.0, (_next_occurrence(hour, minute) - _now()).total_seconds())


def _is_5m_bar_complete() -> str | None:
    """
    Return "HH:MM" if a new completed 5-minute bar is available since last scan,
    else None.  5m bars close at :00, :05, :10 … :55.
    """
    now = _now()
    bar_min = (now.minute // 5) * 5
    # bar closed at HH:bar_min, so the bar_close time was in the past
    bar_close = now.replace(minute=bar_min, second=0, microsecond=0)
    # allow 5 s for data to arrive
    if (now - bar_close).total_seconds() < 5:
        return None
    label = bar_close.strftime("%H:%M")
    st    = get_state()
    if st.last_5m_bar_time == label:
        return None
    return label


class SchedulerService:
    def __init__(
        self,
        db:         "DatabaseService",
        angel:      "AngelAPIService",
        market_data: "MarketDataService",
        ws_manager: "DashboardWSManager",
    ) -> None:
        self._db    = db
        self._angel = angel
        self._mkt   = market_data
        self._ws    = ws_manager
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._phase_driver()),
            asyncio.create_task(self._push_dashboard_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._angel.close_session()

    # ── Phase driver ──────────────────────────────────────────────────────────

    async def _phase_driver(self) -> None:
        st = get_state()
        while True:
            now = _now()

            # Skip weekends
            if now.weekday() >= 5:
                await asyncio.sleep(3600)
                continue

            h, m = now.hour, now.minute

            if h < cfg.PREMARKET_HOUR or (h == cfg.PREMARKET_HOUR and m < cfg.PREMARKET_MIN):
                st.phase = TradingPhase.PRE_MARKET
                secs = _seconds_until(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN)
                print(f"PRE_MARKET — sleeping {secs:.0f}s until {cfg.PREMARKET_HOUR:02d}:{cfg.PREMARKET_MIN:02d}")
                await asyncio.sleep(secs)

            elif h < cfg.MARKET_OPEN_HOUR or (h == cfg.MARKET_OPEN_HOUR and m < cfg.MARKET_OPEN_MIN):
                await self._run_premarket()
                secs = _seconds_until(cfg.MARKET_OPEN_HOUR, cfg.MARKET_OPEN_MIN)
                await asyncio.sleep(secs)

            elif h < cfg.SCAN_START_HOUR or (h == cfg.SCAN_START_HOUR and m < cfg.SCAN_START_MIN):
                st.phase = TradingPhase.WAIT_ZONE
                await self._run_wait_zone()
                secs = _seconds_until(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN)
                await asyncio.sleep(secs)

            elif h < cfg.CUTOFF_HOUR or (h == cfg.CUTOFF_HOUR and m < cfg.CUTOFF_MIN):
                st.phase = TradingPhase.ACTIVE
                await self._run_active_phase()

            elif h < cfg.SESSION_END_HOUR or (h == cfg.SESSION_END_HOUR and m < cfg.SESSION_END_MIN):
                st.phase = TradingPhase.CUTOFF
                secs = _seconds_until(cfg.SESSION_END_HOUR, cfg.SESSION_END_MIN)
                await asyncio.sleep(secs)

            else:
                st.phase = TradingPhase.CLOSED
                await self._run_eod()
                # Sleep overnight
                await asyncio.sleep(_seconds_until(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN))

    # ── Phase handlers ────────────────────────────────────────────────────────

    async def _run_premarket(self) -> None:
        st = get_state()
        st.phase = TradingPhase.PRE_MARKET
        print("=== PRE-MARKET: Gemini AI filter ===")
        symbols = await fetch_gemini_shortlist()
        st.gemini_shortlist = symbols

        universe = await load_nse_universe()
        if symbols:
            st.active_watchlist = build_watchlist(universe, symbols)
        else:
            # Fallback: use a reasonable subset of the universe (first 40)
            fallback = universe[:40]
            st.active_watchlist = {s.symbol: s.token for s in fallback}
            print(f"Gemini unavailable — fallback watchlist: {len(fallback)} stocks")

    async def _run_wait_zone(self) -> None:
        st = get_state()
        st.phase = TradingPhase.WAIT_ZONE
        print("=== WAIT ZONE: Auth + historical data load ===")

        await self._angel.init_session()

        # Load historical candles for all watchlist stocks
        await self._load_all_historical()

        # Start live WebSocket feed
        self._mkt.start()

    async def _run_active_phase(self) -> None:
        st = get_state()
        st.phase = TradingPhase.ACTIVE
        print("=== ACTIVE: Scanning engine open ===")

        while True:
            now = _now()
            # Check cutoff
            if now.hour > cfg.CUTOFF_HOUR or (
                now.hour == cfg.CUTOFF_HOUR and now.minute >= cfg.CUTOFF_MIN
            ):
                break

            bar_label = _is_5m_bar_complete()
            if bar_label:
                st.last_5m_bar_time = bar_label
                await self._scan_all(bar_label)

            await asyncio.sleep(5)

    async def _run_eod(self) -> None:
        st = get_state()
        st.phase = TradingPhase.CLOSED
        await self._mkt.stop()

        positions = list(st.positions.values())
        total     = len(positions)
        winners   = sum(1 for p in positions if p.pnl > 0)
        total_pnl = sum(p.pnl for p in positions)

        try:
            await self._db.upsert_daily_stats(
                total_trades=total,
                winning_trades=winners,
                total_pnl=total_pnl,
                gemini_shortlist=st.gemini_shortlist,
            )
        except Exception as e:
            print(f"EOD stats error: {e}")

        # Reset daily state
        st.positions.clear()
        st.traded_today.clear()
        st.daily_pnl    = 0.0
        st.ltp.clear()
        st.candles_5m.clear()
        st.candles_1h.clear()
        st.candles_1d.clear()
        st.nifty_candles_5m.clear()
        st.nifty_candles_1d.clear()
        print(f"EOD: {total} trades, P&L ₹{total_pnl:.2f} — session closed")

    # ── Historical data loader ────────────────────────────────────────────────

    async def _load_all_historical(self) -> None:
        st = get_state()
        try:
            # Load multi-day 5m history for indicator warmup (ADX needs 29+ bars).
            # fetch_indicator_history returns {token: [Candle]} — store as-is.
            hist = await fetch_indicator_history(st.active_watchlist,
                                                 cfg.INTERVAL_5M, days_back=5)
            for token_key, candles in hist.items():
                st.candles_5m[token_key] = candles

            # Today's 1H and 1D candles
            today = await fetch_today_candles(
                st.active_watchlist,
                [cfg.INTERVAL_1H, cfg.INTERVAL_1D],
            )
            for token, frames in today.items():
                st.candles_1h[token] = frames.get(cfg.INTERVAL_1H, [])
                st.candles_1d[token] = frames.get(cfg.INTERVAL_1D, [])

            # NIFTY 50
            nifty_1d, nifty_5m = await fetch_nifty_candles()
            st.nifty_candles_1d.extend(nifty_1d)
            st.nifty_candles_5m.extend(nifty_5m)

            st.api_status = "API OK"
            print(f"Historical load complete: {len(st.candles_5m)} stocks with 5m data")
        except Exception as e:
            st.api_status = f"Load error: {e}"
            print(f"Historical load error: {e}")

    # ── Bar scanner ───────────────────────────────────────────────────────────

    async def _scan_all(self, bar_label: str) -> None:
        st = get_state()
        signals = []
        for symbol, token in list(st.active_watchlist.items()):
            try:
                sig = scan_stock(symbol, token)
                if sig:
                    signals.append(sig)
                    await self._db.log_scan(
                        symbol, bar_label, "SIGNAL",
                        f"Entry signal at {sig.ltp}",
                        {"rsi": sig.indicators.rsi, "adx": sig.indicators.adx,
                         "pattern": sig.indicators.candle_pattern},
                    )
                else:
                    result = st.last_scan_results.get(symbol, {})
                    await self._db.log_scan(
                        symbol, bar_label, "SKIP",
                        result.get("reason", "No signal"), None,
                    )
            except Exception as e:
                print(f"Scan error {symbol}: {e}")

        st.pending_signals = signals

        # Fire orders for all valid signals
        for sig in signals:
            if st.phase != TradingPhase.ACTIVE:
                break
            order_id = await self._angel.place_bracket_order(
                symbol        = sig.symbol,
                token         = sig.token,
                quantity      = sig.quantity,
                sl_offset     = sig.sl_offset,
                target_offset = sig.target_offset,
            )
            oid = order_id or "PAPER"
            pos = Position(
                symbol        = sig.symbol,
                token         = sig.token,
                entry_price   = sig.ltp,
                entry_time    = sig.bar_time,
                quantity      = sig.quantity,
                stop_loss     = sig.ltp - sig.sl_offset,
                target        = sig.ltp + sig.target_offset,
                sl_offset     = sig.sl_offset,
                target_offset = sig.target_offset,
                order_id      = oid,
                indicators    = sig.indicators,
                trend         = sig.trend,
            )
            st.positions[sig.symbol]    = pos
            st.traded_today.add(sig.symbol)
            try:
                await self._db.save_position(pos)
            except Exception as e:
                print(f"DB save_position error: {e}")

        if signals:
            print(f"Bar {bar_label}: {len(signals)} signal(s) → orders queued")

    # ── Dashboard broadcast ───────────────────────────────────────────────────

    async def _push_dashboard_loop(self) -> None:
        while True:
            try:
                payload = self._build_payload()
                await self._ws.broadcast(json.dumps(payload, default=str))
            except Exception as e:
                print(f"Dashboard push error: {e}")
            await asyncio.sleep(1)

    def _build_payload(self) -> dict:
        st    = get_state()
        now   = _now()
        clock = now.strftime("%H:%M:%S")

        positions_out = []
        for sym, pos in st.positions.items():
            ltp     = st.ltp.get(sym, pos.entry_price)
            live_pnl = (ltp - pos.entry_price) * pos.quantity
            positions_out.append({
                "symbol":    pos.symbol,
                "entry":     pos.entry_price,
                "entryTime": pos.entry_time,
                "qty":       pos.quantity,
                "sl":        pos.stop_loss,
                "target":    pos.target,
                "ltp":       ltp,
                "livePnl":   round(live_pnl, 2),
                "status":    pos.status.value,
                "orderId":   pos.order_id,
            })

        scans_out = [
            {"symbol": sym, **res}
            for sym, res in list(st.last_scan_results.items())[-20:]
        ]

        return {
            "type":       "STATE_UPDATE",
            "clock":      clock,
            "phase":      st.phase.value,
            "wsStatus":   st.ws_status,
            "apiStatus":  st.api_status,
            "watchlist":  list(st.active_watchlist.keys()),
            "geminiList": st.gemini_shortlist,
            "niftyLtp":   st.nifty_ltp,
            "dailyPnl":   round(st.daily_pnl, 2),
            "positions":  positions_out,
            "scanResults": scans_out,
            "lastBarTime": st.last_5m_bar_time,
        }
