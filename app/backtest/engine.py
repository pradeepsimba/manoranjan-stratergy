from __future__ import annotations

"""
Backtest replay engine.

Steps each trading day 5-minute bar by bar, driving the SAME strategy core the
live engine uses (check_trend → compute_indicators → 8 conditions → calc_quantity).
Only the driver differs: historical bars + a simulated clock instead of a live
WebSocket feed.

Anti-look-ahead guarantees:
  * An entry decision at bar t only sees bars [.. t]; entry fills at close[t].
  * A position opened at bar t is only eligible to exit on bars > t.
  * The NIFTY index gates are computed once per bar (shared by all symbols),
    using a session VWAP accumulated forward in time — never future bars.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Dict, List, Optional, Tuple

import app.config as cfg
from app.backtest.data import SymbolSeries, load_backtest_data
from app.backtest.fills import (
    entry_fill,
    square_off_fill,
    stop_fill,
    target_fill,
)
from app.backtest.metrics import compute_metrics
from app.backtest.portfolio import BTPosition, Portfolio
from app.engine.conditions import entry_ok
from app.engine.indicator_engine import compute_indicators, session_vwap_from_cumsums
from app.engine.position_manager import calc_quantity, can_enter
from app.engine.trend_filter import check_trend, trend_blockers
from app.models import Candle

# Scan window / lookback are DYNAMIC settings (and per-run overridable), so
# they are read from cfg at call time inside the day workers — a module-level
# capture would freeze them at import.


def _scan_symbol(
    ss:                SymbolSeries,
    gidx:              int,
    day_start:         int,     # index of today's first bar in ss.series
    hour_open_day:     Dict[str, float],   # today's {HH: open} map
    nifty_daily_green: bool,
    nifty_above_vwap:  bool,
    open_syms,
    traded,
    daily_pnl:         float,
    slippage_bps:      float,
    capital:           Optional[float] = None,
) -> Optional[BTPosition]:
    """Evaluate one symbol at bar `gidx`. Returns a ready BTPosition or None."""
    ok, _ = can_enter(ss.name, open_syms, traded, daily_pnl)
    if not ok:
        return None

    cur = ss.series[gidx]
    ltp = cur.close

    # Trend gate FIRST (cheap) — daily open from today's first 5m bar, forming-hour
    # candle synthesized from 5m data, with no look-ahead. The expensive session/
    # lookback slices + indicators are built only once the gate clears.
    day_open  = ss.series[day_start].open
    hour_open = hour_open_day.get(cur.start_time[11:13], day_open)
    c1h = [Candle(start_time=cur.start_time, open=hour_open, close=ltp, high=cur.high, low=cur.low)]

    gate = check_trend(ltp, day_open, c1h, nifty_daily_green, nifty_above_vwap)
    if trend_blockers(gate):
        return None

    lo  = max(0, gidx - cfg.TALIB_LOOKBACK + 1)
    end = gidx + 1
    if end - lo < 30:
        return None

    # Zero-copy views of the precomputed SymbolSeries arrays + O(1) prefix-sum
    # session VWAP; entry_short_circuit skips TA-Lib on cheap-gate rejections.
    # With ohlcv_window supplied, candles_5m only feeds the 3-bar pattern check
    # (see compute_indicators), so slice just those bars instead of the full
    # lookback window — O(1) instead of O(TALIB_LOOKBACK) per scan.
    ind = compute_indicators(
        ss.series[end - 3 : end],
        ohlcv_window=(ss.closes[lo:end], ss.highs[lo:end],
                      ss.lows[lo:end],   ss.vols[lo:end]),
        session_vwap=session_vwap_from_cumsums(ss.cum_pv, ss.cum_v, day_start, gidx, ss.cum_tp),
        entry_short_circuit=True,
    )

    # Same shared condition table + runtime toggles as the live entry engine,
    # via the short-circuit path (no per-scan dict build in this hot loop).
    # depth_ratio=None → depth_bullish passes (no order book in history).
    if not entry_ok(ind, None):
        return None

    qty, sl_offset, target_offset = calc_quantity(ltp, ind.support_level, capital)
    if qty == 0:
        return None

    fill = entry_fill(ltp, slippage_bps)
    return BTPosition(
        symbol=ss.name, token=ss.token,
        entry_time=cur.start_time, entry_price=fill, qty=qty,
        stop_loss=round(fill - sl_offset, 2),
        target=round(fill + target_offset, 2),
        sl_offset=sl_offset, entry_gidx=gidx,
        entry_rsi=ind.rsi,
        entry_adx=ind.adx,
        entry_pattern=ind.candle_pattern,
        entry_macd=ind.macd_line,
        entry_support=ind.support_level,
    )


def _try_exit(port: Portfolio, pos: BTPosition, bar: Candle, slippage_bps: float) -> None:
    # Resolve a gap at the open FIRST: if the bar opens already past a level, that
    # level filled at the open before any intrabar move. Only when the open sits
    # between the two levels is the touch order ambiguous — and there SL wins.
    if bar.open <= pos.stop_loss:
        price, outcome = stop_fill(pos.stop_loss, bar.open, slippage_bps), "STOP"
    elif bar.open >= pos.target:
        price, outcome = target_fill(pos.target, bar.open, slippage_bps), "TARGET"
    else:
        hit_sl  = bar.low  <= pos.stop_loss
        hit_tgt = bar.high >= pos.target
        if hit_sl:        # SL wins ties — assume the adverse move came first
            price, outcome = stop_fill(pos.stop_loss, bar.open, slippage_bps), "STOP"
        elif hit_tgt:
            price, outcome = target_fill(pos.target, bar.open, slippage_bps), "TARGET"
        else:
            return
    port.close_position(pos.symbol, bar.start_time, price, outcome)


def _simulate_day(
    day:          str,
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    slippage_bps: float,
    capital:      Optional[float] = None,
    overrides:    Optional[Dict]  = None,
) -> List:
    """
    Simulate ONE trading day with its own fresh portfolio. Days are fully
    independent (INTRADAY strategy, EOD square-off, daily reset), which is what
    lets the caller run them in parallel. Returns that day's trades in close
    order.

    This per-day engine is for INTRADAY MODE on intraday timeframes (≤60m): a
    position opens on an early bar and exits on a LATER bar or at EOD
    square-off. Delivery mode routes to _simulate_range_intraday (overnight
    holds) and the 1d timeframe to _simulate_range_daily instead.

    `overrides` are the run's per-request settings; they are scoped to THIS
    worker thread only (cfg.thread_overrides), so a concurrent live session
    keeps reading the global runtime values.
    """
    with cfg.thread_overrides(overrides or {}):
        return _simulate_day_impl(day, symbols, nifty, slippage_bps, capital)


def _simulate_day_impl(
    day:          str,
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    slippage_bps: float,
    capital:      Optional[float],
) -> List:
    # Dynamic settings hoisted ONCE per day — we are inside the run's
    # thread-override scope, so these cannot change mid-day, and re-resolving
    # them per bar/per symbol costs millions of dynamic cfg lookups on a long
    # run (module __getattr__ + thread-local check each).
    scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
    cutoff     = f"{cfg.CUTOFF_HOUR:02d}:{cfg.CUTOFF_MIN:02d}"
    max_pos    = cfg.MAX_CONCURRENT_POSITIONS
    loss_limit = cfg.DAILY_LOSS_LIMIT

    port = Portfolio()
    _replay_day(port, day, symbols, nifty, slippage_bps, capital,
                scan_start, cutoff, max_pos, loss_limit)

    # 3) EOD square-off any survivors at the day's last bar close.
    for sym in list(port.positions.keys()):
        pos  = port.positions[sym]
        ss   = symbols.get(pos.token)
        idxs = ss.by_day.get(day) if ss else None
        if not idxs:
            continue
        last = ss.series[idxs[-1]]
        port.close_position(sym, last.start_time,
                            square_off_fill(last.close, slippage_bps), "EOD")

    return port.trades


def _replay_day(
    port:         Portfolio,
    day:          str,
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    slippage_bps: float,
    capital:      Optional[float],
    scan_start:   str,
    cutoff:       str,
    max_pos:      int,
    loss_limit:   float,
) -> None:
    """
    Replay ONE day's bars into `port` (exits first, then entries, per bar) —
    WITHOUT any EOD square-off, so the caller decides whether positions carry
    overnight (delivery mode) or close at the day's last bar (intraday mode).
    Positions opened on an earlier day exit from today's first bar onward
    (global bar indices grow across days, so `gidx <= entry_gidx` never blocks
    them); the gap-at-open resolution in _try_exit prices overnight gaps.
    """
    grid = sorted(nifty.at.get(day, {}).items())   # [(time, nifty_gidx), ...]
    if not grid:
        return

    nifty_day_start = nifty.by_day[day][0]
    nifty_day_open  = nifty.series[nifty_day_start].open

    # Hoist the per-day lookups out of the bar loop: each entry is
    # (ss, day_map, day_idxs, hour_open_day) for a symbol that trades today.
    # Saves ~75 bars × N symbols of repeated dict.get(day) work per day.
    day_syms = []
    by_token = {}
    for token, ss in symbols.items():
        day_map = ss.at.get(day)
        if not day_map:
            continue
        entry = (ss, day_map, ss.by_day[day], ss.hour_open.get(day, {}))
        day_syms.append(entry)
        by_token[token] = entry

    for tm, ngidx in grid:
        nifty_ltp  = nifty.series[ngidx].close
        # O(1) session VWAP from the precomputed prefix sums — same formula the
        # per-stock scan uses, forward-in-time only (no look-ahead). cum_tp
        # enables the TWAP fallback: the NIFTY feed carries volume=0 on every
        # bar, so without it this gate could never pass.
        nifty_vwap = session_vwap_from_cumsums(
            nifty.cum_pv, nifty.cum_v, nifty_day_start, ngidx, nifty.cum_tp
        )
        nifty_daily_green = nifty_ltp > nifty_day_open
        nifty_above_vwap  = nifty_vwap > 0.0 and nifty_ltp > nifty_vwap

        # 1) Exits first — only for positions opened on an earlier bar.
        for sym in list(port.positions.keys()):
            pos = port.positions[sym]
            ent = by_token.get(pos.token)
            if ent is None:
                continue
            ss, day_map, _, _ = ent
            gidx = day_map.get(tm)
            if gidx is None or gidx <= pos.entry_gidx:
                continue
            _try_exit(port, pos, ss.series[gidx], slippage_bps)

        # 2) Entries — only inside the scan window, before cutoff, and only when
        #    the portfolio can take a new position. Once 3 are open (or the loss
        #    limit is hit) the whole 500-symbol scan is skipped until a slot frees.
        if scan_start <= tm < cutoff:
            open_syms, traded, dpnl = port.snapshot()
            if len(open_syms) < max_pos and dpnl > -loss_limit:
                # Concurrent positions SHARE the account: size new entries from
                # what open positions haven't already committed (value ÷ lev).
                cap_total = capital if capital is not None else cfg.ACCOUNT_BALANCE
                available = cap_total - port.margin_used()
                signals: List[BTPosition] = []
                if available > 0:
                    for ss, day_map, day_idxs, hour_open_day in day_syms:
                        # can_enter (inside _scan_symbol) would reject these
                        # anyway — skipping here avoids the whole call for every
                        # already-traded symbol on every remaining bar of the day.
                        if ss.name in traded or ss.name in open_syms:
                            continue
                        gidx = day_map.get(tm)
                        if gidx is None:
                            continue
                        sig = _scan_symbol(
                            ss, gidx, day_idxs[0], hour_open_day,
                            nifty_daily_green, nifty_above_vwap,
                            open_syms, traded, dpnl, slippage_bps, available,
                        )
                        if sig:
                            signals.append(sig)

                # Apply fills sequentially, honoring the live circuit breakers.
                # Re-check affordability per fill: an earlier fill on this SAME
                # bar shrinks what's left for the next signal. Compare at the
                # SIZING basis (unslipped close, de-slipped exactly): the check
                # exists to catch same-bar consumption by earlier fills — NOT
                # to re-reject the slippage haircut the sizer never saw. A qty
                # capped to exactly fit `available` would otherwise be dropped
                # whenever int() slack < slippage, a systematic under-entry
                # bias vs live (which fills at the scan price).
                lev  = cfg.INTRADAY_LEVERAGE
                slip = 1.0 + slippage_bps / 10_000.0
                for sig in signals:
                    ok, _ = can_enter(sig.symbol, port.positions,
                                      port.traded_today, port.daily_pnl)
                    if not ok:
                        continue
                    if (sig.entry_price / slip * sig.qty) / lev > cap_total - port.margin_used() + 1e-9:
                        continue
                    port.open_position(sig)


def _square_off_range_end(port: Portfolio, symbols: Dict[str, SymbolSeries],
                          lo_s: str, hi_s: str, slippage_bps: float) -> None:
    """Square off survivors at each symbol's last in-range bar (positional modes)."""
    for sym in list(port.positions.keys()):
        pos = port.positions[sym]
        ss  = symbols.get(pos.token)
        if ss is None:
            continue
        last_day = max((d for d in ss.by_day if lo_s <= d <= hi_s), default=None)
        if last_day is None:
            continue
        last = ss.series[ss.by_day[last_day][-1]]
        port.close_position(sym, last.start_time,
                            square_off_fill(last.close, slippage_bps), "EOD")


