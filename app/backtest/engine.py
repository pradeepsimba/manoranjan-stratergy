from __future__ import annotations

"""
Backtest replay engine for the Bank Nifty scalp strategy.

Steps each trading day 5-minute bar by bar, driving the SAME evaluate_entry
function the live scheduler calls (this repo's hard convention — live and
backtest share one strategy core for the ENTRY decision). c.html's own
runBacktest() is a confirmed empty stub, so there is no reference backtest
behavior to preserve fidelity with.

*** BACKTEST FIDELITY LIMITATION (2026-09-21 scalp-strategy rewrite) ***
The live strategy's exit lifecycle is a hard 12-SECOND window
(BN_SCALP_TIME_STOP_S) — this repo has no historical market data anywhere
at sub-5-minute granularity (only 5m OHLC bars, see CLAUDE.md's "Options
pricing"/"Self-recorded BankNifty history" notes), so a 12-second lifecycle
cannot be faithfully replayed here. Rather than fabricate a falsely-precise
simulation, _try_exit below resolves each position using the entry bar's
IMMEDIATE NEXT bar's OHLC-implied premium range (a coarse proxy for "did
target/stop get touched sometime in the ~5 minutes after entry" — see
fills.resolve_premium_touch's own docstring) and forces a TIME_SCRATCH at
that bar's close if neither was touched, since 12 seconds has by then long
since elapsed relative to a 5-minute bar regardless. This still exercises
the real evaluate_entry signal-quality/frequency logic end-to-end, but
treat any backtest ₹ P&L or win-rate number from this specific strategy as
a rough proxy, not a faithful simulation of the real sub-bar lifecycle —
that would need 1-minute-or-finer historical data this repo doesn't have.

Anti-look-ahead guarantees (still fully intact for the entry decision):
  * An entry decision at bar t only sees bars [.. t]; the option's IV/T are
    computed from that same bar's timestamp and closes [.. t].
  * A position opened at bar t is only eligible to exit on bars > t.
"""

import bisect
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import app.config as cfg
from app.backtest.data import SymbolSeries, load_backtest_data
from app.backtest.fills import (
    resolve_premium_touch,
    slip_buy_premium,
    slip_sell_premium,
)
from app.backtest.metrics import compute_metrics
from app.backtest.portfolio import BTPosition, Portfolio
from app.engine.bn_entry_exit import evaluate_entry
from app.engine.bn_pricing import black_scholes, estimate_iv, time_to_expiry_years
from app.models import Candle


def _slice_recent(ss: SymbolSeries, gidx: int, n: int) -> List[Candle]:
    return ss.series[max(0, gidx - n + 1): gidx + 1]


def _basket_recent_at(stocks: Dict[str, SymbolSeries], day: str, tm: str) -> Dict[str, List[Candle]]:
    """
    2026-09-21: builds the TOKEN-keyed candle dict evaluate_entry now wants
    (cfg.BN_SCALP_BASKET's 8 tokens only, not the 14-stock BN_ALL_STOCKS
    universe the old leader-vote rule needed) — untrimmed (full available
    history up to gidx) since session_vwap needs every bar from the day's
    open, same as the live scheduler's own basket_candles build.
    """
    out: Dict[str, List[Candle]] = {}
    for token in cfg.BN_SCALP_BASKET:
        ss = stocks.get(token)
        idx = ss.at.get(day, {}).get(tm) if ss else None
        if ss and idx is None:
            # Fall back to the latest available bar AT OR BEFORE tm for this
            # token on this day (found in review, 2026-09-23). Without this,
            # a single missing bar for one BN_SCALP_BASKET leg — a real,
            # documented vendor-gap class per CLAUDE.md's Kotak Bank/South
            # Indian Bank naming notes — zeroed this leg's ENTIRE day's VWAP
            # history for every subsequent bar that day (idx stayed None
            # forever after the gap, since the exact-timestamp lookup keeps
            # missing), a backtest-only divergence from live: the scheduler's
            # own basket_candles build (scheduler.py) just reads whatever has
            # accumulated in st.candles_5m, with no exact-timestamp lookup to
            # fail in the first place. by_day[day] is chronological, so this
            # never looks past tm (no look-ahead).
            day_idxs = ss.by_day.get(day, [])
            times = [ss.series[i].start_time[11:16] for i in day_idxs]
            pos = bisect.bisect_right(times, tm) - 1
            if pos >= 0:
                idx = day_idxs[pos]
        out[token] = ss.series[:idx + 1] if (ss and idx is not None) else []
    return out


