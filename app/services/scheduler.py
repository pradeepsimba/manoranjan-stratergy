from __future__ import annotations

"""
Timing orchestrator for the equity paper-trading platform:

  PRE_MARKET -> idle, no live feed
  OPEN       -> market-data feed running; orders accepted; every tick,
                resting LIMIT orders on that token are checked for a fill
  CLOSED     -> at MIS_SQUAREOFF time: auto square-off every open MIS
                position; at MARKET_CLOSE: stop the feed for the day

Nothing here decides WHAT to trade — see app/engine/orders.py for the
(non-strategy) order-matching mechanics this loop calls into.
"""

import asyncio
import json
from collections import deque as _deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine import orders as order_engine
from app.models import MarketPhase
from app.services.historical_data import fetch_indicator_history
from app.services.instrument_discovery import discover_and_verify
from app.services.market_data import MarketDataService
from app.state import get_state

if TYPE_CHECKING:
    from app.services.database import DatabaseService
    from app.ws.account_ws import AccountWSManager
    from app.ws.market_ws import MarketWSManager

IST = ZoneInfo("Asia/Kolkata")


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


async def _sleep_toward(hour: int, minute: int) -> None:
    """
    Sleep TOWARD hour:minute in <=30s chunks instead of one long sleep. The
    phase driver re-evaluates its branch conditions every wake-up, so runtime
    changes to the session timings take effect within seconds.
    """
    await asyncio.sleep(min(_seconds_until(hour, minute), 30.0))


