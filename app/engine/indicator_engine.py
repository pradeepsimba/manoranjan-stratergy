from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

import app.config as cfg
from app.models import Candle, IndicatorResult


# ── DataFrame helper ──────────────────────────────────────────────────────────

def _to_df(candles: List[Candle]) -> pd.DataFrame:
    """
    Convert candles to a DataFrame using column arrays (one list per column).
    Faster than building 300 row-dicts and letting pandas infer per row.
    """
    return pd.DataFrame({
        "open":   [c.open   for c in candles],
        "high":   [c.high   for c in candles],
        "low":    [c.low    for c in candles],
        "close":  [c.close  for c in candles],
        "volume": [c.volume for c in candles],
    }, dtype=float)


def _val(series: pd.Series, offset: int = -1) -> Optional[float]:
    """Return a scalar from a Series, or None if NaN / empty."""
    try:
        v = series.iloc[offset]
        return None if pd.isna(v) else float(v)
    except (IndexError, TypeError):
        return None


def session_vwap_last(highs, lows, closes, volumes) -> float:
    """
    Last cumulative session VWAP via numpy — much cheaper than pandas-ta's
    ta.vwap (no datetime-index handling, single vectorised pass).

    Accepts numpy arrays or pandas Series. Returns 0.0 on empty / zero volume.
    """
    h = np.asarray(highs,   dtype=float)
    l = np.asarray(lows,    dtype=float)
    c = np.asarray(closes,  dtype=float)
    v = np.asarray(volumes, dtype=float)
    if h.size == 0:
        return 0.0
    cum_vol = v.cumsum()
    total   = cum_vol[-1]
    if total <= 0:
        return 0.0
    tp = (h + l + c) / 3.0
    return float((tp * v).cumsum()[-1] / total)


# ── Swing Low (custom — pandas-ta has no swing-low primitive) ─────────────────

def swing_low(candles: List[Candle], bars: int = cfg.SWING_LOW_BARS) -> float:
    """Lowest low of the last N completed bars (structural support floor)."""
    if not candles:
        return 0.0
    window = candles[-bars:] if len(candles) >= bars else candles
    return min(c.low for c in window)


# ── Bullish candlestick patterns (custom — most reliable to hand-roll) ────────

