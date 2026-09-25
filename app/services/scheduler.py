from __future__ import annotations

"""
Timing orchestrator — drives the Bank Nifty options paper-trading session:

  PRE_MARKET  → idle (fixed instrument universe — nothing to fetch/screen)
  WAIT_ZONE   → 09:15: historical data load + WebSocket subscribe
  ACTIVE      → 09:30: evaluate every newly-closed 5m BankNifty bar; manage
                the single active trade's exit every ~100ms
  CUTOFF      → 15:00: no new entries; exit management keeps running
  CLOSED      → 15:30: square off, log daily summary
"""

import asyncio
import copy
import json
import math
from collections import deque as _deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine import bn_breakout
from app.engine.bn_entry_exit import _leader_qty_surge, _stock_qty_threshold, evaluate_entry
from app.engine.bn_pricing import build_monthly_option_symbol, get_atm_strike
from app.engine.bn_pricing import get_next_expiry as bn_get_next_expiry
from app.engine.nf_entry_exit import _leader_qty_surge as _nf_leader_qty_surge
from app.engine.nf_entry_exit import _stock_qty_threshold as _nf_stock_qty_threshold
from app.engine.nf_entry_exit import evaluate_entry as nf_evaluate_entry
from app.engine.nf_pricing import build_weekly_option_symbol
from app.engine.nf_pricing import get_atm_strike as nf_get_atm_strike
from app.engine.nf_pricing import get_next_expiry as nf_get_next_expiry
from app.models import BNTrade, NFTrade, PositionStatus, TradingPhase, closed_tail_closes, iv_lookback_closes
from app.services import bn_trade, nf_trade, price_alerts
from app.services.historical_data import fetch_indicator_history
from app.services.market_data import MarketDataService
from app.services.settings import BN_FUNDS_KEY
from app.state import get_state

