from __future__ import annotations

from typing import List, Tuple

import app.config as cfg
from app.engine.indicator_engine import session_vwap_candles
from app.models import Candle, TrendGate


def compute_nifty_gates(
    nifty_ltp:        float,
    nifty_candles_5m: List[Candle],
) -> Tuple[bool, bool]:
    """
    The two index-level gates are identical for every stock in a given bar,
    so compute them ONCE per bar instead of 500× inside check_trend.

    Daily-open is taken from today's first 5m bar (nifty_candles_5m is today's
    session), not a separate 1d fetch that is never updated live.

    Returns (nifty_daily_green, nifty_above_vwap).
    """
    daily_green = bool(
        nifty_candles_5m and nifty_ltp > 0
        and nifty_ltp > nifty_candles_5m[0].open
    )

    above_vwap = False
    if nifty_ltp > 0 and nifty_candles_5m:
        # Single pass over the session bars — this runs every 100ms tick cycle,
        # so no per-call list/array allocations. The NIFTY feed carries
        # volume=0 on every bar, so session_vwap_candles degrades to the
        # session TWAP (mean typical price) — without that fallback this gate
        # could never pass. vwap==0 now only means "no bars yet" → block.
        vwap = session_vwap_candles(nifty_candles_5m)
        above_vwap = vwap > 0.0 and nifty_ltp > vwap

    return daily_green, above_vwap


def check_trend(
    ltp:               float,
    day_open:          float,
    candles_1h:        List[Candle],
    nifty_daily_green: bool,
    nifty_above_vwap:  bool,
) -> TrendGate:
    """
    4-gate multi-timeframe trend filter.

    Pure function. The two NIFTY gates are passed in precomputed (one shared
    computation per bar); only the two per-stock gates are evaluated here.

    `day_open` is today's open, sourced from today's first 5m bar by the caller,
    so the daily gate never depends on a separate (never live-updated) 1d fetch.

    Gates:
      1. Daily Green       — stock LTP > today's open
      2. Hourly Green      — current 1H candle close > open
      3. NIFTY Daily Green — precomputed
      4. NIFTY Above VWAP  — precomputed

    Records the ACTUAL market state of every gate — runtime enable/disable
    toggles are applied in trend_blockers(), NOT here, so the TrendGate saved
    with a position (and the DB daily_green/hourly_green columns) stays an
    honest diagnostic even when a gate is disabled.
    """
    gate = TrendGate()
    gate.nifty_daily_green = nifty_daily_green
    gate.nifty_above_vwap  = nifty_above_vwap

    if day_open > 0 and ltp > 0:
        gate.daily_green = ltp > day_open

    if candles_1h:
        gate.hourly_green = candles_1h[-1].close > candles_1h[-1].open

    return gate


# (gate attr, config toggle, human-readable rejection reason)
_GATE_TOGGLES = (
    ("daily_green",       "GATE_STOCK_DAILY",  "Daily not green"),
    ("hourly_green",      "GATE_STOCK_HOURLY", "Hourly not green"),
    ("nifty_daily_green", "GATE_NIFTY_DAILY",  "NIFTY not daily-green"),
    ("nifty_above_vwap",  "GATE_NIFTY_VWAP",   "NIFTY below VWAP"),
)


def trend_blockers(gate: TrendGate) -> List[str]:
    """
    Rejection reasons among the ENABLED gates that are red; empty list = the
    trend filter passes. The single place gate toggles are applied — shared by
    the live engine and the backtest so they cannot drift.
    """
    return [reason for attr, toggle, reason in _GATE_TOGGLES
            if getattr(cfg, toggle) and not getattr(gate, attr)]