def _simulate_range_intraday(
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    from_d:       date,
    to_d:         date,
    slippage_bps: float,
    capital:      Optional[float],
    overrides:    Optional[Dict],
) -> Tuple[List, int]:
    """
    DELIVERY (positional) replay on an INTRADAY timeframe: ONE portfolio across
    the whole range, stepped chronologically day by day (same per-bar engine as
    intraday mode via _replay_day). Entries still fire only inside each day's
    scan window, but there is NO EOD square-off — positions carry overnight,
    SL/target keep being checked on every later bar (overnight gaps resolve at
    the open via _try_exit), and survivors square off at each symbol's last
    in-range bar.

    Risk-guard semantics match _simulate_range_daily (the positional reading):
    `traded_today` = no re-entry for the WHOLE run, and DAILY_LOSS_LIMIT acts
    as a run-level loss stop. Sequential by construction (the portfolio
    persists across days), so no day-level parallelism here.
    """
    with cfg.thread_overrides(overrides or {}):
        lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
        days = sorted(d for d in nifty.by_day if lo_s <= d <= hi_s)
        if not days:
            return [], 0

        scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
        cutoff     = f"{cfg.CUTOFF_HOUR:02d}:{cfg.CUTOFF_MIN:02d}"
        max_pos    = cfg.MAX_CONCURRENT_POSITIONS
        loss_limit = cfg.DAILY_LOSS_LIMIT

        port = Portfolio()
        for day in days:
            _replay_day(port, day, symbols, nifty, slippage_bps, capital,
                        scan_start, cutoff, max_pos, loss_limit)

        _square_off_range_end(port, symbols, lo_s, hi_s, slippage_bps)
        return port.trades, len(days)


