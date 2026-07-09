from __future__ import annotations

"""
Bank Nifty signal gates — ported from c.html's strongMomentum/calcATR,
leadersMomentum, bnDetectCandlePattern/bnLeaderStockPatterns,
bnCalcEMAStack, and checkBNIndicators.

RSI/EMA use TA-Lib (this repo's convention for indicator math — see
indicator_engine.py) instead of c.html's hand-rolled JS loops; the formulas
are numerically equivalent (Wilder RSI, EMA seeded on an SMA). c.html's MACD
gate is a raw EMA12-EMA26 zero-cross with NO signal line at all — ported as
two plain EMA calls, not talib.MACD (which would force a signal line that
doesn't exist in the source).
"""

from typing import Dict, List, Optional

import numpy as np
import talib

import app.config as cfg
from app.models import Candle


# ── Sideways / momentum filters ───────────────────────────────────────────────

def sideways_range(closes: np.ndarray, bars: int = 5) -> float:
    """Range (max-min) of the last `bars` closes — c.html's sideways-market filter input."""
    tail = closes[-bars:] if closes.size >= bars else closes
    if tail.size == 0:
        return 0.0
    return float(tail.max() - tail.min())


def calc_atr(candles: List[Candle], period: int) -> Optional[float]:
    """
    Simple (non-Wilder) average True Range over the last `period` candles —
    matches c.html's calcATR exactly (plain arithmetic mean of True Range, not
    TA-Lib's Wilder-smoothed talib.ATR).
    """
    if len(candles) < period + 1:
        return None
    window = candles[-period:]
    trs = []
    for i in range(len(window)):
        c = window[i]
        prev_close = window[i - 1].close if i > 0 else candles[-period - 1].close
        high = c.high if c.high else max(c.open, c.close)
        low = c.low if c.low else min(c.open, c.close)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else None


def strong_momentum(candles: List[Candle]) -> Dict:
    """
    Port of c.html's strongMomentum(): a single strong candle, a same-direction
    2-candle combo, or an ATR-relative impulsive move. ATR can only LOWER the
    fixed threshold (never raise it), floored at 60% of the fixed value.
    """
    if len(candles) < 2:
        return {"ok": False, "reason": "Insufficient candles for momentum check"}

    threshold = float(cfg.BN_MOMENTUM_THRESHOLD)
    atr = calc_atr(candles, cfg.BN_ATR_PERIOD)
    if atr and atr > 0:
        atr_threshold = atr * 0.7
        threshold = min(threshold, max(atr_threshold, threshold * 0.6))

    c1, c2 = candles[-1], candles[-2]
    move1 = abs(c1.close - c1.open)
    move2 = abs(c2.close - c2.open)

    if move1 >= threshold * 0.8:
        return {"ok": True, "reason": "Single strong candle", "threshold": threshold}

    dir1 = 1 if c1.close > c1.open else (-1 if c1.close < c1.open else 0)
    dir2 = 1 if c2.close > c2.open else (-1 if c2.close < c2.open else 0)
    if dir1 != 0 and dir1 == dir2 and (move1 + move2) >= threshold:
        return {"ok": True, "reason": "2-candle combo", "threshold": threshold}

    if atr and atr > 0 and move1 >= atr * 0.5 and move1 >= threshold * 0.6:
        return {"ok": True, "reason": "Impulsive vs ATR", "threshold": threshold}

    return {"ok": False, "reason": "Momentum too weak", "threshold": threshold}


# ── Leader-stock direction vote ───────────────────────────────────────────────

def leaders_momentum(leader_last_candles: Dict[str, Optional[Candle]]) -> Dict:
    """
    Port of c.html's leadersMomentum(): ≥ BN_SAME_DIRECTION_REQUIRED of the
    leader stocks must agree on direction (close vs open) to produce a signal.
    """
    if any(c is None for c in leader_last_candles.values()):
        return {"signal": "Nobuysell", "reason": "Leader candle missing",
                "buy_count": 0, "sell_count": 0}

    buy_count = sum(1 for c in leader_last_candles.values() if c.close > c.open)
    sell_count = sum(1 for c in leader_last_candles.values() if c.close < c.open)
    required = cfg.BN_SAME_DIRECTION_REQUIRED

    if buy_count >= required:
        return {"signal": "BUY", "reason": f"{buy_count} leaders aligned bullish",
                "buy_count": buy_count, "sell_count": sell_count}
    if sell_count >= required:
        return {"signal": "SELL", "reason": f"{sell_count} leaders aligned bearish",
                "buy_count": buy_count, "sell_count": sell_count}
    return {"signal": "Nobuysell", "reason": "No leader majority",
            "buy_count": buy_count, "sell_count": sell_count}


