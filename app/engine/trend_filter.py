from __future__ import annotations

from typing import List, Tuple

from app.engine.indicator_engine import session_vwap_last
from app.models import Candle, TrendGate


def compute_nifty_gates(
    nifty_ltp:        float,
    nifty_candles_1d: List[Candle],
    nifty_candles_5m: List[Candle],
) -> Tuple[bool, bool]:
    """
    The two index-level gates are identical for every stock in a given bar,
    so compute them ONCE per bar instead of 500× inside check_trend.

    Returns (nifty_daily_green, nifty_above_vwap).
    """
    daily_green = bool(
        nifty_candles_1d and nifty_ltp > 0
        and nifty_ltp > nifty_candles_1d[-1].open
    )

    above_vwap = False
    if nifty_ltp > 0 and nifty_candles_5m:
        vwap = session_vwap_last(
            [c.high for c in nifty_candles_5m],
            [c.low  for c in nifty_candles_5m],
            [c.close for c in nifty_candles_5m],
            [c.volume for c in nifty_candles_5m],
        )
        above_vwap = vwap > 0 and nifty_ltp > vwap

    return daily_green, above_vwap


def check_trend(
    ltp:               float,
    candles_1d:        List[Candle],
    candles_1h:        List[Candle],
    nifty_daily_green: bool,
    nifty_above_vwap:  bool,
) -> TrendGate:
    """
    4-gate multi-timeframe trend filter.

    Pure function. The two NIFTY gates are passed in precomputed (one shared
    computation per bar); only the two per-stock gates are evaluated here.

    Gates:
      1. Daily Green       — stock LTP > today's daily open
      2. Hourly Green      — current 1H candle close > open
      3. NIFTY Daily Green — precomputed
      4. NIFTY Above VWAP  — precomputed
    """
    gate = TrendGate()
    gate.nifty_daily_green = nifty_daily_green
    gate.nifty_above_vwap  = nifty_above_vwap

    if candles_1d and ltp > 0:
        gate.daily_green = ltp > candles_1d[-1].open

    if candles_1h:
        gate.hourly_green = candles_1h[-1].close > candles_1h[-1].open

    return gate
