from __future__ import annotations

from typing import List

import pandas as pd
import pandas_ta as ta

from app.models import Candle, TrendGate


def _nifty_vwap(candles_5m: List[Candle]) -> float:
    """Session VWAP for NIFTY 50 computed via pandas-ta."""
    if not candles_5m:
        return 0.0
    df = pd.DataFrame([{
        "high": c.high, "low": c.low, "close": c.close, "volume": c.volume,
    } for c in candles_5m], dtype=float)
    try:
        vwap_s = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
        v = vwap_s.iloc[-1]
        return float(v) if not pd.isna(v) else 0.0
    except Exception:
        return 0.0


def check_trend(
    ltp:              float,
    candles_1d:       List[Candle],
    candles_1h:       List[Candle],
    nifty_ltp:        float,
    nifty_candles_1d: List[Candle],
    nifty_candles_5m: List[Candle],
) -> TrendGate:
    """
    4-gate multi-timeframe trend filter.

    Pure function — all data is passed in as snapshots; safe to call from any
    thread including ThreadPoolExecutor workers.

    Gates:
      1. Daily Green       — stock LTP > today's daily open
      2. Hourly Green      — current 1H candle close > open
      3. NIFTY Daily Green — NIFTY LTP > NIFTY daily open
      4. NIFTY Above VWAP  — NIFTY LTP > NIFTY session VWAP
    """
    gate = TrendGate()

    if candles_1d and ltp > 0:
        gate.daily_green = ltp > candles_1d[-1].open

    if candles_1h:
        gate.hourly_green = candles_1h[-1].close > candles_1h[-1].open

    if nifty_candles_1d and nifty_ltp > 0:
        gate.nifty_daily_green = nifty_ltp > nifty_candles_1d[-1].open

    nifty_vwap = _nifty_vwap(nifty_candles_5m)
    if nifty_vwap > 0 and nifty_ltp > 0:
        gate.nifty_above_vwap = nifty_ltp > nifty_vwap

    return gate