# ── Candlestick pattern (bespoke — not TA-Lib CDL*, thresholds don't match) ──

def candle_pattern(candles: List[Candle]) -> Optional[str]:
    """Port of c.html's bnDetectCandlePattern — needs the last 3 candles (candles[-3:])."""
    if len(candles) < 2:
        return None
    c, p = candles[-1], candles[-2]
    pp = candles[-3] if len(candles) >= 3 else None

    c_up, p_up = c.close > c.open, p.close > p.open
    c_body, p_body = abs(c.close - c.open), abs(p.close - p.open)
    c_range = c.high - c.low

    if (not p_up and c_up and c.open <= p.close and c.close >= p.open
            and c_body > p_body * 0.9):
        return "Bullish Engulfing"
    if (p_up and not c_up and c.open >= p.close and c.close <= p.open
            and c_body > p_body * 0.9):
        return "Bearish Engulfing"

    if pp is not None:
        pp_up = pp.close > pp.open
        pp_body = abs(pp.close - pp.open)
        if (not pp_up and p_body <= pp_body * 0.4 and c_up
                and c.close > (pp.open + pp.close) / 2):
            return "Morning Star"
        if (pp_up and p_body <= pp_body * 0.4 and not c_up
                and c.close < (pp.open + pp.close) / 2):
            return "Evening Star"

    if c_range > 0:
        lower_wick = (c.open - c.low) if c_up else (c.close - c.low)
        upper_wick = (c.high - c.close) if c_up else (c.high - c.open)
        if lower_wick >= 2 * c_body and upper_wick <= c_body * 0.5 and c_body / c_range < 0.4:
            return "Hammer"
        if upper_wick >= 2 * c_body and lower_wick <= c_body * 0.5 and c_body / c_range < 0.4:
            return "Shooting Star"

    return None


_PATTERN_SCORE = {
    "Bullish Engulfing": 2, "Morning Star": 2, "Hammer": 1,
    "Bearish Engulfing": -2, "Evening Star": -2, "Shooting Star": -1,
}


def leader_patterns(leader_candle_lists: Dict[str, List[Candle]]) -> Dict:
    """Port of c.html's bnLeaderStockPatterns — tally bull/bear pattern hits across leaders."""
    bull_count = bear_count = 0
    matches: Dict[str, str] = {}
    for name, candles in leader_candle_lists.items():
        if len(candles) < 3:
            continue
        pat = candle_pattern(candles)
        if pat is None:
            continue
        score = _PATTERN_SCORE.get(pat, 0)
        matches[name] = pat
        if score > 0:
            bull_count += 1
        elif score < 0:
            bear_count += 1
    return {"bull_count": bull_count, "bear_count": bear_count, "matches": matches}


# ── EMA stack + composite BN indicator gate ───────────────────────────────────

def ema_stack(closes: np.ndarray) -> Dict:
    """Port of c.html's bnCalcEMAStack — price vs EMA20 vs EMA50."""
    if closes.size < cfg.BN_EMA_SLOW:
        return {"bullish": False, "bearish": False, "ema_fast": None, "ema_slow": None}
    ema_fast_arr = talib.EMA(closes, timeperiod=cfg.BN_EMA_FAST)
    ema_slow_arr = talib.EMA(closes, timeperiod=cfg.BN_EMA_SLOW)
    ema_fast, ema_slow = float(ema_fast_arr[-1]), float(ema_slow_arr[-1])
    price = float(closes[-1])
    if np.isnan(ema_fast) or np.isnan(ema_slow):
        return {"bullish": False, "bearish": False, "ema_fast": None, "ema_slow": None}
    return {
        "bullish": price > ema_fast > ema_slow,
        "bearish": price < ema_fast < ema_slow,
        "ema_fast": ema_fast, "ema_slow": ema_slow,
    }


