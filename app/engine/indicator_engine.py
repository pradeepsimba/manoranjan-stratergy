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
from app.engine.conditions import cheap_gates_veto
from app.models import Candle, IndicatorResult


# ── Array helpers ─────────────────────────────────────────────────────────────

def _last(arr: Optional[np.ndarray], offset: int = -1) -> Optional[float]:
    """Final (or offset) value of a TA-Lib output array, or None if NaN/empty."""
    if arr is None or arr.size == 0:
        return None
    try:
        v = arr[offset]
    except IndexError:
        return None
    return None if np.isnan(v) else float(v)


# ── Session VWAP (TA-Lib has no session-anchored VWAP) ───────────────────────

def session_vwap_candles(candles: List[Candle]) -> float:
    """
    Session-anchored VWAP over the given bars in a single pass — only the final
    value is needed, so no cumulative arrays are materialised. Shared by the
    per-stock scan and the NIFTY gate so both use the exact same formula.
    """
    tot_v  = 0.0
    tot_pv = 0.0
    for c in candles:
        v = c.volume
        tot_v  += v
        tot_pv += (c.high + c.low + c.close) * v
    # typical price = (H+L+C)/3, factored out of the loop
    return tot_pv / (3.0 * tot_v) if tot_v > 0 else 0.0


def session_vwap_from_cumsums(cum_pv, cum_v, start: int, end: int) -> float:
    """
    O(1) session VWAP over bars [start..end] from prefix sums of (H+L+C)·V and
    V (see SymbolSeries.index_days). Algebraically identical to
    session_vwap_candles over the same slice — the backtest's fast path.
    """
    base_pv = cum_pv[start - 1] if start > 0 else 0.0
    base_v  = cum_v[start - 1]  if start > 0 else 0.0
    pv = cum_pv[end] - base_pv
    v  = cum_v[end]  - base_v
    return float(pv / (3.0 * v)) if v > 0 else 0.0


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

    # "Strong Bull Close" — a decisive green bar (body ≥ 70% of range) whose
    # move is significant relative to PRICE. The significance floor is price-
    # relative (0.1% of close) rather than an absolute ₹ amount: an absolute
    # `body > 5` was ₹-biased — trivially true for ₹5000 stocks and effectively
    # unreachable for ₹90 ones, across NSE's wide price range. Shared by live
    # and backtest, so this changes signal generation identically for both.
    if c.is_bullish() and body / rng > 0.7 and body > c.close * 0.001:
        return "Strong Bull Close"

    return None


# ── Master indicator function ─────────────────────────────────────────────────

