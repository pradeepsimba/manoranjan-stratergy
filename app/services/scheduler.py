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
import json
from collections import deque as _deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List
from zoneinfo import ZoneInfo

import numpy as np

import app.config as cfg
from app.engine import bn_breakout
from app.engine.bn_entry_exit import _leader_qty_surge, _stock_qty_threshold, evaluate_entry
from app.models import BNTrade, PositionStatus, TradingPhase
from app.services import bn_trade
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


_LEADER_HISTORY_BARS = 25   # covers both pattern (last 3) and qty-avg (last 20) window

# ── Stock Candles panel (c.html port, unrelated to the BN trading strategy) ──
_STOCK_TABLE_BARS   = 15   # bars per stock sent for the live candle table
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

    async def _load_funds(self) -> None:
        st = get_state()
        try:
            stored = await self._db.get_app_settings()
        except Exception as e:
            print(f"Funds load failed (using default): {e}")
            stored = {}
        funds = stored.get(BN_FUNDS_KEY)
        st.funds = float(funds) if isinstance(funds, (int, float)) else cfg.BN_STARTING_FUNDS

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
                if len(st.bn_index_candles_5m) > cfg.MAX_CANDLE_BUFFER:
                    del st.bn_index_candles_5m[:len(st.bn_index_candles_5m) - cfg.MAX_CANDLE_BUFFER]
            st.bn_synthetic_anchor = bars[-1].close
            print(f"Synthetic BankNifty index: restored {len(st.bn_index_candles_5m)} self-recorded "
                  f"bars from archive, anchor seeded at {st.bn_synthetic_anchor:.2f}")
        else:
            st.bn_synthetic_anchor = 55000.0
            print("Synthetic BankNifty index anchor: no self-recorded history yet, "
                  f"using placeholder {st.bn_synthetic_anchor:.2f}")

    async def _persist_funds(self) -> None:
        try:
            await self._db.set_app_settings({BN_FUNDS_KEY: get_state().funds})
        except Exception as e:
            print(f"Funds persist failed: {e}")

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
                    # the DB BEFORE the WS starts.
                    if not st.closed_trades and st.active_trade is None:
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
        Tick-wise engine. Every TICK_EVAL_INTERVAL_MS:
          • Exit management for the active trade (always, if one is open).
          • Entry evaluation the instant a NEW BankNifty 5m bar closes.
        Only 7 instruments' RSI/MACD/EMA math — cheap enough to run directly
        on the event loop, no thread pool needed (unlike the equity engine's
        hundreds-of-symbols scan).
        """
        print("=== ACTIVE: tick-wise engine open ===")
        st = get_state()

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
            except Exception as e:
                print(f"Tick loop error: {e}")

            await asyncio.sleep(max(0.0, cfg.TICK_EVAL_INTERVAL_MS / 1000.0))

    async def _tick_exits(self) -> None:
        st = get_state()
        if st.active_trade is None or st.bn_index_ltp <= 0:
            return
        with st._bn_index_lock:
            bn_candles = list(st.bn_index_candles_5m)
        if not bn_candles:
            return
        closes = np.fromiter((c.close for c in bn_candles), np.float64, len(bn_candles))
        lookback = closes[-cfg.BN_IV_LOOKBACK_BARS:] if closes.size > cfg.BN_IV_LOOKBACK_BARS else closes
        try:
            closed = bn_trade.check_tick_exit(_now(), st.bn_index_ltp, lookback)
            if closed:
                await self._db.update_position_exit(
                    order_id=closed.order_id, exit_price=closed.exit_index_price,
                    exit_time=closed.exit_time, pnl=closed.pnl,
                    exit_premium=closed.exit_premium,
                )
                await self._persist_funds()
        except Exception as e:
            print(f"Tick exit error: {e}")

    async def _tick_entries(self) -> None:
        st = get_state()
        if st.phase != TradingPhase.ACTIVE or st.active_trade is not None:
            return

        with st._bn_index_lock:
            bn_candles = list(st.bn_index_candles_5m)
        if not bn_candles:
            return

        new_bar_time = bn_candles[-1].start_time
        if new_bar_time == st.last_evaluated_bar:
            return   # already evaluated this bar — wait for the NEXT close
        st.last_evaluated_bar = new_bar_time

        # bn_candles[-1] just APPEARED this instant (market_data._upsert only
        # appends on a newer start_time, mutating in place otherwise) — that
        # means it's the brand-new, just-STARTED bar, often barely one tick
        # old. The bar that actually just closed is whatever came before it.
        # Evaluate only against bars strictly older than this new one (a
        # start_time filter, not a blind [:-1], so a leader stock whose own
        # feed hasn't rolled over yet doesn't lose its still-valid closed bar).
        closed_bn_candles = [c for c in bn_candles if c.start_time < new_bar_time]
        if not closed_bn_candles:
            return

        bn_recent = closed_bn_candles[-max(20, cfg.BN_ATR_PERIOD + 5):]
        closes = np.fromiter((c.close for c in closed_bn_candles), np.float64, len(closed_bn_candles))
        bn_closes_lookback = closes[-cfg.BN_INDICATOR_LOOKBACK_BARS:] if closes.size > cfg.BN_INDICATOR_LOOKBACK_BARS else closes

        leader_recent = {}
        for name, token in cfg.BN_LEADER_STOCKS.items():
            with st.candle_lock(token):
                candles = list(st.candles_5m.get(token, []))
            closed = [c for c in candles if c.start_time < new_bar_time]
            leader_recent[name] = closed[-_LEADER_HISTORY_BARS:]

        last_exit_time = (datetime.fromisoformat(st.last_exit_time)
                          if st.last_exit_time else None)
        now = _now()
        signal, diagnostic = evaluate_entry(now, bn_recent, bn_closes_lookback,
                                            leader_recent, last_exit_time)
        st.bn_diagnostic = diagnostic

        if signal is None or signal.bar_time == st.last_trade_candle:
            return

        trade = bn_trade.place_paper_order(signal, now)
        try:
            await self._db.save_position(trade)
        except Exception as e:
            print(f"DB save_position error: {e}")

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

        for r in rows:
            status = (PositionStatus(r["status"])
                      if r.get("status") in ("OPEN", "CLOSED") else PositionStatus.OPEN)
            trade = BNTrade(
                direction=str(r.get("direction") or "BUY"),
                entry_index_price=_f(r.get("entry_price")),
                entry_time=str(r.get("entry_time") or ""),
                target=_f(r.get("target")),
                current_sl=_f(r.get("stop_loss")),
                strike=int(r.get("strike") or 0),
                option_type=str(r.get("option_type") or "CE"),
                expiry=str(r.get("expiry") or ""),
                entry_premium=_f(r.get("entry_premium")),
                lot_size=int(r.get("quantity") or cfg.BN_LOT_SIZE),
                order_id=str(r.get("order_id") or ""),
                status=status,
                exit_index_price=float(r["exit_price"]) if r.get("exit_price") is not None else None,
                exit_time=r.get("exit_time"),
                exit_premium=float(r["exit_premium"]) if r.get("exit_premium") is not None else None,
                pnl=_f(r.get("pnl")),
            )
            if status == PositionStatus.CLOSED:
                st.closed_trades.append(trade)
                st.daily_pnl += trade.pnl
                if trade.exit_time:
                    st.last_exit_time = trade.exit_time
            else:
                st.active_trade = trade
                st.last_trade_candle = trade.entry_time[:16]

        print(
            f"=== RECOVERY: restored {'1 open' if st.active_trade else '0 open'} / "
            f"{len(st.closed_trades)} closed trades | daily P&L ₹{st.daily_pnl:+.2f} ==="
        )

    async def _run_eod(self) -> None:
        st = get_state()

        if not st.closed_trades and st.active_trade is None:
            await self._restore_from_db()

        if st.active_trade is not None and st.bn_index_ltp > 0:
            with st._bn_index_lock:
                bn_candles = list(st.bn_index_candles_5m)
            if bn_candles:
                closes = np.fromiter((c.close for c in bn_candles), np.float64, len(bn_candles))
                lookback = closes[-cfg.BN_IV_LOOKBACK_BARS:] if closes.size > cfg.BN_IV_LOOKBACK_BARS else closes
                closed = bn_trade.force_close(_now(), st.bn_index_ltp, lookback)
                if closed:
                    try:
                        await self._db.update_position_exit(
                            order_id=closed.order_id, exit_price=closed.exit_index_price,
                            exit_time=closed.exit_time, pnl=closed.pnl,
                            exit_premium=closed.exit_premium,
                        )
                    except Exception as e:
                        print(f"EOD square-off DB error: {e}")

        await self._mkt.stop()
        await self._persist_funds()

        # Grow our own BankNifty history archive (see save_bn_index_bars) —
        # the external server never gives us more than "today", so this is
        # the only way multi-day backtesting becomes possible over time.
        with st._bn_index_lock:
            bn_snapshot = list(st.bn_index_candles_5m)
        if bn_snapshot:
            try:
                await self._db.save_bn_index_bars(bn_snapshot)
            except Exception as e:
                print(f"BN index history save error: {e}")

        trades  = st.closed_trades
        total   = len(trades)
        winners = sum(1 for t in trades if t.pnl > 0)

        peak = cum = max_dd = 0.0
        for t in sorted(trades, key=lambda x: (x.exit_time or "")):
            cum += t.pnl
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        if total > 0 or st.daily_pnl != 0.0:
            try:
                await self._db.upsert_daily_stats(
                    total_trades=total, winning_trades=winners,
                    total_pnl=st.daily_pnl, gemini_shortlist=None,
                    max_drawdown=round(max_dd, 2),
                )
            except Exception as e:
                print(f"EOD stats error: {e}")
        else:
            print("=== EOD: no session state in this process — daily_stats write skipped ===")

        print(f"=== EOD: {total} trades | {winners} winners | Daily PnL ₹{st.daily_pnl:+.2f} ===")

        st.active_trade = None
        st.closed_trades.clear()
        st.last_trade_candle = None
        st.last_exit_time = None
        st.last_evaluated_bar = None
        st.bn_diagnostic = None
        st.daily_pnl = 0.0
        st.ltp.clear()
        st.candles_5m.clear()
        # bn_index_candles_5m is intentionally NOT cleared here — see
        # _load_all_historical: this market-data server has no historical
        # ARCHIVE for the BankNifty index (confirmed empirically — every
        # from_date/to_date range returns only the current day's bars,
        # unlike individual stocks and NIFTY 50, which both return full
        # multi-day history). The composite indicator gate needs
        # BN_INDICATOR_LOOKBACK_BARS (default 200) bars to converge, so the
        # ONLY way to ever have that much BankNifty history is to let live
        # WS ticks accumulate across real trading days (capped at
        # MAX_CANDLE_BUFFER=300, ~4 sessions) — clearing it nightly would
        # mean the gate never converges, ever.

    # ── Historical data loader ────────────────────────────────────────────────

    async def _load_all_historical(self) -> None:
        """
        Loads 5 days of history for the 11 BN stocks (fully archived on this
        server) and merges TODAY's BankNifty bars into whatever's already
        accumulated in bn_index_candles_5m from prior live sessions — the
        BankNifty history fetch itself only ever returns today (see the note
        in _run_eod), so this is a same-day upsert, never a multi-day load.
        """
        st = get_state()
        try:
            hist = await fetch_indicator_history(cfg.BN_ALL_STOCKS, cfg.INTERVAL_5M, days_back=5)
            for token_key, candles in hist.items():
                st.candles_5m[token_key] = _deque(candles, maxlen=cfg.MAX_CANDLE_BUFFER)
                st.tick_version[token_key] = st.tick_version.get(token_key, 0) + 1

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
                if len(st.bn_index_candles_5m) > cfg.MAX_CANDLE_BUFFER:
                    del st.bn_index_candles_5m[:len(st.bn_index_candles_5m) - cfg.MAX_CANDLE_BUFFER]

            st.api_status = "API OK"
            print(f"Historical load complete: {len(st.candles_5m)} stocks | "
                  f"BankNifty buffer now {len(st.bn_index_candles_5m)} bars")
        except Exception as e:
            st.api_status = f"Load error: {e}"
            print(f"Historical load error: {e}")

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
        while True:
            try:
                if self._ws.count() > 0:
                    await self._ws.broadcast(json.dumps(self._build_payload(), default=str))
            except Exception as e:
                print(f"Dashboard push error: {e}")
            await asyncio.sleep(1)

    def _collect_all_candles(self, st) -> dict:
        """BankNifty index + all 11 BN stocks, keyed by TOKEN — for the Stock
        Candles panel (breakout/S-R/global-signal), unrelated to the BN
        trading strategy's own candle reads elsewhere in this file."""
        out = {}
        with st._bn_index_lock:
            out[cfg.BN_INDEX_TOKEN] = list(st.bn_index_candles_5m)
        for token in cfg.BN_ALL_STOCKS.values():
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

    def _build_payload(self) -> dict:
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
            }

        active = None
        if st.active_trade is not None:
            active = _trade_dict(st.active_trade)
            active["currentIndexPrice"] = st.bn_index_ltp

        diag = None
        if st.bn_diagnostic is not None:
            d = st.bn_diagnostic
            diag = {
                "time": d.time, "bnLtp": d.bn_ltp, "green": d.green, "red": d.red,
                "strongQty": d.strong_qty, "leaderRows": d.leader_rows,
                "leaderSignal": d.leader_signal, "sidewaysRange": d.sideways_range,
                "momentumOk": d.momentum_ok, "momentumReason": d.momentum_reason,
                "rsi": d.rsi, "macdDir": d.macd_dir, "macdVal": d.macd_val,
                "emaBullish": d.ema_bullish, "emaBearish": d.ema_bearish,
                "bnBull": d.bn_bull, "bnBear": d.bn_bear,
                "bnBullish": d.bn_bullish, "bnBearish": d.bn_bearish,
                "noTradeReason": d.no_trade_reason, "atmStrike": d.atm_strike,
                "atmPremium": d.atm_premium, "atmIv": d.atm_iv,
                "cooldownOk": d.cooldown_ok, "sidewaysOk": d.sideways_ok,
                "dirCountOk": d.dir_count_ok, "qtySurgeOk": d.qty_surge_ok,
                "sameDirectionRequired": d.same_direction_required,
                "gatesClear": d.gates_clear, "entryReady": d.entry_ready,
                "marketOpen": d.market_open, "candleCloseOk": d.candle_close_ok,
                "noActiveTrade": st.active_trade is None,
            }

        # ── Stock Candles panel data (breakout banner / weighted global signal /
        # S-R table) — a c.html UI-parity port, entirely separate from the BN
        # trading strategy above; token_to_name only exists for serializing
        # these token-keyed computations back to the name-keyed shape the
        # frontend/rest of this payload already uses. ─────────────────────────
        all_candles = self._collect_all_candles(st)
        bn_candles  = all_candles.get(cfg.BN_INDEX_TOKEN, [])
        token_to_name = {cfg.BN_INDEX_TOKEN: cfg.BN_INDEX_NAME,
                        **{tok: name for name, tok in cfg.BN_ALL_STOCKS.items()}}

        breakout = bn_breakout.compute_breakout_prediction(bn_candles, all_candles)

        column_counts   = bn_breakout.compute_column_counts(all_candles, _NUM_SIGNAL_CANDLES)
        latest_by_token = {tok: c[-1] for tok, c in all_candles.items() if c}
        global_signal   = bn_breakout.compute_global_signal(column_counts, latest_by_token, cfg.BN_INDEX_TOKEN)

        # "surged" is only meaningful for the 6 leader stocks (the ones the
        # Big Trades panel shows) — computed per-bar with the exact same
        # threshold _leader_qty_surge uses for the latest bar, so the Big
        # Trades table's highlight and the Entry Loop Monitor's SURGE column
        # are always reading the identical volume + threshold.
        stock_candles = {
            token_to_name.get(tok, tok): [
                {"startTime": c.start_time, "open": c.open, "close": c.close,
                 "high": c.high, "low": c.low, "volume": c.volume, "lastQty": c.last_qty,
                 "surged": (c.volume >= _stock_qty_threshold(token_to_name.get(tok, tok)))
                           if token_to_name.get(tok, tok) in cfg.BN_LEADER_STOCKS else False}
                for c in candles[-_STOCK_TABLE_BARS:]
            ]
            for tok, candles in all_candles.items()
        }
        sr_levels = {
            token_to_name.get(tok, tok): {
                "m5":  bn_breakout.detect_support_resistance(candles),
                "m15": st.sr_15m_levels.get(tok, {"supports": [], "resistances": []}),
            }
            for tok, candles in all_candles.items()
        }

        return {
            "type":         "STATE_UPDATE",
            "clock":        clock,
            "phase":        st.phase.value,
            "wsStatus":     st.ws_status,
            "apiStatus":    st.api_status,
            "bnLtp":        st.bn_index_ltp,
            "bnIndexSynthetic": st.bn_index_synthetic,
            "dailyPnl":     round(st.daily_pnl, 2),
            "funds":        round(st.funds, 2),
            "activeTrade":  active,
            "closedTrades": [_trade_dict(t) for t in st.closed_trades],
            "entryLoop":    diag,
            "liveLeaderRows": self._build_live_leader_rows(st),
            "stockCandles": stock_candles,
            "globalSignal": global_signal,
            "breakout":     breakout,
            "srLevels":     sr_levels,
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
                        for sym in cfg.BN_ALL_STOCKS:
                            if cfg.BN_ALL_STOCKS[sym] in dirty:
                                prices[sym] = st.ltp.get(sym, 0.0)
                        if prices:
                            await self._ws.broadcast(
                                json.dumps({"type": "TICK_UPDATE", "prices": prices}, default=str)
                            )
            except Exception as e:
                print(f"Tick delta push error: {e}")
            await asyncio.sleep(0.1)