def _simulate_range_daily(
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    from_d:       date,
    to_d:         date,
    slippage_bps: float,
    capital:      Optional[float],
    overrides:    Optional[Dict],
) -> Tuple[List, int]:
    """
    POSITIONAL replay for the 1d timeframe: ONE portfolio across the whole
    range, stepped chronologically (bars are days, so sequential is cheap).
    An entry fills at a daily bar's close; SL/target are checked on SUBSEQUENT
    days' bars only (same gap-at-open handling as intraday via _try_exit);
    survivors square off at each symbol's last bar in range.

    Risk-guard semantics in this mode: `traded_today` = no re-entry for the
    WHOLE run, and DAILY_LOSS_LIMIT acts as a run-level loss stop — the
    natural positional reading of the intraday circuit breakers.

    _scan_symbol is reused unchanged with day_start=gidx: "day open" becomes
    the daily bar's open (daily-green = bar green; the synthesized hourly gate
    degenerates to the same signal) and the single-bar session VWAP is the
    bar's typical price, so above_vwap = close > (H+L+C)/3.
    """
    with cfg.thread_overrides(overrides or {}):
        lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
        days = sorted(d for d in nifty.by_day if lo_s <= d <= hi_s)
        if not days:
            return [], 0

        max_pos    = cfg.MAX_CONCURRENT_POSITIONS
        loss_limit = cfg.DAILY_LOSS_LIMIT
        port = Portfolio()

        for day in days:
            n_idx = nifty.by_day[day][0]
            nbar  = nifty.series[n_idx]
            nifty_daily_green = nbar.close > nbar.open
            nvwap = session_vwap_from_cumsums(nifty.cum_pv, nifty.cum_v, n_idx, n_idx, nifty.cum_tp)
            nifty_above_vwap = nvwap > 0.0 and nbar.close > nvwap

            # 1) Exits first — only for positions opened on an EARLIER day.
            for sym in list(port.positions.keys()):
                pos = port.positions[sym]
                ss  = symbols.get(pos.token)
                idxs = ss.by_day.get(day) if ss else None
                if not idxs or idxs[0] <= pos.entry_gidx:
                    continue
                _try_exit(port, pos, ss.series[idxs[0]], slippage_bps)

            # 2) Entries — collect the day's signals, then fill sequentially
            #    honoring the circuit breakers (same shape as the intraday bar).
            #    Sizing shares the account across OPEN (overnight) positions.
            open_syms, traded, dpnl = port.snapshot()
            if len(open_syms) < max_pos and dpnl > -loss_limit:
                cap_total = capital if capital is not None else cfg.ACCOUNT_BALANCE
                available = cap_total - port.margin_used()
                signals: List[BTPosition] = []
                if available > 0:
                    for token, ss in symbols.items():
                        if ss.name in traded or ss.name in open_syms:
                            continue
                        idxs = ss.by_day.get(day)
                        if not idxs:
                            continue
                        sig = _scan_symbol(
                            ss, idxs[0], idxs[0], {},
                            nifty_daily_green, nifty_above_vwap,
                            open_syms, traded, dpnl, slippage_bps, available,
                        )
                        if sig:
                            signals.append(sig)
                # Same sizing-basis (de-slipped) comparison as the intraday
                # engine — see the comment there.
                lev  = cfg.INTRADAY_LEVERAGE
                slip = 1.0 + slippage_bps / 10_000.0
                for sig in signals:
                    ok, _ = can_enter(sig.symbol, port.positions,
                                      port.traded_today, port.daily_pnl)
                    if not ok:
                        continue
                    if (sig.entry_price / slip * sig.qty) / lev > cap_total - port.margin_used() + 1e-9:
                        continue
                    port.open_position(sig)

        # 3) Square off survivors at each symbol's last in-range bar.
        _square_off_range_end(port, symbols, lo_s, hi_s, slippage_bps)
        return port.trades, len(days)


