from __future__ import annotations

"""
Indicator engine — raw TA-Lib over NumPy memory arrays.

Hot path: runs inside the ThreadPoolExecutor(16) for every watchlist token on
every 5-minute bar close. Design rules for thread/GIL efficiency:

  * No pandas DataFrames and no `.ta` method chaining cross the thread boundary.
    Each worker builds plain float64 NumPy arrays from the candle snapshot and
    feeds them straight to TA-Lib's C functions, which release the GIL during
    computation — so the 16 workers do real parallel math.
  * Only the minimum tail slice needed for indicator lookback is materialised
    (TALIB_LOOKBACK bars), not the full multi-day array. Session VWAP is the one
    exception: it must see every bar since 09:15, so it uses the session array.
"""

from typing import List, Optional

import numpy as np
import talib

import app.config as cfg
from app.models import Candle, IndicatorResult

_LOOKBACK = cfg.TALIB_LOOKBACK


# ── Array helpers ─────────────────────────────────────────────────────────────

def _f64(values) -> np.ndarray:
    """Contiguous float64 array — the layout TA-Lib's C layer requires."""
    return np.ascontiguousarray(values, dtype=np.float64)


def _last(arr: Optional[np.ndarray], offset: int = -1) -> Optional[float]:
    """Final (or offset) value of a TA-Lib output array, or None if NaN/empty."""
    if arr is None or arr.size == 0:
        return None
    try:
        v = arr[offset]
    except IndexError:
        return None
    return None if np.isnan(v) else float(v)


# ── Swing Low (structural support) ────────────────────────────────────────────

def swing_low(candles: List[Candle], bars: int = cfg.SWING_LOW_BARS) -> float:
    if not candles:
        return 0.0
    window = candles[-bars:] if len(candles) >= bars else candles
    return min(c.low for c in window)


# ── Session VWAP (numpy; TA-Lib has no session-anchored VWAP) ─────────────────

def session_vwap_last(highs, lows, closes, volumes) -> float:
    h = _f64(highs); l = _f64(lows); c = _f64(closes); v = _f64(volumes)
    if h.size == 0:
        return 0.0
    cum_vol = v.cumsum()
    total   = cum_vol[-1]
    if total <= 0:
        return 0.0
    tp = (h + l + c) / 3.0
    return float((tp * v).cumsum()[-1] / total)


# ── Bullish candlestick patterns (custom) ─────────────────────────────────────

def _detect_bullish_pattern(
    c: Candle,
    prev: Candle,
    prev2: Optional[Candle] = None,
) -> Optional[str]:
    body = abs(c.close - c.open)
    rng  = c.high - c.low
    if rng == 0:
        return None
    lower = (c.open - c.low)  if c.is_bullish() else (c.close - c.low)
    upper = (c.high - c.close) if c.is_bullish() else (c.high - c.open)

    if (c.is_bullish() and prev.is_bearish()
            and lower >= 2 * body and upper <= body * 0.5
            and body / rng < 0.4):
        return "Hammer"

    if (c.is_bullish() and prev.is_bearish()
            and c.open <= prev.close and c.close >= prev.open
            and body > abs(prev.close - prev.open) * 0.9):
        return "Bullish Engulfing"

    if (prev2 and prev2.is_bearish()
            and abs(prev.close - prev.open) <= abs(prev2.close - prev2.open) * 0.4
            and c.is_bullish()
            and c.close > (prev2.open + prev2.close) / 2):
        return "Morning Star"

    if c.is_bullish() and body / rng > 0.7 and body > 5:
        return "Strong Bull Close"

    return None


# ── Master indicator function ─────────────────────────────────────────────────