def compute_indicators(
    candles_5m: List[Candle],
    session_candles_5m: Optional[List[Candle]] = None,
    *,
    ohlcv_window: Optional[tuple] = None,
    session_vwap: Optional[float] = None,
    entry_short_circuit: bool = False,
) -> IndicatorResult:
    """
    Compute all entry-check indicators with TA-Lib.

    candles_5m          — 5-min bars (RSI/ADX/MACD/volume/pattern)
    session_candles_5m  — today's bars from 09:15 for VWAP; falls back to
                          candles_5m if not provided (ignored when session_vwap
                          is given)
    ohlcv_window        — optional precomputed (close, high, low, volume)
                          float64 arrays ending on the same bar as candles_5m.
                          Skips the per-call array build — the backtest passes
                          zero-copy views of SymbolSeries arrays here. When
                          given, candles_5m is consulted ONLY for the 3-bar
                          pattern check, so passing just the final 3 bars is
                          sufficient (and what the backtest does).
    session_vwap        — optional precomputed session VWAP (the backtest's
                          O(1) prefix-sum path).
    entry_short_circuit — evaluate the cheap entry gates (support, pattern,
                          VWAP, volume) first and return early when one fails,
                          skipping the TA-Lib calls. The condition logic is
                          identical either way — only evaluation is lazier —
                          so live (False: full snapshot for the dashboard) and
                          backtest (True) stay in parity.
    """
    ind = IndicatorResult()
    if not candles_5m or len(candles_5m) < 3:
        return ind

    # ── Slice isolation: only the tail needed for lookback enters the C calls.
    # TALIB_LOOKBACK bars is enough for RSI(14)/ADX(14)/MACD(26,9) to fully
    # converge while skipping the multi-day warmup history.
    lookback = cfg.TALIB_LOOKBACK   # dynamic setting — read per call
    if ohlcv_window is not None:
        close, high, low, volume = ohlcv_window
        if close.size > lookback:   # same defensive window as the list path
            close  = close[-lookback:]
            high   = high[-lookback:]
            low    = low[-lookback:]
            volume = volume[-lookback:]
    else:
        window = candles_5m[-lookback:] if len(candles_5m) > lookback else candles_5m
        # np.fromiter builds each contiguous float64 array in one C-level pass —
        # no intermediate tuple list and no non-contiguous column copies.
        n      = len(window)
        close  = np.fromiter((c.close  for c in window), np.float64, n)
        high   = np.fromiter((c.high   for c in window), np.float64, n)
        low    = np.fromiter((c.low    for c in window), np.float64, n)
        volume = np.fromiter((c.volume for c in window), np.float64, n)

    ltp = float(close[-1])

    # ── Cheap gates first (support / pattern / VWAP / volume) ─────────────────
    # Ordered before the TA-Lib block so entry_short_circuit can reject most
    # bars without paying for RSI/MACD/ADX. Values are identical to the old
    # bottom-of-function placement — computation order doesn't affect them.

    # Structural support: lowest low of the last SWING_LOW_BARS bars excluding
    # the current one — taken from the already-built `low` array.
    sl_win = low[-(cfg.SWING_LOW_BARS + 1):-1]
    ind.support_level = float(sl_win.min()) if sl_win.size else 0.0
    if ind.support_level > 0:
        dist = (ltp - ind.support_level) / ind.support_level
        ind.near_support = 0 <= dist <= cfg.SUPPORT_TOUCH_PCT

    # Candlestick pattern
    pat = _detect_bullish_pattern(
        candles_5m[-1], candles_5m[-2],
        candles_5m[-3] if len(candles_5m) >= 3 else None,
    )
    ind.candle_pattern  = pat
    ind.bullish_pattern = pat is not None

    # Session VWAP (full session bars, not the lookback slice)
    if session_vwap is not None:
        ind.vwap = session_vwap
    else:
        sess = session_candles_5m if session_candles_5m else candles_5m
        if sess:
            ind.vwap = session_vwap_candles(sess)
    ind.price_above_vwap = ind.vwap > 0 and ltp > ind.vwap

    # Volume surge
    prev_vol = volume[:-1]
    if prev_vol.size:
        avg = (prev_vol[-cfg.VOLUME_MA_PERIOD:].mean()
               if prev_vol.size >= cfg.VOLUME_MA_PERIOD else prev_vol.mean())
        ind.avg_volume_20 = float(avg)
        ind.volume_surge  = (ind.avg_volume_20 > 0
                             and float(volume[-1]) > ind.avg_volume_20 * cfg.VOLUME_MULTIPLIER)

    if entry_short_circuit and cheap_gates_veto(ind):
        # An ENABLED cheap condition already vetoes the entry — the RSI/MACD/
        # ADX values can't change the (conjunctive) decision, so skip the
        # TA-Lib calls entirely. Shares the _CONDITIONS table with entry_ok,
        # so the veto and the final check cannot drift.
        return ind

    # ── RSI (14) ────────────────────────────────────────────────────────────
    rsi_arr = talib.RSI(close, timeperiod=cfg.RSI_PERIOD)
    ind.rsi = _last(rsi_arr)
    if ind.rsi is not None:
        ind.rsi_above_30 = ind.rsi > cfg.RSI_OVERSOLD
        # Need RSI_RISING_BARS + 1 values to produce RSI_RISING_BARS diffs.
        # NaNs only pad the warmup prefix, so the plain tail slice is valid in
        # the common case; the full-array mask is paid only when the series is
        # still inside the warmup window.
        tail = rsi_arr[-(cfg.RSI_RISING_BARS + 1):]
        if np.isnan(tail).any():
            tail = rsi_arr[~np.isnan(rsi_arr)][-(cfg.RSI_RISING_BARS + 1):]
        ind.rsi_rising = (
            tail.size >= cfg.RSI_RISING_BARS + 1
            and bool(np.all(np.diff(tail) > 0))
        )

    # ── MACD (12, 26, 9) ──────────────────────────────────────────────────────
    macd, macdsignal, _ = talib.MACD(
        close, fastperiod=cfg.MACD_FAST, slowperiod=cfg.MACD_SLOW,
        signalperiod=cfg.MACD_SIGNAL,
    )
    # Store None when TA-Lib returns NaN (insufficient bars) so callers can
    # distinguish "no data" from a legitimate value of 0.0.
    ind.macd_line        = _last(macd)
    ind.macd_signal_line = _last(macdsignal)
    prev_ml  = _last(macd,       -2)
    prev_sig = _last(macdsignal, -2)
    if ind.macd_line is not None and ind.macd_signal_line is not None:
        ind.macd_histogram = ind.macd_line - ind.macd_signal_line
    if (prev_ml is not None and prev_sig is not None
            and ind.macd_line is not None and ind.macd_signal_line is not None):
        # "Bullish cross" — MACD is above signal now AND was below signal within
        # the last MACD_CROSS_BARS bars.  A window wider than 1 lets the entry
        # fire on confirming bars after the cross, not only on the exact cross bar.
        # NaNs only pad the warmup prefix — mask the full arrays only when the
        # plain tail slice still contains warmup NaNs.
        lookback_n = cfg.MACD_CROSS_BARS + 1
        m_tail = macd[-lookback_n:]
        s_tail = macdsignal[-lookback_n:]
        if np.isnan(m_tail).any() or np.isnan(s_tail).any():
            valid  = ~np.isnan(macd) & ~np.isnan(macdsignal)
            m_tail = macd[valid][-lookback_n:]
            s_tail = macdsignal[valid][-lookback_n:]
        nt = len(m_tail)   # same length — joint warmup guarantees alignment
        if nt >= 2:
            above_now    = m_tail[-1] > s_tail[-1]
            was_below    = bool(np.any(m_tail[: nt - 1] <= s_tail[: nt - 1]))
            ind.macd_bullish_cross = above_now and was_below

    # ── ADX (14) + directional movement ──────────────────────────────────────
    adx_arr = talib.ADX(high, low, close, timeperiod=cfg.ADX_PERIOD)
    plus_di_arr  = talib.PLUS_DI(high, low, close, timeperiod=cfg.ADX_PERIOD)
    minus_di_arr = talib.MINUS_DI(high, low, close, timeperiod=cfg.ADX_PERIOD)
    ind.adx      = _last(adx_arr)
    ind.plus_di  = _last(plus_di_arr)
    ind.minus_di = _last(minus_di_arr)
    ind.adx_ok   = (ind.adx is not None and ind.adx > cfg.ADX_THRESHOLD
                    and ind.plus_di is not None and ind.minus_di is not None
                    and ind.plus_di > ind.minus_di)

    return ind
