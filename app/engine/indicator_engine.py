from __future__ import annotations

from typing import List, Optional

import pandas as pd
import pandas_ta as ta

import app.config as cfg
from app.models import Candle, IndicatorResult


# ── DataFrame helper ──────────────────────────────────────────────────────────

def _to_df(candles: List[Candle]) -> pd.DataFrame:
    """Convert a list of Candle objects to a pandas DataFrame."""
    return pd.DataFrame([{
        "open":   c.open,
        "high":   c.high,
        "low":    c.low,
        "close":  c.close,
        "volume": c.volume,
    } for c in candles], dtype=float)


def _val(series: pd.Series, offset: int = -1) -> Optional[float]:
    """Return a scalar from a Series, or None if NaN / empty."""
    try:
        v = series.iloc[offset]
        return None if pd.isna(v) else float(v)
    except (IndexError, TypeError):
        return None


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

    candles_5m          — 5-min bars used for RSI, MACD, ADX, volume, patterns
    session_candles_5m  — today's 5-min bars from 09:15 (for VWAP); falls back
                          to candles_5m if not provided
    """
    ind = IndicatorResult()
    if not candles_5m or len(candles_5m) < 3:
        return ind

    df = _to_df(candles_5m)

    # ── RSI (14) ───────────────────────────────────────────────────────────────
    rsi_s        = ta.rsi(df["close"], length=cfg.RSI_PERIOD)
    ind.rsi      = _val(rsi_s)
    if ind.rsi is not None:
        ind.rsi_above_30 = ind.rsi > cfg.RSI_OVERSOLD
        # Rising: last 3 valid values each greater than the previous
        recent_rsi = rsi_s.dropna().tail(cfg.RSI_RISING_BARS + 1).values
        ind.rsi_rising = (
            len(recent_rsi) >= cfg.RSI_RISING_BARS
            and all(recent_rsi[i] > recent_rsi[i - 1]
                    for i in range(1, len(recent_rsi)))
        )

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    macd_df = ta.macd(df["close"])   # columns: MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
    if macd_df is not None and not macd_df.empty:
        ml_col  = "MACD_12_26_9"
        sig_col = "MACDs_12_26_9"
        hst_col = "MACDh_12_26_9"
        ind.macd_line        = _val(macd_df[ml_col])  or 0.0
        ind.macd_signal_line = _val(macd_df[sig_col]) or 0.0
        ind.macd_histogram   = _val(macd_df[hst_col]) or 0.0

        # Bullish crossover: previous bar below/equal signal, current bar above
        prev_ml  = _val(macd_df[ml_col],  -2)
        prev_sig = _val(macd_df[sig_col], -2)
        if (prev_ml is not None and prev_sig is not None
                and ind.macd_line is not None and ind.macd_signal_line is not None):
            ind.macd_bullish_cross = (prev_ml <= prev_sig
                                      and ind.macd_line > ind.macd_signal_line)

    # ── ADX (14) ──────────────────────────────────────────────────────────────
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=cfg.ADX_PERIOD)
    # columns: ADX_14, DMP_14 (+DI), DMN_14 (-DI)
    if adx_df is not None and not adx_df.empty:
        adx_col = f"ADX_{cfg.ADX_PERIOD}"
        dmp_col = f"DMP_{cfg.ADX_PERIOD}"
        dmn_col = f"DMN_{cfg.ADX_PERIOD}"
        ind.adx      = _val(adx_df[adx_col]) or 0.0
        ind.plus_di  = _val(adx_df[dmp_col]) or 0.0
        ind.minus_di = _val(adx_df[dmn_col]) or 0.0
        ind.adx_ok   = ind.adx > cfg.ADX_THRESHOLD and ind.plus_di > ind.minus_di

    # ── VWAP (session) ────────────────────────────────────────────────────────
    # Pass session candles (today from 09:15); cumulative VWAP is correct
    # because the array starts at session open.
    vwap_candles = session_candles_5m if session_candles_5m else candles_5m
    if vwap_candles:
        vdf    = _to_df(vwap_candles)
        vwap_s = ta.vwap(vdf["high"], vdf["low"], vdf["close"], vdf["volume"])
        ind.vwap = _val(vwap_s) or 0.0
    ltp = candles_5m[-1].close
    ind.price_above_vwap = ind.vwap > 0 and ltp > ind.vwap

    # ── Volume ────────────────────────────────────────────────────────────────
    # Average of the previous N bars (exclude the current bar)
    vol_series         = df["volume"].iloc[:-1]
    ind.avg_volume_20  = float(vol_series.tail(cfg.VOLUME_MA_PERIOD).mean()
                               if len(vol_series) >= cfg.VOLUME_MA_PERIOD else vol_series.mean())
    ind.volume_surge   = (ind.avg_volume_20 > 0
                          and df["volume"].iloc[-1] > ind.avg_volume_20 * cfg.VOLUME_MULTIPLIER)

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

    # ── EMA (20 / 50) — informational ─────────────────────────────────────────
    if len(df) >= 20:
        ind.ema20 = round(float(ta.ema(df["close"], length=20).iloc[-1]), 2)
    if len(df) >= 50:
        ind.ema50 = round(float(ta.ema(df["close"], length=50).iloc[-1]), 2)

    return ind