def compute_indicators(
    candles_5m: List[Candle],
    candles_1h: Optional[List[Candle]] = None,
    session_candles_5m: Optional[List[Candle]] = None,
) -> IndicatorResult:
    """
    Compute all entry-check indicators with TA-Lib.

    candles_5m          — 5-min bars (RSI/ADX/MACD/volume/pattern)
    session_candles_5m  — today's bars from 09:15 for VWAP; falls back to
                          candles_5m if not provided
    """
    ind = IndicatorResult()
    if not candles_5m or len(candles_5m) < 3:
        return ind

    # ── Slice isolation: only the tail needed for lookback enters the C calls.
    # TALIB_LOOKBACK bars is enough for RSI(14)/ADX(14)/MACD(26,9) to fully
    # converge while skipping the multi-day warmup history.
    window = candles_5m[-_LOOKBACK:] if len(candles_5m) > _LOOKBACK else candles_5m

    close  = _f64([c.close  for c in window])
    high   = _f64([c.high   for c in window])
    low    = _f64([c.low    for c in window])
    volume = _f64([c.volume for c in window])

    ltp = float(close[-1])

    # ── RSI (14) ────────────────────────────────────────────────────────────
    rsi_arr = talib.RSI(close, timeperiod=cfg.RSI_PERIOD)
    ind.rsi = _last(rsi_arr)
    if ind.rsi is not None:
        ind.rsi_above_30 = ind.rsi > cfg.RSI_OVERSOLD
        tail = rsi_arr[~np.isnan(rsi_arr)][-(cfg.RSI_RISING_BARS + 1):]
        ind.rsi_rising = (
            tail.size >= cfg.RSI_RISING_BARS
            and bool(np.all(np.diff(tail) > 0))
        )

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    macd, macdsignal, _ = talib.MACD(
        close, fastperiod=12, slowperiod=26, signalperiod=9
    )
    ind.macd_line        = _last(macd)       or 0.0
    ind.macd_signal_line = _last(macdsignal) or 0.0
    prev_ml  = _last(macd,       -2)
    prev_sig = _last(macdsignal, -2)
    if (prev_ml is not None and prev_sig is not None
            and _last(macd) is not None and _last(macdsignal) is not None):
        ind.macd_histogram     = ind.macd_line - ind.macd_signal_line
        ind.macd_bullish_cross = (prev_ml <= prev_sig
                                  and ind.macd_line > ind.macd_signal_line)

    # ── ADX (14) + directional movement ──────────────────────────────────────
    adx_arr = talib.ADX(high, low, close, timeperiod=cfg.ADX_PERIOD)
    plus_di_arr  = talib.PLUS_DI(high, low, close, timeperiod=cfg.ADX_PERIOD)
    minus_di_arr = talib.MINUS_DI(high, low, close, timeperiod=cfg.ADX_PERIOD)
    ind.adx      = _last(adx_arr)      or 0.0
    ind.plus_di  = _last(plus_di_arr)  or 0.0
    ind.minus_di = _last(minus_di_arr) or 0.0
    ind.adx_ok   = ind.adx > cfg.ADX_THRESHOLD and ind.plus_di > ind.minus_di

    # ── Session VWAP (full session array, not the lookback slice) ─────────────
    sess = session_candles_5m if session_candles_5m else candles_5m
    ind.vwap = session_vwap_last(
        [c.high for c in sess], [c.low for c in sess],
        [c.close for c in sess], [c.volume for c in sess],
    )
    ind.price_above_vwap = ind.vwap > 0 and ltp > ind.vwap

    # ── Volume surge ──────────────────────────────────────────────────────────
    prev_vol = volume[:-1]
    if prev_vol.size:
        avg = (prev_vol[-cfg.VOLUME_MA_PERIOD:].mean()
               if prev_vol.size >= cfg.VOLUME_MA_PERIOD else prev_vol.mean())
        ind.avg_volume_20 = float(avg)
        ind.volume_surge  = (ind.avg_volume_20 > 0
                             and float(volume[-1]) > ind.avg_volume_20 * cfg.VOLUME_MULTIPLIER)

    # ── Structural support (swing low) ────────────────────────────────────────
    ind.support_level = swing_low(candles_5m[:-1], cfg.SWING_LOW_BARS)
    if ind.support_level > 0:
        dist = (ltp - ind.support_level) / ind.support_level
        ind.near_support = 0 <= dist <= cfg.SUPPORT_TOUCH_PCT

    # ── Candlestick pattern ───────────────────────────────────────────────────
    c     = candles_5m[-1]
    prev  = candles_5m[-2]
    prev2 = candles_5m[-3] if len(candles_5m) >= 3 else None
    pat   = _detect_bullish_pattern(c, prev, prev2)
    ind.candle_pattern  = pat
    ind.bullish_pattern = pat is not None

    return ind