def _detect_bullish_pattern(
    c: Candle,
    prev: Candle,
    prev2: Optional[Candle] = None,
) -> Optional[str]:
    body  = abs(c.close - c.open)
    rng   = c.high - c.low
    if rng == 0:
        return None
    lower = (c.open - c.low)  if c.is_bullish() else (c.close - c.low)
    upper = (c.high - c.close) if c.is_bullish() else (c.high - c.open)

    # Hammer: small body, long lower shadow, previous bar bearish
    if (c.is_bullish() and prev.is_bearish()
            and lower >= 2 * body and upper <= body * 0.5
            and body / rng < 0.4):
        return "Hammer"

    # Bullish Engulfing
    if (c.is_bullish() and prev.is_bearish()
            and c.open <= prev.close and c.close >= prev.open
            and body > abs(prev.close - prev.open) * 0.9):
        return "Bullish Engulfing"

    # Morning Star (3-bar reversal)
    if (prev2 and prev2.is_bearish()
            and abs(prev.close - prev.open) <= abs(prev2.close - prev2.open) * 0.4
            and c.is_bullish()
            and c.close > (prev2.open + prev2.close) / 2):
        return "Morning Star"

    # Strong bullish close: body > 70% of range and moves > 5 points
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
    Compute all entry-check indicators using pandas-ta.

    Hot path — runs for every watchlist stock on every 5-minute bar close.
    The DataFrame is built once and reused for every indicator; VWAP uses a
    fast numpy pass instead of a second DataFrame.

    candles_5m          — 5-min bars used for RSI, MACD, ADX, volume, patterns
    session_candles_5m  — today's 5-min bars from 09:15 (for VWAP); falls back
                          to candles_5m if not provided
    """
    ind = IndicatorResult()
    if not candles_5m or len(candles_5m) < 3:
        return ind

    df     = _to_df(candles_5m)
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # ── RSI (14) ───────────────────────────────────────────────────────────────
    rsi_s   = ta.rsi(close, length=cfg.RSI_PERIOD)
    ind.rsi = _val(rsi_s)
    if ind.rsi is not None:
        ind.rsi_above_30 = ind.rsi > cfg.RSI_OVERSOLD
        recent_rsi = rsi_s.dropna().tail(cfg.RSI_RISING_BARS + 1).values
        ind.rsi_rising = (
            len(recent_rsi) >= cfg.RSI_RISING_BARS
            and all(recent_rsi[i] > recent_rsi[i - 1]
                    for i in range(1, len(recent_rsi)))
        )

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    macd_df = ta.macd(close)   # cols: MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
    if macd_df is not None and not macd_df.empty:
        ml_col, sig_col, hst_col = "MACD_12_26_9", "MACDs_12_26_9", "MACDh_12_26_9"
        ind.macd_line        = _val(macd_df[ml_col])  or 0.0
        ind.macd_signal_line = _val(macd_df[sig_col]) or 0.0
        ind.macd_histogram   = _val(macd_df[hst_col]) or 0.0

        prev_ml  = _val(macd_df[ml_col],  -2)
        prev_sig = _val(macd_df[sig_col], -2)
        if prev_ml is not None and prev_sig is not None:
            ind.macd_bullish_cross = (prev_ml <= prev_sig
                                      and ind.macd_line > ind.macd_signal_line)

    # ── ADX (14) ──────────────────────────────────────────────────────────────
    adx_df = ta.adx(high, low, close, length=cfg.ADX_PERIOD)
    if adx_df is not None and not adx_df.empty:
        p = cfg.ADX_PERIOD
        ind.adx      = _val(adx_df[f"ADX_{p}"]) or 0.0
        ind.plus_di  = _val(adx_df[f"DMP_{p}"]) or 0.0
        ind.minus_di = _val(adx_df[f"DMN_{p}"]) or 0.0
        ind.adx_ok   = ind.adx > cfg.ADX_THRESHOLD and ind.plus_di > ind.minus_di

    # ── VWAP (session, numpy) ──────────────────────────────────────────────────
    # Reuse the main df arrays when session candles are the same list (the
    # real call path); only build separate arrays if a distinct session set.
    if session_candles_5m is None or session_candles_5m is candles_5m:
        ind.vwap = session_vwap_last(high.values, low.values,
                                     close.values, volume.values)
    else:
        sdf = _to_df(session_candles_5m)
        ind.vwap = session_vwap_last(sdf["high"].values, sdf["low"].values,
                                     sdf["close"].values, sdf["volume"].values)

    ltp = candles_5m[-1].close
    ind.price_above_vwap = ind.vwap > 0 and ltp > ind.vwap

    # ── Volume ────────────────────────────────────────────────────────────────
    vol_prev = volume.iloc[:-1]
    ind.avg_volume_20 = float(
        vol_prev.tail(cfg.VOLUME_MA_PERIOD).mean()
        if len(vol_prev) >= cfg.VOLUME_MA_PERIOD else vol_prev.mean()
    )
    ind.volume_surge = (ind.avg_volume_20 > 0
                        and volume.iloc[-1] > ind.avg_volume_20 * cfg.VOLUME_MULTIPLIER)

    # ── Structural support (swing low, custom) ────────────────────────────────
    ind.support_level = swing_low(candles_5m[:-1], cfg.SWING_LOW_BARS)
    if ind.support_level > 0:
        dist = (ltp - ind.support_level) / ind.support_level
        ind.near_support = 0 <= dist <= cfg.SUPPORT_TOUCH_PCT

    # ── Candlestick pattern (custom) ──────────────────────────────────────────
    c     = candles_5m[-1]
    prev  = candles_5m[-2]
    prev2 = candles_5m[-3] if len(candles_5m) >= 3 else None
    pat   = _detect_bullish_pattern(c, prev, prev2)
    ind.candle_pattern  = pat
    ind.bullish_pattern = pat is not None

    return ind