def bn_composite_indicator(bn_closes: np.ndarray,
                           leader_candle_lists: Dict[str, List[Candle]]) -> Dict:
    """
    Port of c.html's checkBNIndicators — combined RSI level / MACD zero-cross /
    leader-pattern tally / EMA-stack scoring. bull/bear scores ≥ BN_SCORE_MIN
    AND beating the other side by > BN_SCORE_MARGIN decide bullish/bearish.
    """
    lookback = cfg.BN_INDICATOR_LOOKBACK_BARS
    closes = bn_closes[-lookback:] if bn_closes.size > lookback else bn_closes
    if closes.size < 50:
        return {"bull": 0.0, "bear": 0.0, "bullish": False, "bearish": False,
                "rsi": None, "macd_dir": None, "macd_val": None,
                "ema_bullish": None, "ema_bearish": None}

    bull = bear = 0.0

    rsi_arr = talib.RSI(closes, timeperiod=cfg.BN_RSI_PERIOD)
    rsi = float(rsi_arr[-1]) if not np.isnan(rsi_arr[-1]) else None
    if rsi is not None:
        if rsi > cfg.BN_RSI_BULL_LEVEL:
            bull += 1
        elif rsi < cfg.BN_RSI_BEAR_LEVEL:
            bear += 1
        if rsi > cfg.BN_RSI_OVERBOUGHT:
            bear += 0.5
        if rsi < cfg.BN_RSI_OVERSOLD:
            bull += 0.5

    # MACD as a raw EMA(fast)-EMA(slow) zero-cross — NO signal line (c.html
    # never computes one despite the "(12,26,9)" UI label).
    ema_fast_arr = talib.EMA(closes, timeperiod=cfg.BN_MACD_FAST)
    ema_slow_arr = talib.EMA(closes, timeperiod=cfg.BN_MACD_SLOW)
    macd_arr = ema_fast_arr - ema_slow_arr
    macd_val = float(macd_arr[-1]) if not np.isnan(macd_arr[-1]) else None
    prev_macd = float(macd_arr[-2]) if macd_arr.size >= 2 and not np.isnan(macd_arr[-2]) else None
    macd_dir = None
    if macd_val is not None:
        if prev_macd is not None and prev_macd < 0 and macd_val > 0:
            bull += 2
            macd_dir = "CROSS_UP"
        elif prev_macd is not None and prev_macd > 0 and macd_val < 0:
            bear += 2
            macd_dir = "CROSS_DOWN"
        elif macd_val > 0:
            bull += 1
            macd_dir = "BUY"
        elif macd_val < 0:
            bear += 1
            macd_dir = "SELL"

    leader_pat = leader_patterns(leader_candle_lists)
    if leader_pat["bull_count"] >= 2:
        bull += 2
    if leader_pat["bear_count"] >= 2:
        bear += 2

    stack = ema_stack(closes)
    if stack["bullish"]:
        bull += 2
    if stack["bearish"]:
        bear += 2

    if stack["ema_fast"]:
        dist_pct = (float(closes[-1]) - stack["ema_fast"]) / stack["ema_fast"] * 100.0
        if dist_pct > cfg.BN_EMA_EXTENSION_PCT:
            bear += 0.5
        elif dist_pct < -cfg.BN_EMA_EXTENSION_PCT:
            bull += 0.5

    bullish = bull >= cfg.BN_SCORE_MIN and bull > bear + cfg.BN_SCORE_MARGIN
    bearish = bear >= cfg.BN_SCORE_MIN and bear > bull + cfg.BN_SCORE_MARGIN

    return {
        "bull": bull, "bear": bear, "bullish": bullish, "bearish": bearish,
        "rsi": rsi, "macd_dir": macd_dir, "macd_val": macd_val,
        "ema_bullish": stack["bullish"], "ema_bearish": stack["bearish"],
    }