def _open_position(signal, now: datetime, gidx: int) -> BTPosition:
    """Freeze this trade's risk parameters from cfg AT ENTRY — mirrors
    bn_entry_exit.open_trade_from_signal but returns the backtest's own
    BTPosition dataclass (the live/backtest split every dataclass in this
    repo already has — see Position vs BTPosition in the deleted equity engine)."""
    target_rs = cfg.BN_SCALP_TARGET_RS
    stop_rs = cfg.BN_SCALP_STOP_RS
    time_stop_s = cfg.BN_SCALP_TIME_STOP_S
    scratch_slippage_rs = cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS

    return BTPosition(
        direction=signal.direction,
        entry_time=now.isoformat(),
        entry_index_price=signal.entry_index_price,
        entry_gidx=gidx,
        target=signal.entry_premium + target_rs,
        current_sl=signal.entry_premium - stop_rs,
        sl_stage="Initial",
        strike=signal.strike,
        option_type="CE" if signal.direction == "BUY" else "PE",
        expiry=signal.expiry,
        entry_premium=signal.entry_premium,
        target_rs=target_rs, stop_rs=stop_rs, time_stop_s=time_stop_s,
        scratch_slippage_rs=scratch_slippage_rs,
        basket_score_at_entry=signal.basket_score,
        wobi_at_entry=signal.wobi,
        lot_size=cfg.BN_LOT_SIZE,
        confidence=signal.confidence,
        iv_used=signal.iv_used,
    )


def _try_exit(port: Portfolio, bn_ss: SymbolSeries, gidx: int,
             slippage_bps: float) -> None:
    """See this module's docstring for the backtest-fidelity limitation
    this approximates around (a 12s live lifecycle vs 5m historical bars)."""
    pos = port.active
    if pos is None or gidx <= pos.entry_gidx:
        return
    bar = bn_ss.series[gidx]
    now = datetime.fromisoformat(bar.start_time)

    # Look-ahead fix (2026-09-23, found in review): the touch check below can
    # resolve at bar gidx's OPEN — the earliest instant of that bar, before
    # its own close is known — so the lookback here must exclude gidx's own
    # close (unlike the entry-side/EOD lookbacks elsewhere in this module,
    # which correctly include their own bar since they price AT that bar's
    # close). Mirrors the live engine's closed_tail_closes fix for the same
    # class of bug (forming-bar IV leak).
    lookback = bn_ss.closes[max(0, gidx - cfg.BN_IV_LOOKBACK_BARS):gidx]
    iv = estimate_iv(lookback)
    expiry_dt = datetime.fromisoformat(pos.expiry)
    T = time_to_expiry_years(now, expiry_dt)

    def _premium(index_price: float) -> float:
        return black_scholes(index_price, pos.strike, T, cfg.BN_RISK_FREE_RATE, iv, pos.option_type)["price"]

    p_open = _premium(bar.open)
    p_a, p_b = _premium(bar.high), _premium(bar.low)
    p_hi, p_lo = max(p_a, p_b), min(p_a, p_b)   # CE rises with index, PE falls — max/min sidesteps the branch

    touch = resolve_premium_touch(pos.current_sl, pos.target, p_open, p_hi, p_lo)
    if touch is not None:
        exit_premium_raw, outcome = touch
        exit_premium = slip_sell_premium(exit_premium_raw, slippage_bps)
        port.close_position(now, bar.close, exit_premium, outcome)
        return

    # Neither touched within this bar's premium range — force the scratch
    # exit at this bar's own close (see the module docstring).
    exit_premium = slip_sell_premium(max(0.0, _premium(bar.close) - cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS), slippage_bps)
    port.close_position(now, bar.close, exit_premium, "TIME_SCRATCH")


def _try_entry(port: Portfolio, bn_ss: SymbolSeries, stocks: Dict[str, SymbolSeries],
               gidx: int, day: str, tm: str, slippage_bps: float) -> None:
    if port.active is not None:
        return
    bn_recent = _slice_recent(bn_ss, gidx, 5)   # only bn_recent[-1] is read now — see scheduler.py's mirror comment
    bn_closes_lookback = bn_ss.closes[max(0, gidx - cfg.BN_INDICATOR_LOOKBACK_BARS):gidx + 1]
    basket_recent = _basket_recent_at(stocks, day, tm)

    now = datetime.fromisoformat(bn_ss.series[gidx].start_time)
    # port.active is always None here (the function already returned above
    # otherwise), so trades_today is just the closed-trade count — no
    # in-flight trade to add (found in review: a stale ternary here used to
    # imply otherwise).
    trades_today = len(port.trades)
    signal, _diag = evaluate_entry(now, bn_recent, bn_closes_lookback,
                                   basket_recent, port.last_exit_time, trades_today)
    if signal is None:
        return
    signal.entry_premium = slip_buy_premium(signal.entry_premium, slippage_bps)
    port.open_position(_open_position(signal, now, gidx))


