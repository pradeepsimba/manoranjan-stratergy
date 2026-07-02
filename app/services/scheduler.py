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
from collections import deque as _deque
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
from app.services.snapshot import apply_depth, stub_entry
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
    Scan a chunk of (symbol, token, tradeable) triples in one worker thread and
    return the list of entry signals. Batching into ~SCAN_WORKERS chunks collapses
    hundreds of run_in_executor dispatches per cycle down to a handful.
    tradeable=False stocks update indicator_snapshot but never generate a signal.
    """
    out = []
    for sym, tok, tradeable in items:
        try:
            sig = scan_stock(sym, tok, nifty_gates, tradeable=tradeable)
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
            asyncio.create_task(self._push_tick_updates_loop()),
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

                elif h < cfg.SESSION_END_HOUR or (h == cfg.SESSION_END_HOUR and m < cfg.SESSION_END_MIN):
                    if _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN):
                        st.phase = TradingPhase.CUTOFF
                    elif _past(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN):
                        st.phase = TradingPhase.ACTIVE
                    else:
                        st.phase = TradingPhase.WAIT_ZONE

                    if not st.active_watchlist:
                        phase_name = st.phase.value
                        print(f"=== RECOVERY: restarted during {phase_name} — running premarket + load ===")
                        st.api_status = "Recovery: fetching watchlist…"
                        await self._run_premarket()
                        if not st.active_watchlist:
                            # Client-status fetch failed — retry in 60s instead
                            # of entering the active loop watchlist-less, which
                            # would idle the engine for the rest of the day.
                            st.api_status = "Recovery failed — retrying in 60s"
                            await asyncio.sleep(60)
                            continue

                    if not self._mkt._running:
                        st.api_status = "Recovery: loading historical data…"
                        await self._run_wait_zone()

                    # Restore phase
                    if _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN):
                        st.phase = TradingPhase.CUTOFF
                    elif _past(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN):
                        st.phase = TradingPhase.ACTIVE
                    else:
                        st.phase = TradingPhase.WAIT_ZONE

                    await self._run_active_phase()

                else:
                    st.phase = TradingPhase.CLOSED
                    await self._run_eod()
                    # Sleep until next premarket so the loop doesn't spin (which
                    # would re-run EOD and overwrite the day's stats with zeros).
                    await asyncio.sleep(_seconds_until(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN))
            except asyncio.CancelledError:
                raise   # let shutdown cancel cleanly
            except Exception as e:
                # A handler crash must not kill the driver — short backoff and retry.
                # (Must NOT sleep-until-premarket here, or one transient error
                # would freeze the engine for the rest of the trading day.)
                print(f"Phase driver error: {e}")
                await asyncio.sleep(5)

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

        # Persist the full pre-Gemini list so the indicators page can show all stocks.
        st.full_watchlist = full_watchlist
        # token_to_name maps ALL high-volume tokens → names for the tick loop.
        st.token_to_name  = {tok: name for name, tok in full_watchlist.items()}

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
        else:
            filtered = {}

        if filtered:
            st.active_watchlist = filtered
        else:
            # Gemini unavailable/failed, or none of its names mapped back →
            # fall back to the CAPPED head of the full list. An uncapped
            # fallback would subscribe every stock and overflow the WS server's
            # per-connection buffer (1009 close).
            items = list(full_watchlist.items())[:cfg.GEMINI_MAX_STOCKS]
            st.active_watchlist = dict(items)

        st.gemini_shortlist = list(st.active_watchlist.keys())
        print(
            f"=== PRE-MARKET done: {len(st.active_watchlist)} tradeable / "
            f"{len(st.full_watchlist)} total stocks ==="
        )

    async def _run_wait_zone(self) -> None:
        st = get_state()
        st.phase = TradingPhase.WAIT_ZONE
        print("=== WAIT ZONE: Loading historical data ===")
        await self._load_all_historical()
        # Stop any existing WS connections before (re)starting so that recovery
        # runs (premarket missed → wait_zone called twice) don't leak old tasks.
        if self._mkt._running:
            await self._mkt.stop()
        self._mkt.start()

        # Seed indicator_snapshot for ALL stocks immediately after loading history
        loop = asyncio.get_running_loop()
        asyncio.create_task(self._full_scan_all(loop))

    async def _run_active_phase(self) -> None:
        """
        Tick-wise engine. Runs from 09:45 until 15:30, every TICK_EVAL_INTERVAL_MS:
          • Exits  — check every open position's live price vs SL/target (always).
          • Entries — re-evaluate every stock that ticked since the last cycle on
            its forming bar; fill the ones whose 7 signals align (until cutoff).
          • Full scan — every 5 minutes, scan ALL full_watchlist stocks so non-Gemini
            stocks also appear in indicator_snapshot (WS is only subscribed to the
            Gemini subset; this is their only source of indicator updates).
        Heavy indicator math runs in the thread pool (TA-Lib releases the GIL);
        fills/exits/DB stay on the event-loop thread.
        """
        print("=== ACTIVE: tick-wise engine open ===")
        st       = get_state()
        loop     = asyncio.get_running_loop()
        interval = max(0.0, cfg.TICK_EVAL_INTERVAL_MS / 1000.0)

        # Seed indicator_snapshot for ALL stocks immediately on entry.
        asyncio.create_task(self._full_scan_all(loop))
        last_full_scan = loop.time()

        while not _past(cfg.SESSION_END_HOUR, cfg.SESSION_END_MIN):
            try:
                if _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN):
                    st.phase = TradingPhase.CUTOFF
                elif _past(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN):
                    st.phase = TradingPhase.ACTIVE
                else:
                    st.phase = TradingPhase.WAIT_ZONE

                await self._tick_exits()
                await self._tick_entries(loop)

                # Refresh all stocks every 5 minutes (non-Gemini stocks only update here)
                now_ts = loop.time()
                if now_ts - last_full_scan >= 300:
                    asyncio.create_task(self._full_scan_all(loop))
                    last_full_scan = now_ts
            except Exception as e:
                # Never let one bad cycle kill the engine for the rest of the day.
                print(f"Tick loop error: {e}")

            await asyncio.sleep(interval)

    async def _full_scan_all(self, loop) -> None:
        """
        Scan every stock in full_watchlist and update indicator_snapshot.
        Called once at ACTIVE entry and every 5 minutes thereafter.
        Non-Gemini stocks don't get WS ticks so this is their only source of
        live indicator data; Gemini stocks get re-scanned here too (cheap, and
        ensures the snapshot is populated even before the first WS tick arrives).
        """
        st = get_state()
        if not st.full_watchlist:
            return
        try:
            nifty_gates = self._nifty_gates_snapshot(st)

            tradeable_set = set(st.active_watchlist)
            items = [(name, tok, name in tradeable_set)
                     for name, tok in st.full_watchlist.items()]

            await self._scan_in_pool(loop, items, nifty_gates)
            # Returned signals are intentionally discarded — entries are handled
            # only by _tick_entries (which has the dirty-tick freshness guarantee).
        except Exception as e:
            print(f"Full scan error: {e}")

    @staticmethod
    def _nifty_gates_snapshot(st):
        """Copy the shared NIFTY series under its lock, then compute the gates."""
        with st._nifty_lock:
            nifty_ltp = st.nifty_ltp
            nifty_5m  = list(st.nifty_candles_5m)
        return compute_nifty_gates(nifty_ltp, nifty_5m)

    @staticmethod
    async def _scan_in_pool(loop, items, nifty_gates) -> list:
        """
        Partition (symbol, token, tradeable) triples into ≤ SCAN_WORKERS chunks —
        one pool task per worker instead of one per stock. Keeps full parallelism
        while cutting event-loop dispatch overhead ~30×. Returns merged signals.
        """
        size   = max(1, (len(items) + cfg.SCAN_WORKERS - 1) // cfg.SCAN_WORKERS)
        chunks = [items[i : i + size] for i in range(0, len(items), size)]
        results = await asyncio.gather(*[
            loop.run_in_executor(_SCAN_POOL, _scan_chunk, c, nifty_gates)
            for c in chunks
        ])
        return [sig for chunk_res in results for sig in chunk_res]

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

        nifty_gates = self._nifty_gates_snapshot(st)

        # Iterate the (small) dirty-token set directly — O(dirty), not O(watchlist).
        # token_to_name covers the FULL watchlist; tradeable_set is the Gemini subset.
        tradeable_set = set(st.active_watchlist)
        t2n   = st.token_to_name
        items = []
        for tok in dirty:
            name = t2n.get(tok)
            if name is not None:
                items.append((name, tok, name in tradeable_set))
        if not items:
            return

        signals = await self._scan_in_pool(loop, items, nifty_gates)

        # Delta pushes are now handled globally in _push_tick_updates_loop

        # Only check entries and place trades if we are in the ACTIVE phase!
        if st.phase != TradingPhase.ACTIVE:
            return

        # Only fill trades when capacity allows — scanning always runs so that
        # indicator_snapshot stays current even at max concurrent positions.
        if (len(st.positions) >= cfg.MAX_CONCURRENT_POSITIONS
                or st.daily_pnl <= -cfg.DAILY_LOSS_LIMIT):
            return

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

        # Only persist stats when this process actually ran a session. On a
        # restart after market close the state is fresh/empty — writing here
        # would overwrite the day's real row with zeros (ON CONFLICT DO UPDATE).
        if total > 0 or st.daily_pnl != 0.0 or st.gemini_shortlist:
            try:
                await self._db.upsert_daily_stats(
                    total_trades     = total,
                    winning_trades   = winners,
                    total_pnl        = st.daily_pnl,
                    gemini_shortlist = st.gemini_shortlist,
                )
            except Exception as e:
                print(f"EOD stats error: {e}")
        else:
            print("=== EOD: no session state in this process — daily_stats write skipped ===")

        print(
            f"=== EOD: {total} trades | {winners} winners | "
            f"Daily PnL ₹{st.daily_pnl:+.2f} ==="
        )

        st.positions.clear()
        st.closed_positions.clear()
        st.traded_today.clear()
        st.gemini_shortlist.clear()   # stats are written; a stale list must not
                                      # trigger tomorrow's write guard or linger
                                      # on the overnight dashboard
        st.daily_pnl = 0.0
        st.ltp.clear()
        st.candles_5m.clear()
        st.candles_1h.clear()
        st.candles_1d.clear()
        st.nifty_candles_5m.clear()
        st.nifty_candles_1d.clear()
        st.dirty_ticks.clear()
        st.dirty_ticks_push.clear()
        st.token_to_name.clear()
        st.full_watchlist.clear()
        st.active_watchlist.clear()
        st.clear_scan_results()
        st.last_5m_bar_time = None

    # ── Historical data loader ────────────────────────────────────────────────

    async def _load_all_historical(self) -> None:
        st = get_state()
        try:
            # Use the full watchlist so every high-volume stock has candle history —
            # needed because the indicators page shows ALL stocks, not just the
            # Gemini-selected subset.
            wl = st.full_watchlist if st.full_watchlist else st.active_watchlist
            hist = await fetch_indicator_history(
                wl, cfg.INTERVAL_5M, days_back=5
            )
            for token_key, candles in hist.items():
                st.candles_5m[token_key] = _deque(candles, maxlen=cfg.MAX_CANDLE_BUFFER)

            # Only 1H is needed now — the daily gate derives today's open from the
            # 5m session, so the (never-live-updated) 1d series is no longer fetched.
            today = await fetch_today_candles(wl, [cfg.INTERVAL_1H])
            for token_key, frames in today.items():
                st.candles_1h[token_key] = _deque(
                    frames.get(cfg.INTERVAL_1H, []), maxlen=cfg.MAX_CANDLE_BUFFER
                )

            # Replace (not extend) so a re-run of the loader can't accumulate
            # duplicate NIFTY bars, which would skew the index VWAP / daily-open.
            nifty_1d, nifty_5m = await fetch_nifty_candles()
            st.nifty_candles_1d.clear(); st.nifty_candles_1d.extend(nifty_1d)
            st.nifty_candles_5m.clear(); st.nifty_candles_5m.extend(nifty_5m)

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
                for sym, res in st.scan_snapshot()[-20:]
            ],
            "lastBarTime":       st.last_5m_bar_time,
            "indicatorSnapshot": self._build_indicator_snapshot(st),
        }

    async def _push_tick_updates_loop(self) -> None:
        """Pushes real-time LTP delta updates to the UI every 100ms in all active/wait/cutoff phases."""
        st = get_state()
        while True:
            try:
                if self._ws.count() > 0 and st.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF):
                    dirty, st.dirty_ticks_push = st.dirty_ticks_push, set()
                    if dirty:
                        t2n = st.token_to_name
                        snap_delta = {}
                        for tok in dirty:
                            sym = t2n.get(tok)
                            if sym is None:
                                continue
                            snap = st.indicator_snapshot.get(sym)
                            entry = dict(snap) if snap is not None else stub_entry()
                            entry["ltp"] = round(st.ltp.get(sym, 0.0), 2)
                            apply_depth(entry, st.depth.get(sym, {}))
                            snap_delta[sym] = entry

                        if snap_delta:
                            await self._ws.broadcast(
                                json.dumps({"type": "INDICATOR_UPDATE",
                                            "indicatorSnapshot": snap_delta}, default=str)
                            )
            except Exception as e:
                print(f"Tick delta push error: {e}")
            await asyncio.sleep(0.1)

    @staticmethod
    def _build_indicator_snapshot(st) -> dict:
        """
        Return indicator_snapshot extended with LTP-only stubs and always updated with
        the latest live LTP and order-book depth from st.ltp/st.depth, so prices/bids/asks update in real-time.
        (Stub shape / depth merge live in app.services.snapshot — extend them there.)
        """
        snap = {}
        for sym in st.full_watchlist:
            live_ltp = round(st.ltp.get(sym, 0.0), 2)
            existing = st.indicator_snapshot.get(sym)
            if existing is not None:
                entry = dict(existing)
                entry["ltp"] = live_ltp if live_ltp > 0 else entry.get("ltp", 0.0)
            else:
                entry = stub_entry()
                entry["ltp"] = live_ltp
            apply_depth(entry, st.depth.get(sym, {}))
            snap[sym] = entry
        return snap
