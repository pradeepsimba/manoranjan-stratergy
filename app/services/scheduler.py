from __future__ import annotations

"""
Timing orchestrator — drives the trading session through its 5 phases:

  PRE_MARKET  → Gemini AI filter at 09:00
  WAIT_ZONE   → 09:15: historical data load + WebSocket subscribe
  ACTIVE      → 09:45: scan every completed 5-minute bar; paper-fill signals
  CUTOFF      → 14:30: no new entries; existing paper positions still tracked
  CLOSED      → 15:30: log daily summary
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine.entry_engine import scan_stock
from app.engine.position_manager import can_enter
from app.engine.trend_filter import compute_nifty_gates
from app.engine.watchlist import fetch_active_watchlist
from app.models import PositionStatus, TradingPhase
from app.services.gemini_filter import analyse_stocks
from app.services.historical_data import (
    fetch_indicator_history,
    fetch_nifty_candles,
    fetch_today_candles,
)
from app.services.paper_trade import check_tick_exit, force_close, place_paper_order
from app.state import get_state

if TYPE_CHECKING:
    from app.services.database import DatabaseService
    from app.services.market_data import MarketDataService
    from app.ws.dashboard_ws import DashboardWSManager

IST = ZoneInfo("Asia/Kolkata")

# One pool per process; thread_name_prefix helps with profiling / stack traces.
_SCAN_POOL = ThreadPoolExecutor(
    max_workers=cfg.SCAN_WORKERS,
    thread_name_prefix="scan",
)


def _now() -> datetime:
    return datetime.now(IST)


def _seconds_until(hour: int, minute: int) -> float:
    now    = _now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max(0.0, (target - now).total_seconds())


def _past(hour: int, minute: int) -> bool:
    """True once the wall clock has reached hour:minute (IST)."""
    now = _now()
    return now.hour > hour or (now.hour == hour and now.minute >= minute)


def _scan_chunk(items, nifty_gates):
    """
    Scan a chunk of (symbol, token) pairs in one worker thread and return the
    list of signals. Batching into ~SCAN_WORKERS chunks (instead of one pool
    task per stock) collapses hundreds of run_in_executor dispatches per cycle
    down to a handful — the dispatch/await happens on the single event-loop
    thread, so that churn is the tick-wise bottleneck at 500 stocks.
    """
    out = []
    for sym, tok in items:
        try:
            sig = scan_stock(sym, tok, nifty_gates)
        except Exception as e:
            # Isolate per stock — one bad symbol must not kill the whole chunk.
            print(f"Scan error ({sym}): {e}")
            continue
        if sig is not None:
            out.append(sig)
    return out


class SchedulerService:
    def __init__(
        self,
        db:          "DatabaseService",
        market_data: "MarketDataService",
        ws_manager:  "DashboardWSManager",
    ) -> None:
        self._db    = db
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

    # ── Phase driver ──────────────────────────────────────────────────────────

    async def _phase_driver(self) -> None:
        st = get_state()
        while True:
            try:
                now = _now()
                if now.weekday() >= 5:
                    await asyncio.sleep(3600)
                    continue

                h, m = now.hour, now.minute

                if h < cfg.PREMARKET_HOUR or (h == cfg.PREMARKET_HOUR and m < cfg.PREMARKET_MIN):
                    st.phase = TradingPhase.PRE_MARKET
                    await asyncio.sleep(_seconds_until(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN))

                elif h < cfg.MARKET_OPEN_HOUR or (h == cfg.MARKET_OPEN_HOUR and m < cfg.MARKET_OPEN_MIN):
                    await self._run_premarket()
                    await asyncio.sleep(_seconds_until(cfg.MARKET_OPEN_HOUR, cfg.MARKET_OPEN_MIN))

                elif h < cfg.SCAN_START_HOUR or (h == cfg.SCAN_START_HOUR and m < cfg.SCAN_START_MIN):
                    st.phase = TradingPhase.WAIT_ZONE
                    await self._run_wait_zone()
                    await asyncio.sleep(_seconds_until(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN))

                elif h < cfg.CUTOFF_HOUR or (h == cfg.CUTOFF_HOUR and m < cfg.CUTOFF_MIN):
                    st.phase = TradingPhase.ACTIVE
                    await self._run_active_phase()

                elif h < cfg.SESSION_END_HOUR or (h == cfg.SESSION_END_HOUR and m < cfg.SESSION_END_MIN):
                    st.phase = TradingPhase.CUTOFF
                    await asyncio.sleep(_seconds_until(cfg.SESSION_END_HOUR, cfg.SESSION_END_MIN))

                else:
                    st.phase = TradingPhase.CLOSED
                    await self._run_eod()
            except asyncio.CancelledError:
                raise   # let shutdown cancel cleanly
            except Exception as e:
                # A handler crash must not kill the driver; back off briefly and retry.
                print(f"Phase driver error: {e}")
                await asyncio.sleep(5)
                await asyncio.sleep(_seconds_until(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN))

    # ── Phase handlers ────────────────────────────────────────────────────────

    async def _run_premarket(self) -> None:
        st = get_state()
        st.phase = TradingPhase.PRE_MARKET

        # Step 1: fetch ranked high-volume stocks from the custom server
        print("=== PRE-MARKET: Fetching stock list from client status ===")
        full_watchlist = await fetch_active_watchlist()
        if not full_watchlist:
            print("Client status returned empty list — check server")
            return

        # Step 2: grounded Gemini screen — names (not raw tokens) go to the AI,
        # which returns a clean JSON array of BULLISH symbols.
        print(f"=== PRE-MARKET: Gemini grounded screen of {len(full_watchlist)} stocks ===")
        bullish = await analyse_stocks(list(full_watchlist.keys()))

        if bullish:
            bullish_set = {s.upper() for s in bullish}
            filtered = {
                name: tok
                for name, tok in full_watchlist.items()
                if name.upper() in bullish_set
            }
            # If nothing matched after mapping back, fall back to the full list
            st.active_watchlist = filtered if filtered else full_watchlist
        else:
            # Empty list → Gemini unavailable/failed → trade the full list
            st.active_watchlist = full_watchlist

        st.gemini_shortlist = list(st.active_watchlist.keys())
        print(f"=== PRE-MARKET done: {len(st.active_watchlist)} stocks will be scanned ===")

    async def _run_wait_zone(self) -> None:
        st = get_state()
        st.phase = TradingPhase.WAIT_ZONE
        print("=== WAIT ZONE: Loading historical data ===")
        await self._load_all_historical()
        self._mkt.start()

    async def _run_active_phase(self) -> None:
        """
        Tick-wise engine. Runs from 09:45 until 15:30, every TICK_EVAL_INTERVAL_MS:
          • Exits  — check every open position's live price vs SL/target (always).
          • Entries — re-evaluate every stock that ticked since the last cycle on
            its forming bar; fill the ones whose 7 signals align (until cutoff).
        Heavy indicator math runs in the thread pool (TA-Lib releases the GIL);
        fills/exits/DB stay on the event-loop thread.
        """
        print("=== ACTIVE: tick-wise engine open ===")
        st       = get_state()
        loop     = asyncio.get_running_loop()
        interval = max(0.0, cfg.TICK_EVAL_INTERVAL_MS / 1000.0)

        while not _past(cfg.SESSION_END_HOUR, cfg.SESSION_END_MIN):
            try:
                in_cutoff = _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN)
                st.phase  = TradingPhase.CUTOFF if in_cutoff else TradingPhase.ACTIVE

                await self._tick_exits()
                if not in_cutoff:
                    await self._tick_entries(loop)
            except Exception as e:
                # Never let one bad cycle kill the engine for the rest of the day.
                print(f"Tick loop error: {e}")

            await asyncio.sleep(interval)

    async def _tick_exits(self) -> None:
        """Tick-wise SL/target check against the live price for open positions."""
        st = get_state()
        for symbol in list(st.positions.keys()):
            ltp = st.ltp.get(symbol)
            if not ltp:
                continue
            try:
                closed = check_tick_exit(symbol, ltp)
                if closed:
                    await self._db.update_position_exit(
                        symbol     = closed.symbol,
                        exit_price = closed.exit_price,
                        exit_time  = closed.exit_time,
                        pnl        = closed.pnl,
                    )
            except Exception as e:
                print(f"Tick exit error ({symbol}): {e}")

    async def _tick_entries(self, loop) -> None:
        """Re-evaluate every stock that ticked since the last cycle; fill aligned signals."""
        st = get_state()

        # Snapshot-and-clear the dirty set (atomic rebind; a tick landing mid-swap
        # is simply picked up next cycle).
        dirty, st.dirty_ticks = st.dirty_ticks, set()
        if not dirty:
            return
        if (len(st.positions) >= cfg.MAX_CONCURRENT_POSITIONS
                or st.daily_pnl <= -cfg.DAILY_LOSS_LIMIT):
            return

        with st._nifty_lock:
            nifty_gates = compute_nifty_gates(
                st.nifty_ltp, st.nifty_candles_1d, st.nifty_candles_5m,
            )

        items = [(sym, tok) for sym, tok in st.active_watchlist.items() if tok in dirty]
        if not items:
            return

        # Partition into ≤ SCAN_WORKERS chunks → one pool task per worker instead
        # of one per stock. Keeps full parallelism while cutting event-loop
        # dispatch overhead ~30×.
        size   = max(1, (len(items) + cfg.SCAN_WORKERS - 1) // cfg.SCAN_WORKERS)
        chunks = [items[i : i + size] for i in range(0, len(items), size)]
        tasks  = [loop.run_in_executor(_SCAN_POOL, _scan_chunk, c, nifty_gates)
                  for c in chunks]
        results = await asyncio.gather(*tasks)
        signals = [sig for chunk_res in results for sig in chunk_res]

        for sig in signals:
            ok, _ = can_enter(sig.symbol, st.positions, st.traded_today, st.daily_pnl)
            if not ok:
                continue
            pos = place_paper_order(
                symbol        = sig.symbol,
                token         = sig.token,
                quantity      = sig.quantity,
                entry_price   = sig.ltp,
                sl_offset     = sig.sl_offset,
                target_offset = sig.target_offset,
            )
            pos.indicators = sig.indicators
            pos.trend      = sig.trend
            try:
                await self._db.save_position(pos)
            except Exception as e:
                print(f"DB save_position error ({sig.symbol}): {e}")

    async def _run_eod(self) -> None:
        st = get_state()

        # Square off anything still open at the last known price before the feed
        # stops, so every trade has a recorded exit and P&L.
        for symbol in list(st.positions.keys()):
            pos        = st.positions[symbol]
            exit_price = st.ltp.get(symbol, pos.entry_price)
            closed     = force_close(symbol, exit_price)
            if closed:
                try:
                    await self._db.update_position_exit(
                        symbol     = closed.symbol,
                        exit_price = closed.exit_price,
                        exit_time  = closed.exit_time,
                        pnl        = closed.pnl,
                    )
                except Exception as e:
                    print(f"EOD square-off DB error ({symbol}): {e}")

        await self._mkt.stop()

        # All trades are now closed (square-off moved them to closed_positions).
        trades  = st.closed_positions
        total   = len(trades)
        winners = sum(1 for p in trades if p.pnl > 0)

        try:
            await self._db.upsert_daily_stats(
                total_trades     = total,
                winning_trades   = winners,
                total_pnl        = st.daily_pnl,
                gemini_shortlist = st.gemini_shortlist,
            )
        except Exception as e:
            print(f"EOD stats error: {e}")

        print(
            f"=== EOD: {total} trades | {winners} winners | "
            f"Daily PnL ₹{st.daily_pnl:+.2f} ==="
        )

        st.positions.clear()
        st.closed_positions.clear()
        st.traded_today.clear()
        st.daily_pnl = 0.0
        st.ltp.clear()
        st.candles_5m.clear()
        st.candles_1h.clear()
        st.candles_1d.clear()
        st.nifty_candles_5m.clear()
        st.nifty_candles_1d.clear()
        st.dirty_ticks.clear()
        st.last_5m_bar_time = None

    # ── Historical data loader ────────────────────────────────────────────────

    async def _load_all_historical(self) -> None:
        st = get_state()
        try:
            hist = await fetch_indicator_history(
                st.active_watchlist, cfg.INTERVAL_5M, days_back=5
            )
            for token_key, candles in hist.items():
                st.candles_5m[token_key] = candles

            today = await fetch_today_candles(
                st.active_watchlist, [cfg.INTERVAL_1H, cfg.INTERVAL_1D]
            )
            for token_key, frames in today.items():
                st.candles_1h[token_key] = frames.get(cfg.INTERVAL_1H, [])
                st.candles_1d[token_key] = frames.get(cfg.INTERVAL_1D, [])

            nifty_1d, nifty_5m = await fetch_nifty_candles()
            st.nifty_candles_1d.extend(nifty_1d)
            st.nifty_candles_5m.extend(nifty_5m)

            st.api_status = "API OK"
            print(f"Historical load complete: {len(st.candles_5m)} stocks with 5m data")
        except Exception as e:
            st.api_status = f"Load error: {e}"
            print(f"Historical load error: {e}")

    # ── Dashboard broadcast ───────────────────────────────────────────────────

    async def _push_dashboard_loop(self) -> None:
        while True:
            try:
                # Skip building the payload entirely when no browser is watching.
                if self._ws.count() > 0:
                    await self._ws.broadcast(
                        json.dumps(self._build_payload(), default=str)
                    )
            except Exception as e:
                print(f"Dashboard push error: {e}")
            await asyncio.sleep(1)

    def _build_payload(self) -> dict:
        st    = get_state()
        clock = _now().strftime("%H:%M:%S")

        # Open positions (live P&L) followed by the day's closed trades (final P&L).
        positions_out = []
        for pos in list(st.positions.values()) + st.closed_positions:
            if pos.status == PositionStatus.OPEN:
                ltp      = st.ltp.get(pos.symbol, pos.entry_price)
                live_pnl = round((ltp - pos.entry_price) * pos.quantity, 2)
            else:
                ltp      = pos.exit_price if pos.exit_price is not None else pos.entry_price
                live_pnl = pos.pnl
            positions_out.append({
                "symbol":    pos.symbol,
                "entry":     pos.entry_price,
                "entryTime": pos.entry_time,
                "qty":       pos.quantity,
                "sl":        pos.stop_loss,
                "target":    pos.target,
                "ltp":       ltp,
                "livePnl":   live_pnl,
                "status":    pos.status.value,
                "orderId":   pos.order_id,
            })

        return {
            "type":        "STATE_UPDATE",
            "clock":       clock,
            "phase":       st.phase.value,
            "wsStatus":    st.ws_status,
            "apiStatus":   st.api_status,
            "watchlist":   list(st.active_watchlist.keys()),
            "geminiList":  st.gemini_shortlist,
            "niftyLtp":    st.nifty_ltp,
            "dailyPnl":    round(st.daily_pnl, 2),
            "positions":   positions_out,
            "scanResults": [
                {"symbol": sym, **res}
                for sym, res in list(st.last_scan_results.items())[-20:]
            ],
            "lastBarTime": st.last_5m_bar_time,
        }