def simulate(
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    from_d:       date,
    to_d:         date,
    slippage_bps: float,
    capital:      Optional[float] = None,
    overrides:    Optional[Dict]  = None,
    timeframe:    Optional[str]   = None,
    mode:         Optional[str]   = None,
) -> Tuple[List, List, int]:
    """
    Run the full replay.
    Intraday mode on intraday timeframes: days are independent, so they execute
    in parallel across a thread pool — TA-Lib releases the GIL during the
    indicator math, giving real multi-core speedup. Per-day trades merge in
    day order.
    Delivery mode: positional — one portfolio, chronological, overnight holds
    (_simulate_range_intraday); parallelism is not possible there.
    1d: positional by construction (bars ARE days) — _simulate_range_daily
    regardless of mode.
    """
    tf = timeframe or cfg.BACKTEST_TIMEFRAME
    md = mode or cfg.BACKTEST_MODE
    if tf == "1d":
        trades, ndays = _simulate_range_daily(
            symbols, nifty, from_d, to_d, slippage_bps, capital, overrides)
    elif md == "delivery":
        trades, ndays = _simulate_range_intraday(
            symbols, nifty, from_d, to_d, slippage_bps, capital, overrides)
    else:
        lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
        days = sorted(d for d in nifty.by_day if lo_s <= d <= hi_s)
        if not days:
            return [], [], 0
        workers = max(1, min(cfg.SCAN_WORKERS, len(days)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bt-day") as pool:
            # map preserves input order → results already in chronological day order
            per_day = list(pool.map(
                lambda d: _simulate_day(d, symbols, nifty, slippage_bps, capital, overrides),
                days,
            ))
        trades = []
        for day_trades in per_day:
            trades.extend(day_trades)
        ndays = len(days)

    # Rebuild the running-equity curve from the merged, time-ordered trade stream.
    cum = 0.0
    equity_curve: List = []
    for t in trades:
        cum += t.net_pnl
        equity_curve.append((t.exit_time, round(cum, 2)))

    return trades, equity_curve, ndays


async def run_backtest(
    db, run_id: str, from_d: date, to_d: date,
    slippage_bps: float, capital: Optional[float] = None,
    overrides: Optional[Dict] = None, timeframe: Optional[str] = None,
    mode: Optional[str] = None,
) -> None:
    """Orchestrate one backtest run: fetch → simulate (in a worker thread) → persist."""
    try:
        overrides = overrides or {}
        # Warmup + timeframe affect the FETCH (event loop) — resolve them from
        # the run's overrides explicitly instead of thread-local config, which
        # must never be set on the event loop.
        tf       = timeframe or overrides.get("BACKTEST_TIMEFRAME") or cfg.BACKTEST_TIMEFRAME
        md       = mode or overrides.get("BACKTEST_MODE") or cfg.BACKTEST_MODE
        warmup   = int(overrides.get("BACKTEST_WARMUP_DAYS", cfg.BACKTEST_WARMUP_DAYS))
        # Resolve the run's TALIB_LOOKBACK here (event loop) so the warmup fetch
        # matches the window the worker threads will actually slice.
        lookback = int(overrides.get("TALIB_LOOKBACK", cfg.TALIB_LOOKBACK))
        universe, symbols, nifty = await load_backtest_data(
            from_d, to_d, warmup_days=warmup, timeframe=tf, lookback=lookback)
        if not universe:
            await db.fail_backtest_run(
                run_id, "Empty universe — the client-status API returned no stocks")
            return
        if not symbols or nifty is None:
            await db.fail_backtest_run(
                run_id, f"No {tf} data returned for {from_d} → {to_d} "
                        f"(NIFTY {'ok' if nifty else 'missing'}, {len(symbols)} symbols). "
                        f"Try a shorter date range.")
            return

        trades, equity, days = await asyncio.to_thread(
            simulate, symbols, nifty, from_d, to_d, slippage_bps, capital, overrides, tf, md
        )

        summary = compute_metrics(trades, equity, days)
        summary["universe_size"] = len(symbols)

        await db.save_backtest_trades(run_id, trades)
        await db.finish_backtest_run(run_id, summary)
        print(f"Backtest {run_id} done: {summary['total_trades']} trades, "
              f"net ₹{summary['net_pnl']:+.2f}")
    except Exception as e:
        await db.fail_backtest_run(run_id, f"{type(e).__name__}: {e}")
        print(f"Backtest {run_id} failed: {e}")
