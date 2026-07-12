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
from typing import Any, Dict, List, Optional, Tuple

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
    capital:           Optional[float] = None,   # AVAILABLE (account − margin used)
    total_capital:     Optional[float] = None,   # FULL run equity — capital_pct risk basis
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

    qty, sl_offset, target_offset = calc_quantity(ltp, ind.support_level,
                                                  capital, total_capital)
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


def _last_entry_start(tf_minutes: int) -> str:
    """
    Latest bar START time still eligible for entries: a bar decided at its
    CLOSE must close by the cutoff, so start ≤ cutoff − tf_minutes. With
    grid-aligned timings this yields the same bar set as the old `tm < cutoff`
    (5m default: 14:25), but an off-grid cutoff (e.g. 14:28) or a coarse
    timeframe (60m bar starting 14:00 closes 15:00) no longer lets the
    backtest decide on data PAST the cutoff that live could never see.
    Reads cfg at call time — must run inside the run's thread-override scope.
    """
    total = max(0, cfg.CUTOFF_HOUR * 60 + cfg.CUTOFF_MIN - tf_minutes)
    return f"{total // 60:02d}:{total % 60:02d}"


def _simulate_day(
    day:          str,
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    slippage_bps: float,
    capital:      Optional[float] = None,
    overrides:    Optional[Dict]  = None,
    tf_minutes:   int             = 5,
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
        return _simulate_day_impl(day, symbols, nifty, slippage_bps, capital,
                                  tf_minutes)


def _simulate_day_impl(
    day:          str,
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    slippage_bps: float,
    capital:      Optional[float],
    tf_minutes:   int,
) -> List:
    # Dynamic settings hoisted ONCE per day — we are inside the run's
    # thread-override scope, so these cannot change mid-day, and re-resolving
    # them per bar/per symbol costs millions of dynamic cfg lookups on a long
    # run (module __getattr__ + thread-local check each).
    scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
    last_entry = _last_entry_start(tf_minutes)
    max_pos    = cfg.MAX_CONCURRENT_POSITIONS
    loss_limit = cfg.DAILY_LOSS_LIMIT

    port = Portfolio()
    _replay_day(port, day, symbols, nifty, slippage_bps, capital,
                scan_start, last_entry, max_pos, loss_limit)

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
    last_entry:   str,   # latest eligible bar START (= cutoff − timeframe)
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

        # 2) Entries — only inside the scan window (a bar is eligible when it
        #    CLOSES by the cutoff — see _last_entry_start), and only when the
        #    portfolio can take a new position. Once 3 are open (or the loss
        #    limit is hit) the whole 500-symbol scan is skipped until a slot frees.
        if scan_start <= tm <= last_entry:
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
                            cap_total,
                        )
                        if sig:
                            signals.append(sig)

                # Apply fills sequentially, honoring the live circuit breakers.
                _fill_signals(port, signals, cap_total, slippage_bps)


def _fill_signals(port: Portfolio, signals: List[BTPosition],
                  cap_total: float, slippage_bps: float) -> None:
    """
    Apply queued entry signals sequentially, honoring the live circuit
    breakers. Re-checks affordability per fill: an earlier fill on this SAME
    bar/day shrinks what's left for the next signal. Compares at the SIZING
    basis (unslipped close, de-slipped exactly): the check exists to catch
    same-bar consumption by earlier fills — NOT to re-reject the slippage
    haircut the sizer never saw. A qty capped to exactly fit `available`
    would otherwise be dropped whenever int() slack < slippage, a systematic
    under-entry bias vs live (which sizes at the scan price). Shared by the
    intraday per-bar loop (_replay_day) and the daily positional loop
    (_simulate_range_daily) — keep them provably in sync.
    """
    # Deterministic fill priority: tightest stop relative to price first
    # (best risk-efficiency), symbol as tiebreak. Without an explicit order,
    # which 3 of N same-bar signals win the concurrent-position slots depends
    # on universe-fetch order (backtest) / set-hash order (live) — silently
    # nondeterministic books. The LIVE fill loop sorts by the same key.
    signals = sorted(signals, key=lambda s: (
        s.sl_offset / s.entry_price if s.entry_price > 0 else float("inf"),
        s.symbol))
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


