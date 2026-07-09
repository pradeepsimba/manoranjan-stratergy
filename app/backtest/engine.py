from __future__ import annotations

"""
Backtest replay engine for the Bank Nifty options strategy.

Steps each trading day 5-minute bar by bar, driving the SAME
evaluate_entry/evaluate_exit functions the live scheduler calls (this repo's
hard convention — live and backtest share one strategy core). Intraday-only:
a fresh Portfolio per day (single active trade), EOD square-off; days run in
parallel since they're independent. c.html's own runBacktest() is a confirmed
empty stub, so there is no reference backtest behavior to preserve fidelity
with — see fills.resolve_index_touch for the one deliberate improvement this
engine makes over a literal (close-only) port of checkExit.

Anti-look-ahead guarantees:
  * An entry decision at bar t only sees bars [.. t]; the option's IV/T are
    computed from that same bar's timestamp and closes [.. t].
  * A position opened at bar t is only eligible to exit on bars > t.
  * SL/target touch resolution uses gap-at-open + intrabar high/low computed
    from bar t alone; ratcheting for the NEXT bar uses bar t's close — never
    a bar the replay hasn't reached yet.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import app.config as cfg
from app.backtest.data import SymbolSeries, load_backtest_data
from app.backtest.fills import (
    resolve_index_touch,
    slip_buy_premium,
    slip_sell_premium,
)
from app.backtest.metrics import compute_metrics
from app.backtest.portfolio import BTPosition, Portfolio
from app.engine.bn_entry_exit import evaluate_entry, evaluate_exit
from app.engine.bn_pricing import black_scholes, estimate_iv, time_to_expiry_years
from app.models import Candle

_LEADER_HISTORY_BARS = 25   # covers both pattern (last 3) and qty-avg (last 20) window


def _slice_recent(ss: SymbolSeries, gidx: int, n: int) -> List[Candle]:
    return ss.series[max(0, gidx - n + 1): gidx + 1]


def _leader_recent_at(stocks: Dict[str, SymbolSeries], day: str, tm: str) -> Dict[str, List[Candle]]:
    out: Dict[str, List[Candle]] = {}
    for name, token in cfg.BN_LEADER_STOCKS.items():
        ss = stocks.get(token)
        idx = ss.at.get(day, {}).get(tm) if ss else None
        out[name] = _slice_recent(ss, idx, _LEADER_HISTORY_BARS) if idx is not None else []
    return out


def _open_position(signal, now: datetime, gidx: int) -> BTPosition:
    """Freeze this trade's risk parameters from cfg AT ENTRY — mirrors
    bn_entry_exit.open_trade_from_signal but returns the backtest's own
    BTPosition dataclass (the live/backtest split every dataclass in this
    repo already has — see Position vs BTPosition in the deleted equity engine)."""
    stoploss_points = cfg.BN_STOPLOSS_POINTS
    if signal.direction == "BUY":
        target = signal.entry_index_price + cfg.BN_TARGET_POINTS
        initial_sl = signal.entry_index_price - stoploss_points
    else:
        target = signal.entry_index_price - cfg.BN_TARGET_POINTS
        initial_sl = signal.entry_index_price + stoploss_points

    return BTPosition(
        direction=signal.direction,
        entry_time=now.isoformat(),
        entry_index_price=signal.entry_index_price,
        entry_gidx=gidx,
        target=target,
        current_sl=initial_sl,
        sl_stage="Initial",
        strike=signal.strike,
        option_type="CE" if signal.direction == "BUY" else "PE",
        expiry=signal.expiry,
        entry_premium=signal.entry_premium,
        stoploss_points=stoploss_points,
        breakeven_trigger=cfg.BN_BREAKEVEN_TRIGGER,
        trail_trigger=cfg.BN_TRAIL_TRIGGER,
        trail_distance=cfg.BN_TRAIL_DISTANCE,
        lot_size=cfg.BN_LOT_SIZE,
        confidence=signal.confidence,
        iv_used=signal.iv_used,
    )


def _try_exit(port: Portfolio, bn_ss: SymbolSeries, gidx: int,
             slippage_bps: float) -> None:
    pos = port.active
    if pos is None or gidx <= pos.entry_gidx:
        return
    bar = bn_ss.series[gidx]
    now = datetime.fromisoformat(bar.start_time)

    touch = resolve_index_touch(pos.direction, pos.current_sl, pos.target, bar)
    if touch is not None:
        exit_index_price, outcome = touch
        lookback = bn_ss.closes[max(0, gidx - cfg.BN_IV_LOOKBACK_BARS):gidx + 1]
        iv = estimate_iv(lookback)
        expiry_dt = datetime.fromisoformat(pos.expiry)
        T = time_to_expiry_years(now, expiry_dt)
        bs = black_scholes(exit_index_price, pos.strike, T, cfg.BN_RISK_FREE_RATE, iv, pos.option_type)
        exit_premium = slip_sell_premium(bs["price"], slippage_bps)
        port.close_position(now, exit_index_price, exit_premium, outcome)
        return

    # No touch this bar — ratchet trailing/breakeven off THIS bar's close for
    # the NEXT bar's check (evaluate_exit's should_exit is ignored here: our
    # own gap/intrabar-aware resolve_index_touch above is authoritative).
    closes_lookback = bn_ss.closes[max(0, gidx - cfg.BN_INDICATOR_LOOKBACK_BARS):gidx + 1]
    ev = evaluate_exit(pos, now, bar.close, closes_lookback)
    pos.current_sl = ev.new_sl
    pos.sl_stage = ev.sl_stage


def _try_entry(port: Portfolio, bn_ss: SymbolSeries, stocks: Dict[str, SymbolSeries],
               gidx: int, day: str, tm: str, slippage_bps: float) -> None:
    if port.active is not None:
        return
    bn_recent = _slice_recent(bn_ss, gidx, max(20, cfg.BN_ATR_PERIOD + 5))
    bn_closes_lookback = bn_ss.closes[max(0, gidx - cfg.BN_INDICATOR_LOOKBACK_BARS):gidx + 1]
    leader_recent = _leader_recent_at(stocks, day, tm)

    now = datetime.fromisoformat(bn_ss.series[gidx].start_time)
    signal, _diag = evaluate_entry(now, bn_recent, bn_closes_lookback,
                                   leader_recent, port.last_exit_time)
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
