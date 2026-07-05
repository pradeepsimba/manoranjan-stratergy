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
        session_vwap=session_vwap_from_cumsums(ss.cum_pv, ss.cum_v, day_start, gidx),
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

    This engine is intraday-only: a position opens on an early bar and exits on
    a LATER bar or at EOD square-off. Timeframes coarser than 1h yield too few
    bars per day for that to be meaningful, so the API rejects them upstream.

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
    grid = sorted(nifty.at.get(day, {}).items())   # [(time, nifty_gidx), ...]
    if not grid:
        return []

    # Dynamic settings hoisted ONCE per day — we are inside the run's
    # thread-override scope, so these cannot change mid-day, and re-resolving
    # them per bar/per symbol costs millions of dynamic cfg lookups on a long
    # run (module __getattr__ + thread-local check each).
    scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
    cutoff     = f"{cfg.CUTOFF_HOUR:02d}:{cfg.CUTOFF_MIN:02d}"
    max_pos    = cfg.MAX_CONCURRENT_POSITIONS
    loss_limit = cfg.DAILY_LOSS_LIMIT

    port = Portfolio()
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
        # per-stock scan uses, forward-in-time only (no look-ahead).
        nifty_vwap = session_vwap_from_cumsums(
            nifty.cum_pv, nifty.cum_v, nifty_day_start, ngidx
        )
        nifty_daily_green = nifty_ltp > nifty_day_open
        # nifty_vwap==0 means NIFTY has no volume data — block entry (conservative)
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
                signals: List[BTPosition] = []
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
                        open_syms, traded, dpnl, slippage_bps, capital,
                    )
                    if sig:
                        signals.append(sig)

                # Apply fills sequentially, honoring the live circuit breakers.
                for sig in signals:
                    ok, _ = can_enter(sig.symbol, port.positions,
                                      port.traded_today, port.daily_pnl)
                    if ok:
                        port.open_position(sig)

    # 3) EOD square-off any survivors at the day's last bar close.
    for sym in list(port.positions.keys()):
        pos = port.positions[sym]
        ent = by_token.get(pos.token)
        if ent is None:
            continue
        ss, _, day_idxs, _ = ent
        last = ss.series[day_idxs[-1]]
        port.close_position(sym, last.start_time,
                            square_off_fill(last.close, slippage_bps), "EOD")

    return port.trades


def simulate(
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    from_d:       date,
    to_d:         date,
    slippage_bps: float,
    capital:      Optional[float] = None,
    overrides:    Optional[Dict]  = None,
) -> Tuple[List, List, int]:
    """
    Run the full replay. Days are independent, so they execute in parallel across
    a thread pool — TA-Lib releases the GIL during the indicator math, giving real
    multi-core speedup. Per-day trades are merged in chronological order and the
    equity curve is rebuilt from the merged stream.
    """
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

    trades: List = []
    for day_trades in per_day:
        trades.extend(day_trades)

    # Rebuild the running-equity curve from the merged, time-ordered trade stream.
    cum = 0.0
    equity_curve: List = []
    for t in trades:
        cum += t.net_pnl
        equity_curve.append((t.exit_time, round(cum, 2)))

    return trades, equity_curve, len(days)


async def run_backtest(
    db, run_id: str, from_d: date, to_d: date,
    slippage_bps: float, capital: Optional[float] = None,
    overrides: Optional[Dict] = None, timeframe: Optional[str] = None,
) -> None:
    """Orchestrate one backtest run: fetch → simulate (in a worker thread) → persist."""
    try:
        overrides = overrides or {}
        # Warmup + timeframe affect the FETCH (event loop) — resolve them from
        # the run's overrides explicitly instead of thread-local config, which
        # must never be set on the event loop.
        tf       = timeframe or overrides.get("BACKTEST_TIMEFRAME") or cfg.BACKTEST_TIMEFRAME
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
            simulate, symbols, nifty, from_d, to_d, slippage_bps, capital, overrides
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