if TYPE_CHECKING:
    from app.services.database import DatabaseService
    from app.ws.dashboard_ws import DashboardWSManager

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
    Sleep TOWARD hour:minute in ≤30s chunks instead of one long sleep. The
    phase driver re-evaluates its branch conditions every wake-up, so runtime
    changes to the session timings take effect within seconds.
    """
    await asyncio.sleep(min(_seconds_until(hour, minute), 30.0))


# ── Stock Candles panel (c.html port, unrelated to the BN trading strategy) ──
_STOCK_TABLE_BARS   = 50   # bars per stock sent for the live candle table (client "Last N bars" selector trims further) —
                           # kept well under MAX_CANDLE_BUFFER=300; pushed every 1s to every connected browser, so this
                           # is a real bandwidth/CPU tradeoff, not free — don't raise it much further without reason.
_NUM_SIGNAL_CANDLES = 3    # c.html's default `numCandles` for updateGlobalSignal
_SR_15M_REFRESH_S   = 300  # c.html's own findSupportResistance runs infrequently too


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
        # Once-per-day guards: the phase driver wakes every ≤30s (so timing
        # settings are dynamic), so premarket/EOD must self-deduplicate by date.
        self._premarket_date: str | None = None
        self._eod_date:       str | None = None
        # Live ATM CE/PE watchlist (2026-09-18) — last (ce, pe) symbol pair
        # the watch was set to, so _tick_atm_watch only touches the WS
        # subscription when the built symbols actually change, not every
        # tick. Keyed on the SYMBOL PAIR, not just the strike (2026-09-25,
        # found in review — see _tick_atm_watch's docstring): a strike-only
        # key could miss a format-only change (nf_pricing.
        # build_weekly_option_symbol's weekly-vs-monthly-style format flips
        # at the Tuesday-15:30-IST expiry rollover independent of strike).
        self._bn_atm_watch_symbols: tuple[str, str] | None = None
        self._nf_atm_watch_symbols: tuple[str, str] | None = None
        # Resubscribe-rate debounce (2026-09-24, found in review) — the
        # engine became tick-DRIVEN that same day (see _run_active_phase),
        # which can call _tick_atm_watch far more often than the old fixed
        # 100ms timer ever did. Without a floor, spot hovering exactly at a
        # round-100/round-50 strike boundary during a burst of real ticks
        # could reconnect the option WS connection many times a second
        # instead of the ~10/sec ceiling the old timer naturally imposed —
        # see _tick_atm_watch for how this is used.
        self._bn_atm_watch_last_resub_at: datetime | None = None
        self._nf_atm_watch_last_resub_at: datetime | None = None
        # Previous tick's client-connected state, for _tick_alerts's
        # reconnect edge-reset (found in review, 2026-09-23) — see there.
        self._had_alert_clients = False

    async def start(self) -> None:
        await self._load_funds()
        await self._seed_synthetic_anchor()
        self._tasks = [
            asyncio.create_task(self._phase_driver()),
            asyncio.create_task(self._push_dashboard_loop()),
            asyncio.create_task(self._push_tick_updates_loop()),
            asyncio.create_task(self._refresh_15m_sr_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Was missing entirely (found in review, 2026-09-24) — this only
        # ever cancelled this service's OWN 4 tasks; MarketDataService's WS
        # connections (self._mkt) were previously stopped only from
        # _run_eod's 15:30 teardown or a WAIT_ZONE reconnect, never from
        # here. A mid-session graceful shutdown (e.g. `docker compose
        # restart`/SIGTERM outside the 15:30 EOD window, which main.py's
        # lifespan routes straight to this method) left every WS task/socket
        # running uncancelled while main.py's very next line
        # (db_service.close()) tore down the asyncpg pool underneath them —
        # any in-flight tick write could race a closing pool.
        await self._mkt.stop()

    async def _load_funds(self) -> None:
        st = get_state()
        try:
            stored = await self._db.get_app_settings()
        except Exception as e:
            print(f"Funds load failed (using default): {e}")
            stored = {}
        funds = stored.get(BN_FUNDS_KEY)
        loaded = float(funds) if isinstance(funds, (int, float)) else None
        # Sanity guard (2026-09-25, found in review) — a real, unexplained
        # corruption was found live in production: the persisted _BN_FUNDS
        # value had somehow become roughly a billion times its real
        # magnitude (₹100,000 starting capital showing as
        # ₹100,000,000,001,579.97 on the dashboard) despite every code path
        # that touches st.funds (this load, _settle's `+= trade.pnl` in
        # bn_trade.py/nf_trade.py, dashboard.reset_funds) checking out
        # correct on inspection — the exact mechanism could not be traced
        # back further than this process's own history. Rather than
        # silently keep trusting whatever value is sitting in the DB
        # forever once that happens, refuse anything that isn't a finite
        # number within a generous sane ceiling (1000x starting capital —
        # this scalp strategy's ~₹2-3/trade brackets and daily trade cap
        # make legitimately exceeding that essentially impossible) and fall
        # back to the configured starting capital instead, loudly, so a
        # recurrence is immediately visible in logs rather than silently
        # displayed forever on every dashboard load.
        ceiling = 1000.0 * max(cfg.BN_STARTING_FUNDS, 1.0)
        if loaded is not None and math.isfinite(loaded) and abs(loaded) <= ceiling:
            st.funds = loaded
        else:
            print(f"CRITICAL: persisted funds value {funds!r} is missing/non-finite/"
                  f"implausible (sane range ±₹{ceiling:,.2f}) — falling back to starting "
                  f"capital ₹{cfg.BN_STARTING_FUNDS:,.2f}. This should never happen from "
                  f"normal trading; if it recurs, treat it as a real bug to re-investigate.")
            st.funds = cfg.BN_STARTING_FUNDS

    async def _seed_synthetic_anchor(self) -> None:
        """
        Restore up to 14 days of this app's own self-recorded bn_index_bars
        archive into st.bn_index_candles_5m at startup, and seed the
        synthetic BankNifty index's anchor (see market_data.py's
        _update_synthetic_index) from the last close — a grounded anchor
        from actual past index levels, rather than a hardcoded guess.

        Without this, every restart began the composite RSI/MACD/EMA
        indicator gate (bn_signals.bn_composite_indicator, which hard-requires
        >=50 bars, ideally BN_INDICATOR_LOOKBACK_BARS=200) from a near-empty
        buffer — it only ever grows from live ticks otherwise, so the gate
        stayed stuck at "insufficient data" (RSI/MACD/EMA showing "—"/Neutral)
        for hours after every restart. Falls back to a placeholder anchor
        only if the archive is completely empty (e.g. first-ever run).
        """
        st = get_state()
        try:
            from_iso = (_now() - timedelta(days=14)).isoformat()
            to_iso   = _now().isoformat()
            bars = await self._db.get_bn_index_bars(from_iso, to_iso)
        except Exception as e:
            print(f"Synthetic index anchor seed failed: {e}")
            bars = []
        if bars:
            with st._bn_index_lock:
                for c in bars:
                    MarketDataService._upsert_list(st.bn_index_candles_5m, c)
            st.bn_synthetic_anchor = bars[-1].close
            print(f"Synthetic BankNifty index: restored {len(st.bn_index_candles_5m)} self-recorded "
                  f"bars from archive, anchor seeded at {st.bn_synthetic_anchor:.2f}")
        else:
            st.bn_synthetic_anchor = 55000.0
            print("Synthetic BankNifty index anchor: no self-recorded history yet, "
                  f"using placeholder {st.bn_synthetic_anchor:.2f}")

        try:
            nf_bars = await self._db.get_nf_index_bars(from_iso, to_iso)
        except Exception as e:
            print(f"NF synthetic index anchor seed failed: {e}")
            nf_bars = []
        if nf_bars:
            with st._nf_index_lock:
                for c in nf_bars:
                    MarketDataService._upsert_list(st.nf_index_candles_5m, c)
            st.nf_synthetic_anchor = nf_bars[-1].close
            print(f"Synthetic Nifty 50 index: restored {len(st.nf_index_candles_5m)} self-recorded "
                  f"bars from archive, anchor seeded at {st.nf_synthetic_anchor:.2f}")
        else:
            st.nf_synthetic_anchor = 25000.0
            print("Synthetic Nifty 50 index anchor: no self-recorded history yet, "
                  f"using placeholder {st.nf_synthetic_anchor:.2f}")

    async def _persist_funds(self) -> None:
        try:
            await self._db.set_app_settings({BN_FUNDS_KEY: get_state().funds})
        except Exception as e:
            print(f"Funds persist failed: {e}")

    async def _persist_closed_exit(self, closed, label: str) -> None:
        """
        Persists a just-closed trade's exit row + funds, with a short bounded
        retry (found in review, 2026-09-23). bn_trade.check_tick_exit/
        force_close (and the nf_trade mirror) already commit the close IN
        MEMORY — st.active_trade/active_trade_nf set to None, the trade
        appended to closed_trades — before this method ever runs. Without a
        retry, a single transient DB hiccup here (pool contention, a brief
        connection blip) used to be caught by the caller's try/except and
        just printed once: the `positions` row for this trade is left OPEN
        forever (or, if save_position itself had failed earlier, never
        written at all — update_position_exit's UPDATE...WHERE order_id=...
        AND status='OPEN' then silently matches zero rows), while the
        dashboard and st.closed_trades already show it closed — a
        permanent, invisible gap in the trade audit log with no reconciler
        anywhere in this app to catch it later. A couple of short retries
        cover the common transient case without meaningfully delaying the
        100ms tick loop (this only runs on an actual exit, not every tick);
        a final failure is logged with a CRITICAL prefix so it's at least
        greppable, since there's no dead-letter/reconciliation queue here.
        """
        delays = (0.0, 0.2, 0.6)
        last_exc: Optional[Exception] = None
        # Tracks the position-exit UPDATE separately from the funds persist
        # (found in review, 2026-09-23): update_position_exit's WHERE
        # order_id=... AND status='OPEN' now raises on a 0-row match (see
        # database.py) — correct for a genuine first-attempt failure, but a
        # naive retry-both-steps-every-time loop would re-call it after it
        # had ALREADY succeeded (e.g. this step commits, then _persist_funds
        # transiently fails) — the row is 'CLOSED' by then, so the retry's
        # WHERE clause legitimately matches 0 rows and would raise again,
        # producing a false "positions row NOT updated" CRITICAL log for a
        # trade whose exit actually DID persist correctly. Only re-attempt
        # whichever step hasn't succeeded yet.
        position_persisted = False
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                if not position_persisted:
                    await self._db.update_position_exit(
                        order_id=closed.order_id, exit_price=closed.exit_index_price,
                        exit_time=closed.exit_time, pnl=closed.pnl,
                        exit_premium=closed.exit_premium,
                        # 2026-09-24, found in review — see models.py's
                        # BNTrade.exit_reason comment. Every exit path (tick
                        # exit, EOD, manual) routes through this one shared
                        # helper, so a single call site covers all of them.
                        outcome=closed.exit_reason,
                    )
                    position_persisted = True
                # NOT self._persist_funds() — that helper swallows its own
                # exceptions (print-and-continue, for its other fire-and-
                # forget callers elsewhere in this file) so a funds-persist
                # failure here would never actually reach this loop's
                # except clause below, silently skipping the retry this
                # whole method exists to provide (found in review,
                # 2026-09-23) — call the DB write directly so a failure
                # genuinely propagates and gets retried/logged like the
                # position-exit write above.
                await self._db.set_app_settings({BN_FUNDS_KEY: get_state().funds})
                return
            except Exception as e:
                last_exc = e
        if position_persisted:
            print(f"CRITICAL: {label} exit funds persist failed after retries for "
                  f"order_id={closed.order_id} — positions row WAS updated, but "
                  f"funds NOT persisted for this exit: {last_exc}")
        else:
            print(f"CRITICAL: {label} exit DB persist failed after retries for "
                  f"order_id={closed.order_id} — positions row NOT updated, "
                  f"funds NOT persisted for this exit: {last_exc}")

    async def _persist_new_position(self, trade, instrument: str, label: str) -> bool:
        """
        Persists a freshly-opened trade's entry row, with the same short
        bounded retry as _persist_closed_exit (found in review, 2026-09-23:
        this entry-side save had no retry at all, just a bare try/except-
        and-print, even though it can fail for the exact same transient-DB
        reasons the exit path was hardened against). Without a retry, a
        single transient DB hiccup here silently drops the entry row
        entirely — the trade still runs correctly in memory (st.active_trade
        is already set by place_paper_order before this call), but roughly
        BN_SCALP_TIME_STOP_S/NF_SCALP_TIME_STOP_S later, when the exit fires,
        update_position_exit's WHERE order_id=... AND status='OPEN' matches
        zero rows and raises — surfacing as a confusing "positions row NOT
        updated" CRITICAL log instead of a clear "entry save failed" one,
        for what was really an entry-side failure all along.

        Returns True/False (2026-09-24, found in review) — the algo tick-loop
        callers still just fire-and-forget this (a human isn't waiting on an
        HTTP response there), but app/api/dashboard.py's manual-order
        endpoints DO have a human waiting for a response and need to know
        whether persistence actually succeeded, to decide what to tell them
        (and, more importantly, whether to roll back st.active_trade/
        active_trade_nf on total failure — see dashboard.py's manual_order).
        """
        delays = (0.0, 0.2, 0.6)
        last_exc: Optional[Exception] = None
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._db.save_position(trade, instrument=instrument)
                return True
            except Exception as e:
                last_exc = e
        print(f"CRITICAL: {label} entry DB persist failed after retries for "
              f"order_id={trade.order_id} — positions row NOT written for this "
              f"entry: {last_exc}")
        return False

    # ── Phase driver ──────────────────────────────────────────────────────────

    async def _phase_driver(self) -> None:
        st = get_state()
        while True:
            try:
                now = _now()
                if now.weekday() >= 5:
                    await asyncio.sleep(3600)
                    continue

                h, m  = now.hour, now.minute
                today = now.strftime("%Y-%m-%d")

                if h < cfg.PREMARKET_HOUR or (h == cfg.PREMARKET_HOUR and m < cfg.PREMARKET_MIN):
                    st.phase = TradingPhase.PRE_MARKET
                    await _sleep_toward(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN)

                elif h < cfg.MARKET_OPEN_HOUR or (h == cfg.MARKET_OPEN_HOUR and m < cfg.MARKET_OPEN_MIN):
                    if self._premarket_date != today:
                        st.phase = TradingPhase.PRE_MARKET
                        self._premarket_date = today
                    await _sleep_toward(cfg.MARKET_OPEN_HOUR, cfg.MARKET_OPEN_MIN)

                elif h < cfg.SESSION_END_HOUR or (h == cfg.SESSION_END_HOUR and m < cfg.SESSION_END_MIN):
                    if _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN):
                        st.phase = TradingPhase.CUTOFF
                    elif _past(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN):
                        st.phase = TradingPhase.ACTIVE
                    else:
                        st.phase = TradingPhase.WAIT_ZONE

                    # Mid-session restart: rebuild today's trade/PnL state from
                    # the DB BEFORE the WS starts. Must check BOTH instruments'
                    # empty-state, not just BN's (found in review, 2026-09-25):
                    # _restore_from_db() itself restores BN+NF together and has
                    # no dedup against rows already reflected in memory, so a
                    # BN-only guard re-fires and double-appends NF's already-
                    # live closed trades into st.closed_trades_nf on any day BN
                    # happens to have zero trades (a realistic outcome given
                    # the strategy's narrow windows/cooldown/daily cap) — this
                    # silently doubled NF's daily_pnl/trade count and, via
                    # _run_eod's own identical bug below, corrupted the
                    # persisted daily_stats row for NIFTY50.
                    if (not st.closed_trades and not st.closed_trades_nf
                            and st.active_trade is None and st.active_trade_nf is None):
                        await self._restore_from_db()

                    if not self._mkt._running:
                        st.api_status = "Recovery: loading historical data…"
                        await self._run_wait_zone()

                    # Restore phase (the loads above can take a while).
                    if _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN):
                        st.phase = TradingPhase.CUTOFF
                    elif _past(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN):
                        st.phase = TradingPhase.ACTIVE
                    else:
                        st.phase = TradingPhase.WAIT_ZONE

                    await self._run_active_phase()

                else:
                    st.phase = TradingPhase.CLOSED
                    if self._eod_date != today:
                        await self._run_eod()
                        self._eod_date = today
                    await _sleep_toward(cfg.PREMARKET_HOUR, cfg.PREMARKET_MIN)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Phase driver error: {e}")
                await asyncio.sleep(5)

    # ── Phase handlers ────────────────────────────────────────────────────────

    async def _run_wait_zone(self) -> None:
        st = get_state()
        st.phase = TradingPhase.WAIT_ZONE
        print("=== WAIT ZONE: Loading historical data ===")
        await self._load_all_historical()
        if self._mkt._running:
            await self._mkt.stop()
        self._mkt.start()

    async def _run_active_phase(self) -> None:
        """
        Tick-DRIVEN engine (2026-09-24, explicit user decision — replaces
        the old fixed 100ms poll with "react to every live WebSocket tick
        directly"). Each iteration below runs the instant
        MarketDataService._process_tick sets st.tick_event for a real 5m
        tick (see there), not on a timer — so a real price move is seen
        within one event-loop turn instead of up to TICK_EVAL_INTERVAL_MS
        (100ms) later.

        TICK_EVAL_INTERVAL_MS is still very much load-bearing as a BOUNDED
        FALLBACK, not a leftover: several things this loop drives are
        wall-clock conditions that must be re-checked even when the feed
        goes quiet and no new tick ever arrives — the 12s TIME_SCRATCH exit
        timer, the pending-entry/pending-exit FILL_MAX_WAIT_MS fallback, the
        cooldown window, and even TradingPhase transitioning on schedule. A
        pure event-wait with no timeout would let all of those silently
        stall during a feed outage. asyncio.wait_for(...) below waits for
        EITHER the event OR the timeout, whichever comes first, so both
        properties hold simultaneously: near-zero latency on a real tick,
        and the exact same worst-case cadence as before if ticks stop.

        Every iteration still runs the full pass (exits, entries, alerts,
        ATM watch) for both instruments — a tick from ANY tracked symbol
        wakes it, not just BN/NF index ticks, since basket-score entries
        depend on 8 other stocks each and there's no cheap way to know in
        advance which single tick might flip a gate. The extra evaluations
        this causes for an unrelated symbol's tick are cheap (no I/O, pure
        in-memory math over ~15 instruments) — see the module's own history
        of running this at 100ms already being "cheap enough."
        """
        print("=== ACTIVE: tick-driven engine open ===")
        st = get_state()
        fallback_s = max(0.01, cfg.TICK_EVAL_INTERVAL_MS / 1000.0)

        while not _past(cfg.SESSION_END_HOUR, cfg.SESSION_END_MIN):
            try:
                if _past(cfg.CUTOFF_HOUR, cfg.CUTOFF_MIN):
                    st.phase = TradingPhase.CUTOFF
                elif _past(cfg.SCAN_START_HOUR, cfg.SCAN_START_MIN):
                    st.phase = TradingPhase.ACTIVE
                else:
                    st.phase = TradingPhase.WAIT_ZONE

                await self._tick_exits()
                await self._tick_entries()
                await self._tick_exits_nf()
                await self._tick_entries_nf()
                await self._tick_alerts()
                await self._tick_atm_watch()
            except Exception as e:
                print(f"Tick loop error: {e}")

            try:
                await asyncio.wait_for(st.tick_event.wait(), timeout=fallback_s)
            except asyncio.TimeoutError:
                pass
            st.tick_event.clear()

    async def _tick_exits(self) -> None:
        st = get_state()
        if st.active_trade is None:
            return
        with st._bn_index_lock:
            bn_candles = list(st.bn_index_candles_5m)
        if not bn_candles:
            return
        # bn_index_ltp can legitimately be 0 here for a restart-recovered
        # trade: _restore_from_db can repopulate st.active_trade before
        # _run_wait_zone's historical load + the first live/synthetic tick
        # ever arrive, and bn_index_ltp starts at 0.0 in a fresh process
        # (state.py). Falling back to the last known candle close (bn_candles
        # is already loaded by the time this runs) mirrors the EOD
        # force-close fallback fixed 2026-09-23 for the identical ltp==0
        # hazard (found in review, 2026-09-24) — without it, a restored open
        # trade got ZERO target/stop/time-scratch checking for the entire
        # reload window, easily longer than the strategy's own ~12s
        # time-stop, defeating the whole ₹2-3 bracket it depends on.
        price = st.bn_index_ltp if st.bn_index_ltp > 0 else bn_candles[-1].close
        # closed_tail_closes() excludes the still-forming last bar before
        # feeding estimate_iv — see its own docstring for why (this was the
        # actual root cause of a real production bug on the exit side, where
        # the tight ~₹2-3 target/stop bracket got blown through by pure
        # IV-estimate inconsistency, not real price movement).
        lookback = iv_lookback_closes(bn_candles, cfg.BN_IV_LOOKBACK_BARS)
        try:
            closed = bn_trade.check_tick_exit(_now(), price, lookback)
            if closed:
                await self._persist_closed_exit(closed, "BN")
        except Exception as e:
            print(f"Tick exit error: {e}")

    async def _tick_entries(self) -> None:
        st = get_state()
        if st.active_trade is not None:
            return

        with st._bn_index_lock:
            bn_candles = list(st.bn_index_candles_5m)
        if not bn_candles or st.bn_index_ltp <= 0:
            return

        # 2026-09-22, explicit user decision: the scalp entry is evaluated
        # EVERY TICK (~100ms), not once per closed 5m bar — this method is
        # already called every tick by _run_active_phase's loop, so the only
        # change is no longer gating on "a new bar just closed" (the old
        # last_evaluated_bar dedup is gone). current_index_price/basket_ltp
        # feed evaluate_entry the LIVE tick price so the score reacts
        # immediately instead of waiting up to 5 minutes for the next bar
        # close; the candle history is still used for VWAP (which only
        # meaningfully updates as bars complete) and the IV estimate. Once a
        # trade opens, the `st.active_trade is not None` guard above blocks
        # every subsequent tick until it closes + cooldown — no risk of
        # firing twice off one live signal.
        # closed_tail_closes() excludes the still-forming last bar (see its
        # own docstring) even though the live tick price is deliberately
        # used elsewhere in this function (current_index_price/basket_ltp
        # below).
        bn_closes_lookback = closed_tail_closes(bn_candles, cfg.BN_INDICATOR_LOOKBACK_BARS)

        # An armed signal is waiting out its simulated entry-fill lag
        # (2026-09-24, explicit user decision: "300ms Entry Delay ...
        # Realistic Slippage") — no new evaluate_entry call while one is
        # pending, same as the active_trade-is-not-None guard above; see
        # bn_trade.try_fill_pending_entry for the actual fill condition
        # (delay elapsed + a genuinely new live tick, or the bounded
        # max-wait fallback). Resolved regardless of phase — an order armed
        # a moment before the 15:00 cutoff must still fill/abandon rather
        # than get stuck forever the instant phase flips to CUTOFF (the
        # phase gate below only blocks arming a NEW signal, not resolving
        # one already in flight).
        if st.pending_entry is not None:
            trade = bn_trade.try_fill_pending_entry(_now(), st.bn_index_ltp, bn_closes_lookback)
            if trade is not None:
                await self._persist_new_position(trade, "BANKNIFTY", "BN")
            return

        if st.phase != TradingPhase.ACTIVE:
            return

        bn_recent = bn_candles[-5:]

        # Top-8 weighted-basket scalp strategy (2026-09-21) — only these 8
        # tokens (cfg.BN_SCALP_BASKET), not the full 14-stock BN_ALL_STOCKS
        # universe the old leader-vote rule needed. Untrimmed history —
        # session VWAP needs every bar from today's open.
        name_by_token = cfg.BN_NAME_BY_TOKEN
        basket_candles = {}
        basket_ltp = {}
        for token in cfg.BN_SCALP_BASKET:
            with st.candle_lock(token):
                basket_candles[token] = list(st.candles_5m.get(token, []))
            basket_ltp[token] = st.ltp.get(name_by_token.get(token, token), 0.0)

        last_exit_time = (datetime.fromisoformat(st.last_exit_time)
                          if st.last_exit_time else None)
        now = _now()
        signal, diagnostic = evaluate_entry(now, bn_recent, bn_closes_lookback,
                                            basket_candles, last_exit_time, st.trades_today_combined,
                                            current_index_price=st.bn_index_ltp, basket_ltp=basket_ltp)
        # trades_today_combined (above) is correctly what the GATE inside
        # evaluate_entry checks against SCALP_MAX_TRADES_PER_DAY (the
        # 2026-09-24 shared-cap fix) — but the DIAGNOSTIC's own trades_today
        # field is meant to be a per-instrument DISPLAY value (see
        # state.py's bn_trades_today comment), so overwrite it back to the
        # true per-instrument count here (found in review, 2026-09-24: this
        # was left holding the combined value, meaning BN's and NF's
        # diagnostic panels would both silently show the identical combined
        # number instead of each instrument's own — currently low-impact
        # since dashboard.js doesn't render this field yet, but the data
        # contract itself was wrong).
        diagnostic.trades_today = st.bn_trades_today
        st.bn_diagnostic = diagnostic

        if signal is None:
            return

        # Arm the fill-delay pending entry instead of placing it instantly —
        # see bn_trade.arm_pending_entry/try_fill_pending_entry above.
        bn_trade.arm_pending_entry(signal, now)

    # ── Nifty 50 — mirrors _tick_exits/_tick_entries above ───────────────────

    async def _tick_exits_nf(self) -> None:
        st = get_state()
        if st.active_trade_nf is None:
            return
        with st._nf_index_lock:
            nf_candles = list(st.nf_index_candles_5m)
        if not nf_candles:
            return
        # See the BN mirror's identical fallback comment above (found in
        # review, 2026-09-24) — a restart-recovered NF trade gets the same
        # ltp==0 hazard for the exact same reason.
        price = st.nf_index_ltp if st.nf_index_ltp > 0 else nf_candles[-1].close
        # See the BN mirror's comment above — same forming-bar exclusion via closed_tail_closes().
        lookback = iv_lookback_closes(nf_candles, cfg.NF_IV_LOOKBACK_BARS)
        try:
            closed = nf_trade.check_tick_exit(_now(), price, lookback)
            if closed:
                await self._persist_closed_exit(closed, "NF")
        except Exception as e:
            print(f"NF tick exit error: {e}")

    async def _tick_entries_nf(self) -> None:
        st = get_state()
        if st.active_trade_nf is not None:
            return

        with st._nf_index_lock:
            nf_candles = list(st.nf_index_candles_5m)
        if not nf_candles or st.nf_index_ltp <= 0:
            return

        # See the BN mirror's comment in _tick_entries above —
        # closed_tail_closes() excludes the still-forming last bar.
        nf_closes_lookback = closed_tail_closes(nf_candles, cfg.NF_INDICATOR_LOOKBACK_BARS)

        # See the BN mirror's identical comment in _tick_entries above
        # (2026-09-24) — no new nf_evaluate_entry call while a signal is
        # waiting out its simulated entry-fill lag; resolved regardless of
        # phase (an order armed just before cutoff must still fill/abandon).
        if st.pending_entry_nf is not None:
            trade = nf_trade.try_fill_pending_entry(_now(), st.nf_index_ltp, nf_closes_lookback)
            if trade is not None:
                await self._persist_new_position(trade, "NIFTY50", "NF")
            return

        if st.phase != TradingPhase.ACTIVE:
            return

        # Evaluated every tick, not once per closed bar — see the BN
        # mirror's comment in _tick_entries above.
        nf_recent = nf_candles[-5:]

        # 2026-09-21: Top-8 weighted-basket tokens (cfg.NF_SCALP_BASKET),
        # not the 12-stock NF_LEADER_STOCKS the old rule used.
        name_by_token = cfg.NF_NAME_BY_TOKEN
        basket_candles = {}
        basket_ltp = {}
        for token in cfg.NF_SCALP_BASKET:
            with st.candle_lock(token):
                basket_candles[token] = list(st.candles_5m.get(token, []))
            basket_ltp[token] = st.ltp.get(name_by_token.get(token, token), 0.0)

        last_exit_time = (datetime.fromisoformat(st.last_exit_time_nf)
                          if st.last_exit_time_nf else None)
        now = _now()
        signal, diagnostic = nf_evaluate_entry(now, nf_recent, nf_closes_lookback,
                                               basket_candles, last_exit_time, st.trades_today_combined,
                                               current_index_price=st.nf_index_ltp, basket_ltp=basket_ltp)
        # See the BN mirror's identical comment above (2026-09-24, found in review).
        diagnostic.trades_today = st.nf_trades_today
        st.nf_diagnostic = diagnostic

        if signal is None:
            return

        # Arm the fill-delay pending entry instead of placing it instantly —
        # see nf_trade.arm_pending_entry/try_fill_pending_entry above.
        nf_trade.arm_pending_entry(signal, now)

    async def _tick_alerts(self) -> None:
        """
        Server-side leader-consensus price-move alert check (see
        price_alerts.py) — runs every tick, independent of whether/how often
        a dashboard browser tab is open. Purely informational, same as the
        client-side version it replaces; never touches evaluate_entry/exit.
        """
        st = get_state()
        has_clients = self._ws.count() > 0
        # Reconnect edge-reset (found in review, 2026-09-23): the
        # has_clients commit-gate above (2026-09-23) fixed "detected once
        # while offline, never toggled" — a condition still true when a
        # client connects now correctly fires. It does NOT fix a condition
        # that went true->false->true entirely while offline: _was_consensus
        # stays frozen at whatever it was last committed (True), so the
        # first post-reconnect tick sees met=True/was=True and no edge
        # fires, even though a fresh down-up cycle happened unseen. On a
        # 0->positive client-count transition, clear all committed state so
        # the very next check is guaranteed to see a fresh edge if the
        # condition is currently active — only committed state is reset, not
        # detection (which already runs every tick regardless of clients).
        # Tradeoff: a condition that's been continuously true through a
        # brief disconnect/reconnect blip re-fires once, spuriously — judged
        # acceptable since this alert is purely informational (never feeds
        # evaluate_entry/evaluate_exit) and over-notifying beats the
        # previous silent-miss failure mode.
        if has_clients and not self._had_alert_clients:
            price_alerts.reset_consensus_state()
        self._had_alert_clients = has_clients
        try:
            fired = price_alerts.check_consensus(
                st, "BankNifty", cfg.BN_LEADER_STOCKS, cfg.BN_PRICE_ALERT_ATTR,
                cfg.BN_ALERT_CONSENSUS_REQUIRED, has_clients=has_clients)
            fired += price_alerts.check_consensus(
                st, "Nifty 50", cfg.NF_LEADER_STOCKS, cfg.NF_PRICE_ALERT_ATTR,
                cfg.NF_ALERT_CONSENSUS_REQUIRED, has_clients=has_clients)
        except Exception as e:
            print(f"Alert check error: {e}")
            return
        if fired and has_clients:
            for alert in fired:
                await self._ws.broadcast(json.dumps({"type": "ALERT", **alert}, default=str))

    # Minimum real time between ATM-watch WS resubscriptions (2026-09-24,
    # found in review) — see _tick_atm_watch's docstring. Reuses
    # TICK_EVAL_INTERVAL_MS as the reference rate: that's the exact ceiling
    # the old fixed-timer loop always imposed on this before the engine
    # became tick-driven, so capping at the same rate is a genuine
    # behavior-preserving fix, not an arbitrary new number.
    @staticmethod
    def _atm_resub_ok(now: datetime, last_resub_at: datetime | None) -> bool:
        if last_resub_at is None:
            return True
        return (now - last_resub_at).total_seconds() * 1000.0 >= cfg.TICK_EVAL_INTERVAL_MS

    async def _tick_atm_watch(self) -> None:
        """
        Live ATM CE/PE watchlist (2026-09-18, explicit user decision) — keeps
        a continuous real-LTP subscription for whatever the CURRENT at-the-
        money strike is, separate from bn_trade.py/nf_trade.py's per-active-
        trade option subscription (that one is FROZEN at entry; this one
        tracks the live, moving spot). Recomputing the ATM strike + building
        the option symbols is cheap (no Black-Scholes — just
        round(spot/100)*100 plus a couple of pure string-format calls), so
        this runs every pass of the tick-driven engine; the WS subscription
        is only touched when the built (ce, pe) SYMBOL PAIR actually changes
        (see market_data.py's set_bn_atm_watch/set_nf_atm_watch), since a
        reconnect is what changing it costs — AND at most once per
        TICK_EVAL_INTERVAL_MS (2026-09-24, found in review): the engine
        became tick-driven that same day, so this can now be called far more
        often than the old fixed-100ms timer ever allowed. Without this
        floor, spot sitting exactly at a strike boundary during a burst of
        real ticks could reconnect the option WS connection many times a
        second. A skipped change here is NOT lost — self._bn_atm_watch_
        symbols is only updated once the resubscribe actually happens, so
        the very next call still sees the mismatch and retries once the
        debounce window passes; a real, sustained move still gets picked up
        promptly, only pure oscillation-driven churn is capped.

        Keyed on the SYMBOL PAIR, not the strike alone (2026-09-25, found in
        review) — nf_pricing.build_weekly_option_symbol's format can change
        (weekly numeric-date vs. monthly-style, see its own docstring) at
        the Tuesday-15:30-IST expiry rollover WITHOUT the numeric ATM strike
        necessarily changing at all. Keying only on strike would then leave
        a stale, wrong-format symbol subscribed indefinitely with nothing to
        trigger a resubscribe. This can't currently manifest — _run_eod
        resets this same tracking to None every day at exactly 15:30 IST,
        the only moment the format can flip, forcing a fresh recompute on
        the next trading day's first tick regardless — but relying on that
        coincidence was fragile against any future change to the EOD-reset
        timing; comparing the actual symbols is correct on its own terms.
        """
        st = get_state()
        now = _now()
        if st.bn_index_ltp > 0:
            strike = get_atm_strike(st.bn_index_ltp)
            expiry = bn_get_next_expiry(now)
            ce = build_monthly_option_symbol(cfg.BN_OPTION_UNDERLYING, expiry, strike, "CE")
            pe = build_monthly_option_symbol(cfg.BN_OPTION_UNDERLYING, expiry, strike, "PE")
            if (ce, pe) != self._bn_atm_watch_symbols and self._atm_resub_ok(
                    now, self._bn_atm_watch_last_resub_at):
                self._bn_atm_watch_symbols = (ce, pe)
                self._bn_atm_watch_last_resub_at = now
                self._mkt.set_bn_atm_watch(ce, pe, strike=strike)
        if st.nf_index_ltp > 0:
            strike = nf_get_atm_strike(st.nf_index_ltp)
            expiry = nf_get_next_expiry(now)
            ce = build_weekly_option_symbol(cfg.NF_OPTION_UNDERLYING, expiry, strike, "CE")
            pe = build_weekly_option_symbol(cfg.NF_OPTION_UNDERLYING, expiry, strike, "PE")
            if (ce, pe) != self._nf_atm_watch_symbols and self._atm_resub_ok(
                    now, self._nf_atm_watch_last_resub_at):
                self._nf_atm_watch_symbols = (ce, pe)
                self._nf_atm_watch_last_resub_at = now
                self._mkt.set_nf_atm_watch(ce, pe, strike=strike)

    async def _restore_from_db(self) -> None:
        """
        Restart recovery: rebuild today's trade/P&L state from the DB, so the
        60s cooldown and daily stats survive a crash.
        """
        st = get_state()
        try:
            rows = await self._db.get_today_positions()
        except Exception as e:
            print(f"Recovery: could not reload today's positions: {e}")
            return
        if not rows:
            return

        def _f(v) -> float:
            return float(v) if v is not None else 0.0

        def _apply_row(r) -> None:
            status = (PositionStatus(r["status"])
                      if r.get("status") in ("OPEN", "CLOSED") else PositionStatus.OPEN)
            is_nf = r.get("instrument") == "NIFTY50"
            cls = NFTrade if is_nf else BNTrade
            # DB never persisted target_rs/stop_rs/time_stop_s (no columns
            # for them — the scalp lifecycle is new, 2026-09-21). A restored
            # OPEN trade backfills them from the CURRENT cfg defaults as a
            # best-effort recovery value (same "restart loses the exact
            # frozen-at-entry number" caveat this repo's own restart-
            # recovery already had for breakeven/trail before this rewrite)
            # — critically, NOT left at the dataclass default of 0.0, which
            # would make time_stop_s=0 force an immediate TIME_SCRATCH exit
            # on the very next tick after recovery.
            time_stop_s = cfg.NF_SCALP_TIME_STOP_S if is_nf else cfg.BN_SCALP_TIME_STOP_S
            target_rs   = cfg.NF_SCALP_TARGET_RS   if is_nf else cfg.BN_SCALP_TARGET_RS
            stop_rs     = cfg.NF_SCALP_STOP_RS     if is_nf else cfg.BN_SCALP_STOP_RS
            # scratch_slippage_rs (2026-09-23) belongs in this same backfill —
            # was missing (found in review), silently defaulting to the
            # dataclass 0.0 and reintroducing the exact bug this field was
            # added to prevent (evaluate_exit reading a live cfg value
            # instead of a frozen one) for any restart-recovered OPEN trade.
            scratch_slippage_rs = (cfg.NF_SCALP_SCRATCH_SLIPPAGE_RS if is_nf
                                   else cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS)
            trade = cls(
                direction=str(r.get("direction") or "BUY"),
                entry_index_price=_f(r.get("entry_price")),
                entry_time=str(r.get("entry_time") or ""),
                target=_f(r.get("target")),
                current_sl=_f(r.get("stop_loss")),
                strike=int(r.get("strike") or 0),
                option_type=str(r.get("option_type") or "CE"),
                expiry=str(r.get("expiry") or ""),
                entry_premium=_f(r.get("entry_premium")),
                target_rs=target_rs, stop_rs=stop_rs, time_stop_s=time_stop_s,
                scratch_slippage_rs=scratch_slippage_rs,
                lot_size=int(r.get("quantity") or (cfg.NF_LOT_SIZE if is_nf else cfg.BN_LOT_SIZE)),
                order_id=str(r.get("order_id") or ""),
                status=status,
                exit_index_price=float(r["exit_price"]) if r.get("exit_price") is not None else None,
                exit_time=r.get("exit_time"),
                exit_premium=float(r["exit_premium"]) if r.get("exit_premium") is not None else None,
                pnl=_f(r.get("pnl")),
                # 2026-09-24, found in review — a restart-recovered closed
                # trade needs this backfilled from the DB same as every
                # other field above, or "Today's Trades" would show a blank
                # outcome for anything closed before the last restart.
                exit_reason=r.get("outcome"),
            )
            if status == PositionStatus.CLOSED:
                st.daily_pnl += trade.pnl   # shared account — every closed trade nets into the one daily_pnl
                if is_nf:
                    st.closed_trades_nf.append(trade)
                    st.nf_trades_today += 1   # counts toward SCALP_MAX_TRADES_PER_DAY across a restart too
                    if trade.exit_time:
                        st.last_exit_time_nf = trade.exit_time
                else:
                    st.closed_trades.append(trade)
                    st.bn_trades_today += 1
                    if trade.exit_time:
                        st.last_exit_time = trade.exit_time
            else:
                if is_nf:
                    st.active_trade_nf = trade
                    st.nf_trades_today += 1
                else:
                    st.active_trade = trade
                    st.bn_trades_today += 1

        # Per-row isolation (found in review, 2026-09-25): a single malformed
        # row used to raise straight out of this whole method, leaving a
        # PARTIAL restore (e.g. BN rows already applied, NF rows never
        # reached) — and because the re-entry guard at both call sites checks
        # st.closed_trades/st.closed_trades_nf non-empty to decide "already
        # restored," a partial restore permanently blocks any further
        # restore attempt for the rest of that session, silently leaving
        # whichever instrument's rows came later in `rows` unrestored. One
        # bad row (malformed strike/quantity/exit_price, the only fields not
        # already defensively coerced above) now only skips ITSELF.
        for r in rows:
            try:
                _apply_row(r)
            except Exception as e:
                print(f"Recovery: skipping malformed position row "
                      f"order_id={r.get('order_id')!r}: {e}")

        print(
            f"=== RECOVERY: restored BN {'1 open' if st.active_trade else '0 open'}/"
            f"{len(st.closed_trades)} closed, NF {'1 open' if st.active_trade_nf else '0 open'}/"
            f"{len(st.closed_trades_nf)} closed | daily P&L ₹{st.daily_pnl:+.2f} ==="
        )

    async def _run_eod(self) -> None:
        st = get_state()

        # Same combined-instrument guard as the mid-session-restart call site
        # above — see its comment for why a BN-only check double-counts NF's
        # (or vice versa) already-in-memory trades on re-entry.
        if (not st.closed_trades and not st.closed_trades_nf
                and st.active_trade is None and st.active_trade_nf is None):
            await self._restore_from_db()

        if st.active_trade is not None:
            with st._bn_index_lock:
                bn_candles = list(st.bn_index_candles_5m)
            # bn_index_ltp can still be 0 here (e.g. a restart/recovery right
            # before 15:30 with no live tick yet) — fall back to the last
            # known candle close, then the trade's own entry price, so the
            # trade is ALWAYS actually settled (finalize_exit + DB row +
            # daily_pnl) instead of just vanishing from st.active_trade below
            # with nothing ever recorded (a real money/data-loss bug fixed
            # 2026-09-23: force-closing used to be skipped entirely whenever
            # ltp was 0, yet active_trade was still unconditionally cleared).
            fallback_price = st.bn_index_ltp if st.bn_index_ltp > 0 else (
                bn_candles[-1].close if bn_candles else st.active_trade.entry_index_price)
            lookback = iv_lookback_closes(bn_candles, cfg.BN_IV_LOOKBACK_BARS)
            closed = bn_trade.force_close(_now(), fallback_price, lookback)
            if closed:
                # Reuses the same bounded-retry persistence as _tick_exits
                # (found in review, 2026-09-23) — force_close above already
                # committed this exit in memory, same as check_tick_exit, so
                # a bare try/except-and-print here would have the identical
                # silent-data-loss failure mode on a transient DB hiccup.
                await self._persist_closed_exit(closed, "BN EOD square-off")

        if st.active_trade_nf is not None:
            with st._nf_index_lock:
                nf_candles = list(st.nf_index_candles_5m)
            fallback_price = st.nf_index_ltp if st.nf_index_ltp > 0 else (
                nf_candles[-1].close if nf_candles else st.active_trade_nf.entry_index_price)
            lookback = iv_lookback_closes(nf_candles, cfg.NF_IV_LOOKBACK_BARS)
            closed = nf_trade.force_close(_now(), fallback_price, lookback)
            if closed:
                await self._persist_closed_exit(closed, "NF EOD square-off")

        # Stop the live ATM CE/PE watchlist for the day — no point holding a
        # WS subscription for stale strikes once the market's closed; it'll
        # naturally resume tomorrow on the first live tick that moves it.
        self._mkt.set_bn_atm_watch(None, None)
        self._mkt.set_nf_atm_watch(None, None)
        self._bn_atm_watch_symbols = None
        self._nf_atm_watch_symbols = None
        self._bn_atm_watch_last_resub_at = None
        self._nf_atm_watch_last_resub_at = None

        # Reset the leader-consensus alert's edge-trigger state at day
        # boundary too (found in review, 2026-09-24) — previously only reset
        # on a 0->positive dashboard-client reconnect (_tick_alerts), same as
        # every other per-day counter/flag here. Without this, a condition
        # still true at 15:30 close stays "already seen" into the next day,
        # so an immediate gap-open repeat of the same condition at the next
        # session's open would silently fail to fire — the same edge-case
        # class as the reconnect bug already fixed for _was_consensus, just
        # at the day boundary instead of the client-connection boundary.
        price_alerts.reset_consensus_state()

        await self._mkt.stop()
        await self._persist_funds()

        # Grow our own BankNifty/Nifty 50 history archives (see
        # save_bn_index_bars/save_nf_index_bars) — the external server never
        # gives us more than "today" for either index, so this is the only
        # way multi-day backtesting becomes possible over time.
        with st._bn_index_lock:
            bn_snapshot = list(st.bn_index_candles_5m)
        if bn_snapshot:
            try:
                await self._db.save_bn_index_bars(bn_snapshot)
            except Exception as e:
                print(f"BN index history save error: {e}")

        with st._nf_index_lock:
            nf_snapshot = list(st.nf_index_candles_5m)
        if nf_snapshot:
            try:
                await self._db.save_nf_index_bars(nf_snapshot)
            except Exception as e:
                print(f"NF index history save error: {e}")

        trades  = st.closed_trades
        total   = len(trades)
        winners = sum(1 for t in trades if t.pnl > 0)

        peak = cum = max_dd = 0.0
        for t in sorted(trades, key=lambda x: (x.exit_time or "")):
            cum += t.pnl
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        nf_trades  = st.closed_trades_nf
        nf_total   = len(nf_trades)
        nf_winners = sum(1 for t in nf_trades if t.pnl > 0)

        nf_peak = nf_cum = nf_max_dd = 0.0
        for t in sorted(nf_trades, key=lambda x: (x.exit_time or "")):
            nf_cum += t.pnl
            nf_peak = max(nf_peak, nf_cum)
            nf_max_dd = max(nf_max_dd, nf_peak - nf_cum)

        # daily_pnl is the SHARED account total (BN + NF combined) — split
        # each instrument's own total_pnl for its daily_stats row from its
        # own closed_trades list, not from the shared daily_pnl figure.
        bn_pnl = sum(t.pnl for t in trades)
        nf_pnl = sum(t.pnl for t in nf_trades)

        if total > 0:
            try:
                await self._db.upsert_daily_stats(
                    total_trades=total, winning_trades=winners,
                    total_pnl=bn_pnl, gemini_shortlist=None,
                    max_drawdown=round(max_dd, 2), instrument="BANKNIFTY",
                )
            except Exception as e:
                print(f"EOD stats error: {e}")
        if nf_total > 0:
            try:
                await self._db.upsert_daily_stats(
                    total_trades=nf_total, winning_trades=nf_winners,
                    total_pnl=nf_pnl, gemini_shortlist=None,
                    max_drawdown=round(nf_max_dd, 2), instrument="NIFTY50",
                )
            except Exception as e:
                print(f"NF EOD stats error: {e}")
        if total == 0 and nf_total == 0:
            print("=== EOD: no session state in this process — daily_stats write skipped ===")

        print(f"=== EOD: BN {total} trades ({winners} winners) | NF {nf_total} trades "
              f"({nf_winners} winners) | Shared daily P&L ₹{st.daily_pnl:+.2f} ===")

        st.active_trade = None
        st.closed_trades.clear()
        st.last_exit_time = None
        st.bn_diagnostic = None
        st.bn_trades_today = 0
        st.active_trade_nf = None
        st.closed_trades_nf.clear()
        st.last_exit_time_nf = None
        st.nf_diagnostic = None
        st.nf_trades_today = 0
        st.daily_pnl = 0.0
        # Locked (2026-09-24, found in review) — same race as market_data.py's
        # st.ltp[name] = ltp write and dashboard.py's get_prices() read: a
        # size-changing mutation here could land mid-iteration of that sync
        # `def` handler's dict.update(st.ltp) call, running in a real
        # executor thread. See state.py's _ltp_lock comment.
        with st._ltp_lock:
            st.ltp.clear()
        # Defensive cleanup (2026-09-24) — a pending entry armed via
        # bn_trade.arm_pending_entry/nf_trade.arm_pending_entry always
        # resolves within BN_FILL_MAX_WAIT_MS/NF_FILL_MAX_WAIT_MS of its
        # delay elapsing (a few seconds at most — see _tick_entries/
        # _tick_entries_nf's cutoff-boundary comment), so this should never
        # actually be non-None here; cleared anyway so a stuck one from some
        # unforeseen edge case can't silently carry into tomorrow's session
        # (bn_index_ltp/nf_index_ltp reset to 0.0 just below would otherwise
        # permanently block its own resolution — see try_fill_pending_entry's
        # `if not bn_candles or st.bn_index_ltp <= 0: return` guard).
        st.pending_entry = None
        st.pending_entry_nf = None
        # bn_index_ltp/nf_index_ltp were NEVER reset here (found in review,
        # 2026-09-24) — unlike st.ltp above, which the individual-stock
        # candles depend on. They're only ever assigned from a real/
        # synthetic tick (market_data.py), never zeroed anywhere else, so
        # they silently held yesterday's last close/synthetic value all the
        # way through CLOSED/PRE_MARKET and into the next day's WAIT_ZONE,
        # until the first real tick of the new day happened to arrive.
        # Every "no live price yet" guard in this codebase (place_paper_
        # order's manual-order check, _tick_entries'/_tick_entries_nf's own
        # gate, _tick_atm_watch's strike computation) tests `> 0`, which a
        # stale-but-positive value from yesterday silently passes — a
        # manual order placed in the seconds before today's first tick (or
        # an algo entry racing a WS reconnect gap) could price off
        # yesterday's close instead of being correctly rejected. Reset to
        # 0.0 so those guards work as intended for the first tick of a
        # fresh day; bn_synthetic_anchor/nf_synthetic_anchor are untouched
        # (deliberately preserved across days — see _seed_synthetic_anchor).
        st.bn_index_ltp = 0.0
        st.nf_index_ltp = 0.0
        # Re-fetch each stock's history right away (2026-09-16, explicit user
        # decision) instead of leaving st.candles_5m empty until the next
        # 09:15 WAIT_ZONE reload — the vendor DOES fully archive multi-day
        # history for individual stocks (unlike either index — see the note
        # below), so the Stock Candles panel can keep showing today's real
        # data through the closed session instead of "N/A" on every row but
        # the index. _load_all_historical REPLACES (not just clears) every
        # token's candle list, so a failed fetch here just leaves today's
        # already-good data in place rather than blanking it — see its own
        # per-block error handling.
        try:
            await self._load_all_historical()
        except Exception as e:
            print(f"EOD stock-history refresh error: {e}")
        # bn_index_candles_5m/nf_index_candles_5m are intentionally NOT
        # cleared here — see _load_all_historical: this market-data server
        # has no historical ARCHIVE for either index (confirmed empirically —
        # every from_date/to_date range returns only the current day's bars,
        # unlike individual stocks, which return full multi-day history). The
        # composite indicator gate needs *_INDICATOR_LOOKBACK_BARS (default
        # 200) bars to converge, so the ONLY way to ever have that much
        # history is to let live WS ticks accumulate across real trading days
        # (capped at MAX_CANDLE_BUFFER=300, ~4 sessions) — clearing nightly
        # would mean the gate never converges, ever.

    # ── Historical data loader ────────────────────────────────────────────────

    async def _load_all_historical(self) -> None:
        """
        Loads 5 days of history for the 14 BN stocks (fully archived on this
        server) and merges TODAY's BankNifty bars into whatever's already
        accumulated in bn_index_candles_5m from prior live sessions — the
        BankNifty history fetch itself only ever returns today (see the note
        in _run_eod), so this is a same-day upsert, never a multi-day load.
        """
        st = get_state()
        try:
            hist = await fetch_indicator_history(cfg.BN_ALL_STOCKS, cfg.INTERVAL_5M, days_back=5)
            for token_key, candles in hist.items():
                # Locked (2026-09-23 fix, found in review): this bulk replace
                # is the only candles_5m write in the repo that ran unlocked
                # — _build_payload's executor thread reads candles_5m under
                # st.candle_lock(token) every 1s and could otherwise observe
                # a torn read mid-reassignment.
                with st.candle_lock(token_key):
                    st.candles_5m[token_key] = _deque(candles, maxlen=cfg.MAX_CANDLE_BUFFER)

            bn_hist = await fetch_indicator_history(
                {cfg.BN_INDEX_NAME: cfg.BN_INDEX_TOKEN}, cfg.INTERVAL_5M, days_back=1)
            bn_today = bn_hist.get(cfg.BN_INDEX_TOKEN, [])
            with st._bn_index_lock:
                # Upsert (not replace) — reuses MarketDataService's own merge
                # logic so there's exactly ONE implementation of "how a
                # BankNifty bar gets folded into bn_index_candles_5m",
                # whether it arrives via this REST catch-up or a live WS tick.
                for c in bn_today:
                    MarketDataService._upsert_list(st.bn_index_candles_5m, c)

            st.api_status = "API OK"
            print(f"Historical load complete: {len(st.candles_5m)} stocks | "
                  f"BankNifty buffer now {len(st.bn_index_candles_5m)} bars")
        except Exception as e:
            st.api_status = f"Load error: {e}"
            print(f"Historical load error: {e}")

        try:
            nf_hist = await fetch_indicator_history(cfg.NF_ALL_STOCKS, cfg.INTERVAL_5M, days_back=5)
            for token_key, candles in nf_hist.items():
                with st.candle_lock(token_key):
                    st.candles_5m[token_key] = _deque(candles, maxlen=cfg.MAX_CANDLE_BUFFER)

            # 1 day back, matching BN_INDEX_NAME's own fetch — an older repo
            # comment claimed the vendor's REST API returns full multi-day
            # history for "NIFTY 50" (unlike BankNifty), but that predates the
            # 2026-07-23 protocol migration and is unverified under the
            # current symbol scheme; a wider days_back here isn't worth the
            # extra vendor load until that's actually confirmed. The
            # self-recorded nf_index_bars archive + synthetic-index fallback
            # cover the gap exactly like they do for BankNifty either way.
            nf_idx_hist = await fetch_indicator_history(
                {cfg.NF_INDEX_NAME: cfg.NF_INDEX_TOKEN}, cfg.INTERVAL_5M, days_back=1)
            nf_idx_bars = nf_idx_hist.get(cfg.NF_INDEX_TOKEN, [])
            with st._nf_index_lock:
                for c in nf_idx_bars:
                    MarketDataService._upsert_list(st.nf_index_candles_5m, c)

            print(f"NF historical load complete: {len(nf_hist)} stocks | "
                  f"Nifty 50 buffer now {len(st.nf_index_candles_5m)} bars")
        except Exception as e:
            print(f"NF historical load error: {e}")

    # ── 15m support/resistance refresh (Stock Candles panel only) ────────────

    async def _refresh_15m_sr_loop(self) -> None:
        """
        Port of c.html's findSupportResistance — computes 5m/15m support &
        resistance for the Stock Candles panel. Unlike c.html (which re-fetches
        BOTH intervals from scratch), this only fetches 15m here: 5m candles
        are already resident in AppState (candles_5m/bn_index_candles_5m),
        computed on the fly in _build_payload. Infrequent by design, matching
        c.html's own occasional (not tick-wise) S/R refresh.
        """
        st = get_state()
        while True:
            try:
                if st.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF):
                    hist = await fetch_indicator_history(cfg.BN_ALL_STOCKS, "15m", days_back=7)
                    bn_hist = await fetch_indicator_history(
                        {cfg.BN_INDEX_NAME: cfg.BN_INDEX_TOKEN}, "15m", days_back=1)
                    hist.update(bn_hist)

                    nf_hist = await fetch_indicator_history(cfg.NF_ALL_STOCKS, "15m", days_back=7)
                    nf_idx_hist = await fetch_indicator_history(
                        {cfg.NF_INDEX_NAME: cfg.NF_INDEX_TOKEN}, "15m", days_back=1)
                    hist.update(nf_hist)
                    hist.update(nf_idx_hist)

                    levels = {
                        token: bn_breakout.detect_support_resistance(candles)
                        for token, candles in hist.items() if candles
                    }
                    st.sr_15m_levels = levels
            except Exception as e:
                print(f"15m S/R refresh error: {e}")
            await asyncio.sleep(_SR_15M_REFRESH_S)

    # ── Dashboard broadcast ───────────────────────────────────────────────────

    async def _push_dashboard_loop(self) -> None:
        """
        Every 1s: snapshot state into the full dashboard payload and
        broadcast it. _build_payload is pure CPU work over lock-protected
        candle snapshots (breakout/S-R scans across ~45 BN+NF tokens) with no
        further AppState mutation, so it's run in a worker thread via
        run_in_executor rather than inline on the event loop — otherwise it
        directly delays the 100ms trading tick loop (_run_active_phase),
        which shares this same event loop. The candle locks it takes are
        real threading.Locks (see state.py), already designed to be safely
        acquired from a non-event-loop thread (that's how the WS ingest
        thread uses them too).

        st.active_trade/active_trade_nf are the one piece of state the tick
        loop mutates FIELD-BY-FIELD in place (current_sl/current_premium/etc,
        every ~100ms in bn_trade.check_tick_exit) rather than by whole-object
        reassignment — reading those fields from a second thread while the
        event loop is mid-mutation would be a genuine torn read that didn't
        exist when everything ran on one thread. copy.copy() them (and
        list-copy closed_trades, which is only ever appended/cleared, never
        field-mutated in place) HERE, synchronously on the event loop, before
        handing off — the copy call itself can't interleave with another
        event-loop coroutine's mutation, so it's a consistent snapshot, and
        the executor thread then only ever touches its own private copies.
        """
        loop = asyncio.get_running_loop()
        st = get_state()
        while True:
            try:
                if self._ws.count() > 0:
                    active_snapshot    = copy.copy(st.active_trade) if st.active_trade is not None else None
                    active_nf_snapshot = copy.copy(st.active_trade_nf) if st.active_trade_nf is not None else None
                    closed_snapshot    = list(st.closed_trades)
                    closed_nf_snapshot = list(st.closed_trades_nf)
                    payload = await loop.run_in_executor(
                        None, self._build_payload,
                        active_snapshot, active_nf_snapshot, closed_snapshot, closed_nf_snapshot,
                    )
                    await self._ws.broadcast(json.dumps(payload, default=str))
            except Exception as e:
                print(f"Dashboard push error: {e}")
            await asyncio.sleep(1)

    def _collect_all_candles(self, st) -> dict:
        """BankNifty index + all 14 BN stocks, keyed by TOKEN — for the Stock
        Candles panel (breakout/S-R/global-signal), unrelated to the BN
        trading strategy's own candle reads elsewhere in this file."""
        out = {}
        with st._bn_index_lock:
            out[cfg.BN_INDEX_TOKEN] = list(st.bn_index_candles_5m)
        for token in cfg.BN_ALL_STOCKS.values():
            with st.candle_lock(token):
                out[token] = list(st.candles_5m.get(token, []))
        return out

    def _collect_all_candles_nf(self, st) -> dict:
        """NF mirror of _collect_all_candles — Nifty 50 index + all 51 NF stocks."""
        out = {}
        with st._nf_index_lock:
            out[cfg.NF_INDEX_TOKEN] = list(st.nf_index_candles_5m)
        for token in cfg.NF_ALL_STOCKS.values():
            with st.candle_lock(token):
                out[token] = list(st.candles_5m.get(token, []))
        return out

    @staticmethod
    def _build_live_leader_rows(st) -> list:
        """
        Live (per-second) OPEN/CLOSE/VOLUME/SURGE snapshot of each leader
        stock's CURRENT (possibly still-forming) bar — cosmetic only, for the
        Entry Loop Monitor's leader table. Deliberately separate from
        st.bn_diagnostic.leader_rows, which stays a frozen record of the data
        evaluate_entry actually last decided on (once per closed bar) — this
        live view must never feed evaluate_entry/evaluate_exit.
        """
        live_recent = {}
        for name, token in cfg.BN_LEADER_STOCKS.items():
            with st.candle_lock(token):
                candles = list(st.candles_5m.get(token, []))
            live_recent[name] = candles[-1:] if candles else []
        surge = _leader_qty_surge(live_recent)
        return [
            {"stock": name, "open": c[0].open if c else None, "close": c[0].close if c else None,
             "volume": c[0].volume if c else None, "surged": surge.get(name, False)}
            for name, c in live_recent.items()
        ]

    @staticmethod
    def _build_live_leader_rows_nf(st) -> list:
        """NF mirror of _build_live_leader_rows — same per-second live snapshot, NF's 12 leaders."""
        live_recent = {}
        for name, token in cfg.NF_LEADER_STOCKS.items():
            with st.candle_lock(token):
                candles = list(st.candles_5m.get(token, []))
            live_recent[name] = candles[-1:] if candles else []
        surge = _nf_leader_qty_surge(live_recent)
        return [
            {"stock": name, "open": c[0].open if c else None, "close": c[0].close if c else None,
             "volume": c[0].volume if c else None, "surged": surge.get(name, False)}
            for name, c in live_recent.items()
        ]

    def _build_payload(self, active_trade, active_trade_nf,
                       closed_trades: list, closed_trades_nf: list) -> dict:
        """
        active_trade/active_trade_nf/closed_trades/closed_trades_nf are
        snapshots taken by the caller (_push_dashboard_loop), NOT live
        AppState reads — this runs in a worker thread (see there) while the
        tick loop keeps mutating st.active_trade's fields in place, so
        reading it live here would be a torn read across threads.
        """
        st = get_state()
        clock = _now().strftime("%H:%M:%S")

        def _trade_dict(t) -> dict:
            return {
                "direction": t.direction, "entryIndexPrice": t.entry_index_price,
                "entryTime": t.entry_time, "target": t.target, "currentSl": t.current_sl,
                "slStage": t.sl_stage, "strike": t.strike, "optionType": t.option_type,
                "expiry": t.expiry, "entryPremium": t.entry_premium,
                "lotSize": t.lot_size, "orderId": t.order_id, "status": t.status.value,
                "exitIndexPrice": t.exit_index_price, "exitTime": t.exit_time,
                "exitPremium": t.exit_premium, "pnl": t.pnl,
                "indexPnlPoints": t.index_pnl_points, "confidence": t.confidence,
                "currentPremium": t.current_premium, "currentIv": t.current_iv,
                "optionSymbol": t.option_symbol,
                "targetRs": t.target_rs, "stopRs": t.stop_rs, "timeStopS": t.time_stop_s,
                "basketScoreAtEntry": t.basket_score_at_entry, "wobiAtEntry": t.wobi_at_entry,
                # Pending-exit fill delay (2026-09-24 execution-simulation
                # feature — see CLAUDE.md) was computed server-side from day
                # one but never sent to the frontend at all (found in
                # review, 2026-09-24) — the dashboard had no way to
                # distinguish "armed to exit, settling any moment" from a
                # perfectly normal open position. None while no exit
                # condition has fired yet.
                "pendingExitReason": t.pending_exit_reason,
                # The trade's final outcome label once closed (2026-09-24,
                # found in review — see models.py's BNTrade.exit_reason
                # comment) — "TARGET HIT"/"STOP HIT"/"TIME_SCRATCH HIT"/
                # "EOD SQUARE-OFF"/"MANUAL EXIT". None while still open.
                "exitReason": t.exit_reason,
            }

        active = None
        if active_trade is not None:
            active = _trade_dict(active_trade)
            active["currentIndexPrice"] = st.bn_index_ltp

        active_nf = None
        if active_trade_nf is not None:
            active_nf = _trade_dict(active_trade_nf)
            active_nf["currentIndexPrice"] = st.nf_index_ltp

        def _diag_dict(d, no_active_trade: bool) -> dict:
            return {
                "time": d.time, "bnLtp": d.bn_ltp, "green": d.green, "red": d.red,
                "strongQty": d.strong_qty, "leaderRows": d.leader_rows,
                "leaderSignal": d.leader_signal, "sidewaysRange": d.sideways_range,
                "momentumOk": d.momentum_ok, "momentumReason": d.momentum_reason,
                "rsi": d.rsi, "macdDir": d.macd_dir, "macdVal": d.macd_val,
                "emaBullish": d.ema_bullish, "emaBearish": d.ema_bearish,
                "bnBull": d.bn_bull, "bnBear": d.bn_bear,
                "bnBullish": d.bn_bullish, "bnBearish": d.bn_bearish,
                "noTradeReason": d.no_trade_reason, "itmStrike": d.itm_strike,
                "itmPremium": d.itm_premium, "itmIv": d.itm_iv,
                "itmCePremium": d.itm_ce_premium, "itmPePremium": d.itm_pe_premium,
                "cooldownOk": d.cooldown_ok, "cooldownMs": d.cooldown_ms, "sidewaysOk": d.sideways_ok,
                "dirCountOk": d.dir_count_ok, "qtySurgeOk": d.qty_surge_ok,
                "sameDirectionRequired": d.same_direction_required,
                "gatesClear": d.gates_clear, "entryReady": d.entry_ready,
                "marketOpen": d.market_open, "candleCloseOk": d.candle_close_ok,
                "noActiveTrade": no_active_trade,
                # Top-8 weighted-basket scalp strategy (2026-09-21) — see
                # bn_entry_exit.evaluate_entry / config.py's "Static: Scalping
                # strategy" block.
                "basketScore": d.basket_score, "scoreThreshold": d.score_threshold,
                "top2Ok": d.top2_ok, "top2Names": d.top2_names,
                "wobi": d.wobi, "wobiMinRatio": d.wobi_min_ratio,
                "windowOk": d.window_ok, "tradesToday": d.trades_today,
                "maxTradesToday": d.max_trades_today,
                "itmOffsetPoints": d.itm_offset_points,
                "targetRs": d.target_rs, "stopRs": d.stop_rs, "timeStopS": d.time_stop_s,
            }

        # Single local-variable capture of each AppState reference (found in
        # review, 2026-09-25) — st.bn_diagnostic/nf_diagnostic/pending_entry/
        # pending_entry_nf are only ever atomically replaced wholesale or set
        # to None by the event loop, never mutated in place, so a captured
        # reference stays internally consistent for the rest of this executor-
        # thread function even if the event loop reassigns the AppState
        # attribute itself in the meantime. The bug this replaces wasn't the
        # object being unsafe to read — it was reading st.bn_diagnostic (etc.)
        # from AppState two-or-more separate times each below, which COULD
        # observe a None in between an `is not None` check and the dict-
        # literal access that followed, raising AttributeError.
        bn_diag_snap = st.bn_diagnostic
        nf_diag_snap = st.nf_diagnostic
        diag = _diag_dict(bn_diag_snap, active_trade is None) if bn_diag_snap is not None else None
        diag_nf = _diag_dict(nf_diag_snap, active_trade_nf is None) if nf_diag_snap is not None else None

        # ── Stock Candles panel data (breakout banner / weighted global signal /
        # S-R table) — a c.html UI-parity port, entirely separate from the BN
        # trading strategy above; token_to_name only exists for serializing
        # these token-keyed computations back to the name-keyed shape the
        # frontend/rest of this payload already uses. ─────────────────────────
        all_candles = self._collect_all_candles(st)
        bn_candles  = all_candles.get(cfg.BN_INDEX_TOKEN, [])
        token_to_name = {cfg.BN_INDEX_TOKEN: cfg.BN_INDEX_NAME,
                        **{tok: name for name, tok in cfg.BN_ALL_STOCKS.items()}}

        # detect_support_resistance is O(n) per token and every token's S-R
        # table entry needs it anyway — compute each token's once here (incl.
        # the index) and hand the index's result into compute_breakout_prediction
        # instead of letting it silently redo that same scan a second time.
        sr_by_token = {tok: bn_breakout.detect_support_resistance(candles)
                       for tok, candles in all_candles.items()}
        bn_swings = bn_breakout.detect_swings(bn_candles, 2)
        breakout = bn_breakout.compute_breakout_prediction(
            bn_candles, all_candles, cfg.BN_INDEX_WEIGHTS,
            swings=bn_swings,
            sr_levels=sr_by_token.get(cfg.BN_INDEX_TOKEN, {"supports": [], "resistances": []}))

        column_counts   = bn_breakout.compute_column_counts(all_candles, _NUM_SIGNAL_CANDLES)
        latest_by_token = {tok: c[-1] for tok, c in all_candles.items() if c}
        global_signal   = bn_breakout.compute_global_signal(column_counts, latest_by_token,
                                                             cfg.BN_INDEX_TOKEN, cfg.BN_INDEX_WEIGHTS)
        weighted_red_green = bn_breakout.compute_weighted_red_green(
            latest_by_token, cfg.BN_INDEX_WEIGHTS, cfg.BN_INDEX_WEIGHTS_CONFIRMED)

        # "surged" is only meaningful for the 6 leader stocks (the ones the
        # Big Trades panel shows) — computed per-bar with the exact same
        # threshold _leader_qty_surge uses for the latest bar, so the Big
        # Trades table's highlight and the Entry Loop Monitor's SURGE column
        # are always reading the identical volume + threshold.
        stock_candles = {
            token_to_name.get(tok, tok): [
                {"startTime": c.start_time, "open": c.open, "close": c.close,
                 "high": c.high, "low": c.low, "volume": c.volume, "lastQty": c.last_qty,
                 "buyQty": c.buy_qty, "sellQty": c.sell_qty,
                 "surged": (c.volume >= _stock_qty_threshold(token_to_name.get(tok, tok)))
                           if token_to_name.get(tok, tok) in cfg.BN_LEADER_STOCKS else False}
                for c in candles[-_STOCK_TABLE_BARS:]
            ]
            for tok, candles in all_candles.items()
        }
        sr_levels = {
            token_to_name.get(tok, tok): {
                "m5":  sr_by_token.get(tok, {"supports": [], "resistances": []}),
                "m15": st.sr_15m_levels.get(tok, {"supports": [], "resistances": []}),
            }
            for tok, candles in all_candles.items()
        }

        # ── Nifty 50 Stock Candles panel data — mirrors the BN block above,
        # over NF's own 32-stock + index universe, using NF_INDEX_WEIGHTS. ───
        all_candles_nf = self._collect_all_candles_nf(st)
        nf_candles     = all_candles_nf.get(cfg.NF_INDEX_TOKEN, [])
        token_to_name_nf = {cfg.NF_INDEX_TOKEN: cfg.NF_INDEX_NAME,
                           **{tok: name for name, tok in cfg.NF_ALL_STOCKS.items()}}

        sr_by_token_nf = {tok: bn_breakout.detect_support_resistance(candles)
                          for tok, candles in all_candles_nf.items()}
        nf_swings = bn_breakout.detect_swings(nf_candles, 2)
        breakout_nf = bn_breakout.compute_breakout_prediction(
            nf_candles, all_candles_nf, cfg.NF_INDEX_WEIGHTS,
            swings=nf_swings,
            sr_levels=sr_by_token_nf.get(cfg.NF_INDEX_TOKEN, {"supports": [], "resistances": []}))

        column_counts_nf   = bn_breakout.compute_column_counts(all_candles_nf, _NUM_SIGNAL_CANDLES)
        latest_by_token_nf = {tok: c[-1] for tok, c in all_candles_nf.items() if c}
        global_signal_nf   = bn_breakout.compute_global_signal(column_counts_nf, latest_by_token_nf,
                                                                cfg.NF_INDEX_TOKEN, cfg.NF_INDEX_WEIGHTS)
        weighted_red_green_nf = bn_breakout.compute_weighted_red_green(
            latest_by_token_nf, cfg.NF_INDEX_WEIGHTS, cfg.NF_INDEX_WEIGHTS_CONFIRMED)

        stock_candles_nf = {
            token_to_name_nf.get(tok, tok): [
                {"startTime": c.start_time, "open": c.open, "close": c.close,
                 "high": c.high, "low": c.low, "volume": c.volume, "lastQty": c.last_qty,
                 "buyQty": c.buy_qty, "sellQty": c.sell_qty,
                 "surged": (c.volume >= _nf_stock_qty_threshold(token_to_name_nf.get(tok, tok)))
                           if token_to_name_nf.get(tok, tok) in cfg.NF_LEADER_STOCKS else False}
                for c in candles[-_STOCK_TABLE_BARS:]
            ]
            for tok, candles in all_candles_nf.items()
        }
        sr_levels_nf = {
            token_to_name_nf.get(tok, tok): {
                "m5":  sr_by_token_nf.get(tok, {"supports": [], "resistances": []}),
                "m15": st.sr_15m_levels.get(tok, {"supports": [], "resistances": []}),
            }
            for tok, candles in all_candles_nf.items()
        }

        # Snapshot the ATM-watch fields as one atomic group (2026-09-23 fix,
        # found in review) — this method runs in a real executor thread
        # (_push_dashboard_loop), and reading these fields one at a time
        # without a lock could interleave with MarketDataService.set_bn_
        # atm_watch/set_nf_atm_watch's own group write on a strike change,
        # pairing the NEW strike's symbols with the PREVIOUS strike's
        # stale (pre-reset) LTP for one payload push. The strike itself is
        # read from st.bn_atm_watch_strike/nf_atm_watch_strike here — NOT
        # self._bn_atm_watch_symbols/_nf_atm_watch_symbols (renamed
        # 2026-09-25 from _bn_atm_watch_strike/_nf_atm_watch_strike, now
        # keyed on the built symbol pair instead of the bare strike — see
        # _tick_atm_watch's docstring; still the
        # SchedulerService-local, unlocked change-detection cache used only
        # by _tick_atm_watch on the event loop) — because that unlocked
        # instance attribute is what let this exact same executor thread
        # read a torn (new-strike, stale-symbol) pair before this fix
        # (2026-09-23, found in review): _tick_atm_watch used to write it
        # BEFORE calling set_bn_atm_watch, outside any lock.
        with st._atm_watch_lock:
            _bn_atm_watch_strike = st.bn_atm_watch_strike
            _bn_atm_ce_symbol, _bn_atm_pe_symbol = st.bn_atm_ce_symbol, st.bn_atm_pe_symbol
            _bn_atm_ce_ltp, _bn_atm_pe_ltp = st.bn_atm_ce_ltp, st.bn_atm_pe_ltp
            _nf_atm_watch_strike = st.nf_atm_watch_strike
            _nf_atm_ce_symbol, _nf_atm_pe_symbol = st.nf_atm_ce_symbol, st.nf_atm_pe_symbol
            _nf_atm_ce_ltp, _nf_atm_pe_ltp = st.nf_atm_ce_ltp, st.nf_atm_pe_ltp

        # Single local capture, same reasoning as bn_diag_snap/nf_diag_snap
        # above — used by pendingEntry/pendingEntryNf below.
        pending_entry_snap = st.pending_entry
        pending_entry_nf_snap = st.pending_entry_nf

        return {
            "type":         "STATE_UPDATE",
            "clock":        clock,
            "phase":        st.phase.value,
            "wsStatus":     st.ws_status,
            "apiStatus":    st.api_status,
            "bnLtp":        st.bn_index_ltp,
            "bnIndexSynthetic": st.bn_index_synthetic,
            "nfLtp":        st.nf_index_ltp,
            "nfIndexSynthetic": st.nf_index_synthetic,
            "dailyPnl":     round(st.daily_pnl, 2),   # shared account — BN + NF combined
            "funds":        round(st.funds, 2),        # shared account — BN + NF combined
            "activeTrade":  active,
            # Pending entry (2026-09-24 execution-simulation feature — see
            # CLAUDE.md) — armed but not yet filled, same "computed server-
            # side, never sent" gap as pendingExitReason above. Captured into
            # a local once, below, before this dict is built (found in
            # review, 2026-09-25 — this used to read st.pending_entry
            # directly, multiple separate times, inside this dict literal;
            # the PendingBNEntry instance itself is never mutated in place,
            # only ever atomically replaced wholesale (armed) or set to None
            # (resolved/abandoned) by the event loop, but THAT reassignment
            # could land between this dict's `is not None` check and its
            # field access, raising AttributeError on a None read).
            "pendingEntry": (
                {"direction": pending_entry_snap.signal.direction,
                 "armedAt": pending_entry_snap.armed_at,
                 "fillAfter": pending_entry_snap.fill_after}
                if pending_entry_snap is not None else None
            ),
            "closedTrades": [_trade_dict(t) for t in closed_trades],
            "entryLoop":    diag,
            "bnAtmWatch": {
                "strike": _bn_atm_watch_strike,
                "ceSymbol": _bn_atm_ce_symbol, "peSymbol": _bn_atm_pe_symbol,
                "ceLtp": _bn_atm_ce_ltp, "peLtp": _bn_atm_pe_ltp,
            },
            "activeTradeNf":  active_nf,
            # NF mirror of pendingEntry above.
            "pendingEntryNf": (
                {"direction": pending_entry_nf_snap.signal.direction,
                 "armedAt": pending_entry_nf_snap.armed_at,
                 "fillAfter": pending_entry_nf_snap.fill_after}
                if pending_entry_nf_snap is not None else None
            ),
            "closedTradesNf": [_trade_dict(t) for t in closed_trades_nf],
            "entryLoopNf":    diag_nf,
            "nfAtmWatch": {
                "strike": _nf_atm_watch_strike,
                "ceSymbol": _nf_atm_ce_symbol, "peSymbol": _nf_atm_pe_symbol,
                "ceLtp": _nf_atm_ce_ltp, "peLtp": _nf_atm_pe_ltp,
            },
            "liveLeaderRows": self._build_live_leader_rows(st),
            "liveLeaderRowsNf": self._build_live_leader_rows_nf(st),
            "stockCandles": stock_candles,
            "globalSignal": global_signal,
            "weightedRedGreen": weighted_red_green,
            "breakout":     breakout,
            "srLevels":     sr_levels,
            "stockCandlesNf": stock_candles_nf,
            "globalSignalNf": global_signal_nf,
            "weightedRedGreenNf": weighted_red_green_nf,
            "breakoutNf":     breakout_nf,
            "srLevelsNf":     sr_levels_nf,
        }

    async def _push_tick_updates_loop(self) -> None:
        """Live-price ticker delta — every ~100ms in all active/wait/cutoff phases."""
        st = get_state()
        while True:
            try:
                if (self._ws.count() > 0
                        and st.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF)):
                    dirty, st.dirty_ticks_push = st.dirty_ticks_push, set()
                    if dirty:
                        prices = {}
                        if cfg.BN_INDEX_TOKEN in dirty:
                            prices[cfg.BN_INDEX_NAME] = st.bn_index_ltp
                        if cfg.NF_INDEX_TOKEN in dirty:
                            prices[cfg.NF_INDEX_NAME] = st.nf_index_ltp
                        for sym in cfg.BN_ALL_STOCKS:
                            if cfg.BN_ALL_STOCKS[sym] in dirty:
                                prices[sym] = st.ltp.get(sym, 0.0)
                        for sym in cfg.NF_ALL_STOCKS:
                            if cfg.NF_ALL_STOCKS[sym] in dirty:
                                prices[sym] = st.ltp.get(sym, 0.0)
                        if prices:
                            await self._ws.broadcast(
                                json.dumps({"type": "TICK_UPDATE", "prices": prices}, default=str)
                            )
            except Exception as e:
                print(f"Tick delta push error: {e}")
            await asyncio.sleep(0.1)
