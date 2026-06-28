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
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine.entry_engine import scan_stock
from app.engine.watchlist import build_watchlist, load_nse_universe
from app.models import PositionStatus, TradingPhase
from app.services.gemini_filter import fetch_gemini_shortlist
from app.services.historical_data import (
    fetch_indicator_history,
    fetch_nifty_candles,
    fetch_today_candles,
)
from app.services.paper_trade import check_paper_exits, place_paper_order
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


def _is_5m_bar_complete() -> str | None:
    """
    Return "HH:MM" label of the most recently closed 5-minute bar if it hasn't
    been scanned yet, else None. Waits 5 s after bar close for data to arrive.
    """
    now     = _now()
    bar_min = (now.minute // 5) * 5
    closed  = now.replace(minute=bar_min, second=0, microsecond=0)
    if (now - closed).total_seconds() < 5:
        return None
    label = closed.strftime("%H:%M")
    return None if get_state().last_5m_bar_time == label else label


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
            fallback = universe[:40]
            st.active_watchlist = {s.symbol: s.token for s in fallback}
            print(f"Gemini unavailable — fallback watchlist: {len(fallback)} stocks")

    async def _run_wait_zone(self) -> None:
        st = get_state()
        st.phase = TradingPhase.WAIT_ZONE
        print("=== WAIT ZONE: Loading historical data ===")
        await self._load_all_historical()
        self._mkt.start()

    async def _run_active_phase(self) -> None:
        print("=== ACTIVE: Scanning engine open ===")
        while True:
            now = _now()
            if now.hour > cfg.CUTOFF_HOUR or (
                now.hour == cfg.CUTOFF_HOUR and now.minute >= cfg.CUTOFF_MIN
            ):
                break
            bar_label = _is_5m_bar_complete()
            if bar_label:
                get_state().last_5m_bar_time = bar_label
                await self._on_bar_close(bar_label)
            await asyncio.sleep(5)

    async def _run_eod(self) -> None:
        await self._mkt.stop()
        st        = get_state()
        positions = list(st.positions.values())
        total     = len(positions)
        winners   = sum(1 for p in positions if p.pnl > 0)

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
        st.traded_today.clear()
        st.daily_pnl = 0.0
        st.ltp.clear()
        st.candles_5m.clear()
        st.candles_1h.clear()
        st.candles_1d.clear()
        st.nifty_candles_5m.clear()
        st.nifty_candles_1d.clear()
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

    # ── Bar close handler ─────────────────────────────────────────────────────

    async def _on_bar_close(self, bar_label: str) -> None:
        st   = get_state()
        loop = asyncio.get_running_loop()

        # Step 1: exit checks — sequential (max 3 open positions, negligible cost)
        # Runs before scan tasks are spawned so no thread contention on candles.
        for token in list(st.active_watchlist.values()):
            candles = st.candles_5m.get(token, [])
            if len(candles) < 2:
                continue
            last = candles[-2]          # the bar that just closed
            try:
                closed_pos = check_paper_exits(token, last.high, last.low)
                if closed_pos:
                    await self._db.update_position_exit(
                        symbol     = closed_pos.symbol,
                        exit_price = closed_pos.exit_price,
                        exit_time  = closed_pos.exit_time,
                        pnl        = closed_pos.pnl,
                    )
            except Exception as e:
                print(f"Exit check error ({token}): {e}")

        if st.phase != TradingPhase.ACTIVE:
            return

        # Step 2: one NIFTY snapshot for the whole bar — all 500 workers share it
        # so nobody needs to acquire _nifty_lock from inside a thread.
        with st._nifty_lock:
            nifty_snap = (
                list(st.nifty_candles_5m),
                list(st.nifty_candles_1d),
                st.nifty_ltp,
            )

        # Step 3: fan out to thread pool — pandas-ta releases GIL, giving real
        # CPU parallelism. 500 stocks with 16 workers ≈ 150 ms vs ~5 s sequential.
        watchlist_items = list(st.active_watchlist.items())
        tasks = [
            loop.run_in_executor(_SCAN_POOL, scan_stock, sym, tok, nifty_snap)
            for sym, tok in watchlist_items
        ]
        results = await asyncio.gather(*tasks)
        signals = [r for r in results if r is not None]

        st.pending_signals = signals

        # Step 4: batch scan log — one executemany instead of 500 round-trips
        log_entries = []
        for sym, _ in watchlist_items:
            res    = st.last_scan_results.get(sym, {})
            action = "SIGNAL" if res.get("pass") else "SKIP"
            reason = res.get("reason", "No signal")
            ind_d  = None
            if action == "SIGNAL":
                s     = res.get("signal", {})
                ind_d = json.dumps({
                    "rsi":     s.get("rsi"),
                    "adx":     s.get("adx"),
                    "pattern": s.get("pattern"),
                })
            log_entries.append((sym, bar_label, action, reason, ind_d))

        asyncio.create_task(self._safe_batch_log(log_entries))

        # Step 5: paper fills
        for sig in signals:
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

        if signals:
            print(f"Bar {bar_label}: {len(signals)} paper fill(s)")

    async def _safe_batch_log(self, entries: list) -> None:
        try:
            await self._db.batch_log_scans(entries)
        except Exception as e:
            print(f"Batch scan log error: {e}")

    # ── Dashboard broadcast ───────────────────────────────────────────────────

    async def _push_dashboard_loop(self) -> None:
        while True:
            try:
                await self._ws.broadcast(json.dumps(self._build_payload(), default=str))
            except Exception as e:
                print(f"Dashboard push error: {e}")
            await asyncio.sleep(1)

    def _build_payload(self) -> dict:
        st    = get_state()
        clock = _now().strftime("%H:%M:%S")

        positions_out = []
        for sym, pos in st.positions.items():
            ltp      = st.ltp.get(sym, pos.entry_price)
            live_pnl = round((ltp - pos.entry_price) * pos.quantity, 2)
            positions_out.append({
                "symbol":    pos.symbol,
                "entry":     pos.entry_price,
                "entryTime": pos.entry_time,
                "qty":       pos.quantity,
                "sl":        pos.stop_loss,
                "target":    pos.target,
                "ltp":       ltp,
                "livePnl":   live_pnl if pos.status == PositionStatus.OPEN else pos.pnl,
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