class SchedulerService:
    def __init__(
        self,
        db:          "DatabaseService",
        market_data: "MarketDataService",
        market_ws:   "MarketWSManager",
        account_ws:  "AccountWSManager",
    ) -> None:
        self._db      = db
        self._mkt     = market_data
        self._mkt_ws  = market_ws
        self._acct_ws = account_ws
        self._tasks: List[asyncio.Task] = []
        # Once-per-day guard: the phase driver wakes every <=30s (so timing
        # settings are dynamic), so square-off must self-deduplicate by date.
        self._squareoff_date: str | None = None
        self._instruments: List[Dict[str, str]] = []   # cached [{"token","name"}]

    async def start(self) -> None:
        await self._ensure_instruments()
        # Load last-known candles immediately, independent of phase/live feed,
        # so the UI has a last-close price to show even if the process starts
        # while the market is CLOSED/PRE_MARKET (AppState is in-memory only
        # and doesn't survive a restart). The OPEN-phase handler below still
        # reloads + starts the feed on its own schedule.
        await self._load_all_historical()
        self._tasks = [
            asyncio.create_task(self._phase_driver()),
            asyncio.create_task(self._tick_loop()),
            asyncio.create_task(self._push_market_state_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._mkt.stop()

    async def _ensure_instruments(self) -> None:
        try:
            count = await self._db.count_instruments()
        except Exception as e:
            print(f"Instrument count check failed: {e}")
            count = 0
        if count == 0:
            print("=== No tradable instruments on record — running discovery ===")
            try:
                rows = await discover_and_verify()
                await self._db.upsert_instruments(rows)
            except Exception as e:
                print(f"Instrument discovery failed: {e}")
        try:
            rows = await self._db.get_tradable_instruments()
            self._instruments = [{"token": r["token"], "name": r["name"]} for r in rows]
        except Exception as e:
            print(f"Loading tradable instruments failed: {e}")
            self._instruments = []

    # ── Phase driver ──────────────────────────────────────────────────────────

    async def _phase_driver(self) -> None:
        st = get_state()
        eod_date: str | None = None
        while True:
            try:
                now = _now()
                if now.weekday() >= 5:
                    st.phase = MarketPhase.CLOSED
                    await asyncio.sleep(3600)
                    continue

                h, m  = now.hour, now.minute
                today = now.strftime("%Y-%m-%d")

                if h < cfg.MARKET_OPEN_HOUR or (h == cfg.MARKET_OPEN_HOUR and m < cfg.MARKET_OPEN_MIN):
                    st.phase = MarketPhase.PRE_MARKET
                    await _sleep_toward(cfg.MARKET_OPEN_HOUR, cfg.MARKET_OPEN_MIN)

                elif h < cfg.MARKET_CLOSE_HOUR or (h == cfg.MARKET_CLOSE_HOUR and m < cfg.MARKET_CLOSE_MIN):
                    st.phase = MarketPhase.OPEN
                    if not self._mkt._running:
                        await self._run_market_open()

                    if (_past(cfg.MIS_SQUAREOFF_HOUR, cfg.MIS_SQUAREOFF_MIN)
                            and self._squareoff_date != today):
                        await self._run_mis_squareoff()
                        self._squareoff_date = today

                    await asyncio.sleep(1)

                else:
                    st.phase = MarketPhase.CLOSED
                    if eod_date != today:
                        await self._run_eod()
                        eod_date = today
                    await _sleep_toward(cfg.MARKET_OPEN_HOUR, cfg.MARKET_OPEN_MIN)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Phase driver error: {e}")
                await asyncio.sleep(5)

    # ── Phase handlers ────────────────────────────────────────────────────────

    async def _run_market_open(self) -> None:
        print("=== MARKET OPEN: loading historical data + starting live feed ===")
        await self._load_all_historical()
        self._mkt.start(self._instruments)

    async def _run_mis_squareoff(self) -> None:
        st = get_state()
        price_lookup = dict(st.ltp)
        try:
            events = await order_engine.eod_square_off_all_mis(self._db, price_lookup)
        except Exception as e:
            print(f"MIS square-off error: {e}")
            return
        for ev in events:
            user_id = ev.pop("user_id", None)
            if user_id is not None:
                await self._acct_ws.send_to_user(user_id, json.dumps(ev, default=str))
        print(f"=== MIS square-off: {len(events)} position(s) closed ===")

    async def _run_eod(self) -> None:
        print("=== EOD: stopping live feed for the day ===")
        await self._mkt.stop()

    # ── Historical data loader ────────────────────────────────────────────────

    async def _load_all_historical(self) -> None:
        st = get_state()
        if not self._instruments:
            return
        watchlist = {i["name"]: i["token"] for i in self._instruments}
        try:
            hist = await fetch_indicator_history(watchlist, cfg.INTERVAL_5M, days_back=5)
            for token, candles in hist.items():
                st.candles_5m[token] = _deque(candles, maxlen=cfg.MAX_CANDLE_BUFFER)
                st.tick_version[token] = st.tick_version.get(token, 0) + 1
            st.api_status = "API OK"
            print(f"Historical load complete: {len(hist)}/{len(self._instruments)} instruments")
        except Exception as e:
            st.api_status = f"Load error: {e}"
            print(f"Historical load error: {e}")

    # ── Tick loop: limit-order matching + live-price ticker delta ────────────
    # ONE consumer of dirty_ticks_push (draining it in two different loops
    # would race over who gets which tokens each cycle) — every dirty token
    # is checked against resting LIMIT orders AND folded into the broadcast
    # delta, in the same pass.

    async def _tick_loop(self) -> None:
        st = get_state()
        while True:
            try:
                if st.phase == MarketPhase.OPEN:
                    dirty, st.dirty_ticks_push = st.dirty_ticks_push, set()
                    if dirty:
                        await self._match_limit_orders(dirty)
                        if self._mkt_ws.count() > 0:
                            prices = {tok: st.ltp.get(tok, 0.0) for tok in dirty}
                            await self._mkt_ws.broadcast(
                                json.dumps({"type": "WATCHLIST_TICK", "prices": prices}, default=str)
                            )
            except Exception as e:
                print(f"Tick loop error: {e}")
            await asyncio.sleep(max(0.01, cfg.TICK_EVAL_INTERVAL_MS / 1000.0))

    async def _match_limit_orders(self, tokens: set) -> None:
        st = get_state()
        for token in tokens:
            ltp = st.ltp.get(token, 0.0)
            if ltp <= 0:
                continue
            try:
                events = await order_engine.match_pending_limit_orders(self._db, token, ltp)
            except Exception as e:
                print(f"Limit-match error for {token}: {e}")
                continue
            for ev in events:
                order = ev.get("order")
                if order:
                    await self._acct_ws.send_to_user(order["user_id"], json.dumps(ev, default=str))

    # ── Shared market-data broadcast (public, no auth) ────────────────────────

    async def _push_market_state_loop(self) -> None:
        while True:
            try:
                if self._mkt_ws.count() > 0:
                    await self._mkt_ws.broadcast(json.dumps(self._build_market_payload(), default=str))
            except Exception as e:
                print(f"Market state push error: {e}")
            await asyncio.sleep(1)

    def _build_market_payload(self) -> Dict[str, Any]:
        st = get_state()
        return {
            "type":      "MARKET_STATE",
            "clock":     _now().strftime("%H:%M:%S"),
            "phase":     st.phase.value,
            "wsStatus":  st.ws_status,
            "apiStatus": st.api_status,
        }