def _simulate_day(day: str, bn_ss: SymbolSeries, stocks: Dict[str, SymbolSeries],
                  slippage_bps: float, overrides: Optional[Dict] = None) -> List:
    """
    Simulate ONE trading day with its own fresh portfolio — intraday mode,
    EOD square-off, days independent (lets the caller run them in parallel).
    """
    with cfg.thread_overrides(overrides or {}):
        return _simulate_day_impl(day, bn_ss, stocks, slippage_bps)


def _simulate_day_impl(day: str, bn_ss: SymbolSeries, stocks: Dict[str, SymbolSeries],
                       slippage_bps: float) -> List:
    scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
    cutoff     = f"{cfg.CUTOFF_HOUR:02d}:{cfg.CUTOFF_MIN:02d}"

    port = Portfolio()
    grid = sorted(bn_ss.at.get(day, {}).items())   # [(time, gidx), ...]
    for tm, gidx in grid:
        _try_exit(port, bn_ss, gidx, slippage_bps)
        if scan_start <= tm < cutoff:
            _try_entry(port, bn_ss, stocks, gidx, day, tm, slippage_bps)

    # EOD square-off any survivor at the day's last bar close.
    if port.active is not None and grid:
        last_gidx = grid[-1][1]
        last_bar = bn_ss.series[last_gidx]
        now = datetime.fromisoformat(last_bar.start_time)
        lookback = bn_ss.closes[max(0, last_gidx - cfg.BN_IV_LOOKBACK_BARS):last_gidx + 1]
        iv = estimate_iv(lookback)
        expiry_dt = datetime.fromisoformat(port.active.expiry)
        T = time_to_expiry_years(now, expiry_dt)
        bs = black_scholes(last_bar.close, port.active.strike, T, cfg.BN_RISK_FREE_RATE,
                           iv, port.active.option_type)
        exit_premium = slip_sell_premium(bs["price"], slippage_bps)
        port.close_position(now, last_bar.close, exit_premium, "EOD")

    return port.trades


def simulate(bn_index: SymbolSeries, stocks: Dict[str, SymbolSeries],
            from_d: date, to_d: date, slippage_bps: float,
            overrides: Optional[Dict] = None) -> Tuple[List, List, int]:
    """
    Run the full replay. Days are independent (intraday, EOD square-off), so
    they execute in parallel across a thread pool.
    """
    lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
    days = sorted(d for d in bn_index.by_day if lo_s <= d <= hi_s)
    if not days:
        return [], [], 0

    workers = max(1, min(cfg.SCAN_WORKERS, len(days)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bt-day") as pool:
        # map preserves input order → results already in chronological day order
        per_day = list(pool.map(
            lambda d: _simulate_day(d, bn_index, stocks, slippage_bps, overrides),
            days,
        ))
    trades: List = []
    for day_trades in per_day:
        trades.extend(day_trades)

    cum = 0.0
    equity_curve: List = []
    for t in trades:
        cum += t.net_pnl
        equity_curve.append((t.exit_time, round(cum, 2)))

    return trades, equity_curve, len(days)


async def run_backtest(
    db, run_id: str, from_d: date, to_d: date,
    slippage_bps: float, overrides: Optional[Dict] = None,
) -> None:
    """Orchestrate one backtest run: fetch → simulate (in a worker thread) → persist."""
    import asyncio
    try:
        overrides = overrides or {}
        warmup   = int(overrides.get("BACKTEST_WARMUP_DAYS", cfg.BACKTEST_WARMUP_DAYS))
        lookback = int(overrides.get("BN_INDICATOR_LOOKBACK_BARS", cfg.BN_INDICATOR_LOOKBACK_BARS))
        bn_index, stocks = await load_backtest_data(
            db, from_d, to_d, warmup_days=warmup, lookback=lookback)
        if bn_index is None:
            await db.fail_backtest_run(
                run_id, f"No self-recorded BankNifty history for {from_d} → {to_d} yet "
                        f"(the archive grows by one day at a time as the live engine runs — "
                        f"see app.services.database.bn_index_bars). Try a range that "
                        f"includes a day the engine has already completed.")
            return

        trades, equity, days = await asyncio.to_thread(
            simulate, bn_index, stocks, from_d, to_d, slippage_bps, overrides
        )

        summary = compute_metrics(trades, equity, days)
        summary["stocks_loaded"] = len(stocks)

        await db.save_backtest_trades(run_id, trades)
        await db.finish_backtest_run(run_id, summary)
        print(f"Backtest {run_id} done: {summary['total_trades']} trades, "
              f"net ₹{summary['net_pnl']:+.2f}")
    except Exception as e:
        await db.fail_backtest_run(run_id, f"{type(e).__name__}: {e}")
        print(f"Backtest {run_id} failed: {e}")
