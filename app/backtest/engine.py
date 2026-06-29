from __future__ import annotations

"""
Backtest replay engine.

Steps each trading day 5-minute bar by bar, driving the SAME strategy core the
live engine uses (check_trend → compute_indicators → 7 conditions → calc_quantity).
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
from app.engine.indicator_engine import compute_indicators
from app.engine.position_manager import calc_quantity, can_enter
from app.engine.trend_filter import check_trend
from app.models import Candle

_SCAN_START  = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"   # "09:45"
_CUTOFF      = f"{cfg.CUTOFF_HOUR:02d}:{cfg.CUTOFF_MIN:02d}"           # "14:30"
_LOOKBACK    = cfg.TALIB_LOOKBACK + 40   # +40 covers pattern lookback and swing_low bars


def _scan_symbol(
    ss:                SymbolSeries,
    gidx:              int,
    day:               str,
    nifty_daily_green: bool,
    nifty_above_vwap:  bool,
    open_syms,
    traded,
    daily_pnl:         float,
    slippage_bps:      float,
    capital:           float = cfg.ACCOUNT_BALANCE,
) -> Optional[BTPosition]:
    """Evaluate one symbol at bar `gidx`. Returns a ready BTPosition or None."""
    ok, _ = can_enter(ss.name, open_syms, traded, daily_pnl)
    if not ok:
        return None

    day_idxs = ss.by_day.get(day)
    if not day_idxs:
        return None
    day_start = day_idxs[0]
    session   = ss.series[day_start : gidx + 1]

    cur = ss.series[gidx]
    ltp = cur.close

    # Trend gate FIRST (cheap) — daily open from today's first 5m bar, forming-hour
    # candle synthesized from 5m data, with no look-ahead. The expensive lookback
    # slice + indicators are built only once the gate clears.
    day_open  = ss.series[day_start].open
    cur_hour  = cur.start_time[11:13]
    hour_open = next((b.open for b in session if b.start_time[11:13] == cur_hour), day_open)
    c1h = [Candle(start_time=cur.start_time, open=hour_open, close=ltp, high=cur.high, low=cur.low)]

    gate = check_trend(ltp, day_open, c1h, nifty_daily_green, nifty_above_vwap)
    if not gate.all_clear:
        return None

    lo       = max(0, gidx - _LOOKBACK + 1)
    lookback = ss.series[lo : gidx + 1]
    if len(lookback) < 30:
        return None

    ind = compute_indicators(lookback, session_candles_5m=session)

    if not (ind.near_support and ind.bullish_pattern and ind.adx_ok
            and (ind.rsi_above_30 or ind.rsi_rising) and ind.macd_bullish_cross
            and ind.volume_surge and ind.price_above_vwap):
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
    capital:      float = cfg.ACCOUNT_BALANCE,
) -> List:
    """
    Simulate ONE trading day with its own fresh portfolio. Days are fully
    independent (intraday strategy, EOD square-off, daily reset), which is what
    lets the caller run them in parallel. Returns that day's trades in close
    order.
    """
    grid = sorted(nifty.at.get(day, {}).items())   # [(time, nifty_gidx), ...]
    if not grid:
        return []

    port = Portfolio()
    nifty_day_open = nifty.series[nifty.by_day[day][0]].open
    cum_tpv = 0.0
    cum_vol = 0.0

    for tm, ngidx in grid:
        nbar = nifty.series[ngidx]
        cum_tpv += ((nbar.high + nbar.low + nbar.close) / 3.0) * nbar.volume
        cum_vol += nbar.volume
        nifty_ltp  = nbar.close
        nifty_vwap = (cum_tpv / cum_vol) if cum_vol > 0 else 0.0
        nifty_daily_green = nifty_ltp > nifty_day_open
        # nifty_vwap==0 means NIFTY has no volume data — degrade gracefully
        nifty_above_vwap  = (nifty_vwap == 0.0) or (nifty_ltp > nifty_vwap)

        # 1) Exits first — only for positions opened on an earlier bar.
        for sym in list(port.positions.keys()):
            pos     = port.positions[sym]
            ss      = symbols.get(pos.token)
            day_map = ss.at.get(day) if ss else None
            gidx    = day_map.get(tm) if day_map else None
            if gidx is None or gidx <= pos.entry_gidx:
                continue
            _try_exit(port, pos, ss.series[gidx], slippage_bps)

        # 2) Entries — only inside the scan window, before cutoff, and only when
        #    the portfolio can take a new position. Once 3 are open (or the loss
        #    limit is hit) the whole 500-symbol scan is skipped until a slot frees.
        if _SCAN_START <= tm < _CUTOFF:
            open_syms, traded, dpnl = port.snapshot()
            if (len(open_syms) < cfg.MAX_CONCURRENT_POSITIONS
                    and dpnl > -cfg.DAILY_LOSS_LIMIT):
                signals: List[BTPosition] = []
                for token, ss in symbols.items():
                    day_map = ss.at.get(day)
                    gidx    = day_map.get(tm) if day_map else None
                    if gidx is None:
                        continue
                    sig = _scan_symbol(
                        ss, gidx, day, nifty_daily_green, nifty_above_vwap,
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
        pos  = port.positions[sym]
        ss   = symbols.get(pos.token)
        idxs = ss.by_day.get(day, []) if ss else []
        if not idxs:
            continue
        last = ss.series[idxs[-1]]
        port.close_position(sym, last.start_time,
                            square_off_fill(last.close, slippage_bps), "EOD")

    return port.trades


def simulate(
    symbols:      Dict[str, SymbolSeries],
    nifty:        SymbolSeries,
    from_d:       date,
    to_d:         date,
    slippage_bps: float,
    capital:      float = cfg.ACCOUNT_BALANCE,
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
            lambda d: _simulate_day(d, symbols, nifty, slippage_bps, capital), days
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
    slippage_bps: float, capital: float = cfg.ACCOUNT_BALANCE,
) -> None:
    """Orchestrate one backtest run: fetch → simulate (in a worker thread) → persist."""
    try:
        universe, symbols, nifty = await load_backtest_data(from_d, to_d)
        if not symbols or nifty is None:
            await db.fail_backtest_run(run_id, "No historical data or empty universe")
            return

        trades, equity, days = await asyncio.to_thread(
            simulate, symbols, nifty, from_d, to_d, slippage_bps, capital
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