def _delivery_overrides(user_overrides: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Map the DELIVERY_* dynamic settings onto the plain keys the shared strategy
    core reads (calc_quantity, can_enter, conditions.py, trend_filter.py), so a
    positional replay gets delivery's own stop/target/risk/leverage/toggle
    profile WITHOUT forking any of that shared logic. Resolved via cfg (not a
    literal dict) so a request's own DELIVERY_* overrides — already applied by
    the caller's outer thread_overrides — flow through.

    A plain key the request EXPLICITLY overrode (e.g. {"RR_RATIO": 3.0}) is
    dropped from the shadow map so the user's value wins — otherwise the run
    would record the override in backtest_runs.params yet silently trade the
    delivery default instead.
    """
    shadow = {
        "MIN_SL_OFFSET":            cfg.DELIVERY_MIN_SL_OFFSET,
        "RR_RATIO":                 cfg.DELIVERY_RR_RATIO,
        "RISK_MODE":                cfg.DELIVERY_RISK_MODE,
        "RISK_PER_TRADE":           cfg.DELIVERY_RISK_PER_TRADE,
        "RISK_CAPITAL_PCT":         cfg.DELIVERY_RISK_CAPITAL_PCT,
        "MAX_CONCURRENT_POSITIONS": cfg.DELIVERY_MAX_CONCURRENT_POSITIONS,
        "DAILY_LOSS_LIMIT":         cfg.DELIVERY_DAILY_LOSS_LIMIT,
        "INTRADAY_LEVERAGE":        cfg.DELIVERY_LEVERAGE,
        "COND_NEAR_SUPPORT":        cfg.DELIVERY_COND_NEAR_SUPPORT,
        "COND_BULLISH_PATTERN":     cfg.DELIVERY_COND_BULLISH_PATTERN,
        "COND_ADX":                 cfg.DELIVERY_COND_ADX,
        "COND_RSI":                 cfg.DELIVERY_COND_RSI,
        "COND_MACD_CROSS":          cfg.DELIVERY_COND_MACD_CROSS,
        "COND_VOLUME_SURGE":        cfg.DELIVERY_COND_VOLUME_SURGE,
        "COND_ABOVE_VWAP":          cfg.DELIVERY_COND_ABOVE_VWAP,
        "GATE_STOCK_DAILY":         cfg.DELIVERY_GATE_STOCK_DAILY,
        "GATE_STOCK_HOURLY":        cfg.DELIVERY_GATE_STOCK_HOURLY,
        "GATE_NIFTY_DAILY":         cfg.DELIVERY_GATE_NIFTY_DAILY,
        "GATE_NIFTY_VWAP":          cfg.DELIVERY_GATE_NIFTY_VWAP,
        # CNC cost profile: STT on BOTH legs, higher stamp, flat DP per sell,
        # usually zero brokerage — the intraday cost defaults understate
        # delivery costs ~9x on STT alone.
        "COST_BROKERAGE_PCT":       cfg.DELIVERY_COST_BROKERAGE_PCT,
        "COST_STT_SELL":            cfg.DELIVERY_COST_STT,
        "COST_STT_BUY":             cfg.DELIVERY_COST_STT,
        "COST_STAMP_BUY":           cfg.DELIVERY_COST_STAMP,
        "COST_DP_SELL":             cfg.DELIVERY_COST_DP,
    }
    for key in (user_overrides or {}):
        shadow.pop(key, None)
    return shadow


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
    tf_minutes:   int = 5,
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
    with cfg.thread_overrides(overrides or {}), cfg.thread_overrides(_delivery_overrides(overrides)):
        lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
        days = sorted(d for d in nifty.by_day if lo_s <= d <= hi_s)
        if not days:
            return [], 0

        scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
        last_entry = _last_entry_start(tf_minutes)
        max_pos    = cfg.MAX_CONCURRENT_POSITIONS
        loss_limit = cfg.DAILY_LOSS_LIMIT

        port = Portfolio()
        for day in days:
            _replay_day(port, day, symbols, nifty, slippage_bps, capital,
                        scan_start, last_entry, max_pos, loss_limit)

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
    with cfg.thread_overrides(overrides or {}), cfg.thread_overrides(_delivery_overrides(overrides)):
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
                            cap_total,
                        )
                        if sig:
                            signals.append(sig)
                _fill_signals(port, signals, cap_total, slippage_bps)

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
    tf_min = cfg.TIMEFRAME_MINUTES.get(tf, 5)
    if tf == "1d":
        trades, ndays = _simulate_range_daily(
            symbols, nifty, from_d, to_d, slippage_bps, capital, overrides)
    elif md == "delivery":
        trades, ndays = _simulate_range_intraday(
            symbols, nifty, from_d, to_d, slippage_bps, capital, overrides, tf_min)
    else:
        lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
        days = sorted(d for d in nifty.by_day if lo_s <= d <= hi_s)
        if not days:
            return [], [], 0
        workers = max(1, min(cfg.SCAN_WORKERS, len(days)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bt-day") as pool:
            # map preserves input order → results already in chronological day order
            per_day = list(pool.map(
                lambda d: _simulate_day(d, symbols, nifty, slippage_bps, capital,
                                        overrides, tf_min),
                days,
            ))
        trades = []
        for day_trades in per_day:
            trades.extend(day_trades)
        ndays = len(days)

    # Square-offs are appended in ENTRY order and a symbol whose data ends early
    # (halt / missing days) exits at ITS last bar — both can land trades in the
    # list after trades that closed later in wall-clock time. Sort by exit_time
    # (ISO strings) so the equity curve / max-drawdown are computed over the
    # true chronological P&L sequence; stable sort keeps same-timestamp order.
    trades.sort(key=lambda t: t.exit_time)

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
        # A partially failed batched fetch silently drops whole batches — a run
        # over 20% of the universe would otherwise complete 'done' with biased
        # results and no error surface (the batch errors only reach the LIVE
        # dashboard's api_status). Some symbols legitimately lack data in the
        # range, so only a gross shortfall fails the run.
        if len(symbols) < len(universe) * 0.5:
            await db.fail_backtest_run(
                run_id, f"Partial data: only {len(symbols)} of {len(universe)} "
                        f"universe symbols returned {tf} candles — historical API "
                        f"batches likely failed; retry the run.")
            return

        trades, equity, days = await asyncio.to_thread(
            simulate, symbols, nifty, from_d, to_d, slippage_bps, capital, overrides, tf, md
        )

        summary = compute_metrics(trades, equity, days)
        summary["universe_size"] = len(symbols)
        # The ACTUAL replayed span: if the data server holds less history than
        # requested, the replay silently begins at the real start of the data
        # — record it so the UI can show "data starts <date>" instead of the
        # user wondering why an old range produced few days.
        lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
        in_range = sorted(d for d in nifty.by_day if lo_s <= d <= hi_s)
        if in_range:
            summary["data_from"], summary["data_to"] = in_range[0], in_range[-1]

        await db.save_backtest_trades(run_id, trades)
        await db.finish_backtest_run(run_id, summary)
        print(f"Backtest {run_id} done: {summary['total_trades']} trades, "
              f"net ₹{summary['net_pnl']:+.2f}")
    except Exception as e:
        await db.fail_backtest_run(run_id, f"{type(e).__name__}: {e}")
        print(f"Backtest {run_id} failed: {e}")
