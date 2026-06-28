from __future__ import annotations

from typing import List, Optional

import pandas as pd
import pandas_ta as ta

from app.models import Candle, TrendGate
from app.state import get_state


def _today_open(candles_1d: List[Candle]) -> Optional[float]:
    if not candles_1d:
        return None
    return candles_1d[-1].open


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


def check_trend(symbol: str, token: str) -> TrendGate:
    """
    4-gate multi-timeframe trend filter.

    Gates:
      1. Daily Green       — stock LTP > today's daily open
      2. Hourly Green      — current 1H candle close > open
      3. NIFTY Daily Green — NIFTY LTP > NIFTY daily open
      4. NIFTY Above VWAP  — NIFTY LTP > NIFTY session VWAP

    Candle stores are keyed by token; LTP store is keyed by symbol name.
    """
    st   = get_state()
    gate = TrendGate()

    ltp = st.ltp.get(symbol, 0.0)

    # Gate 1: Daily candle green
    day_open = _today_open(st.candles_1d.get(token, []))
    if day_open and ltp > 0:
        gate.daily_green = ltp > day_open

    # Gate 2: Hourly candle green
    hourly = st.candles_1h.get(token, [])
    if hourly:
        gate.hourly_green = hourly[-1].close > hourly[-1].open

    # Gate 3: NIFTY daily green
    nifty_ltp      = st.nifty_ltp
    nifty_day_open = _today_open(st.nifty_candles_1d)
    if nifty_day_open and nifty_ltp > 0:
        gate.nifty_daily_green = nifty_ltp > nifty_day_open

    # Gate 4: NIFTY above session VWAP
    nifty_vwap = _nifty_vwap(st.nifty_candles_5m)
    if nifty_vwap > 0 and nifty_ltp > 0:
        gate.nifty_above_vwap = nifty_ltp > nifty_vwap

    return gate
