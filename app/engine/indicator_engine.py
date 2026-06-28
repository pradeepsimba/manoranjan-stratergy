from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Tuple

import app.config as cfg
from app.models import BNIndicators, Candle, EmaStack, LeaderPatterns, PatternMatch


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return 0.0
    k   = 2.0 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val


# ── RSI(14) ───────────────────────────────────────────────────────────────────

def calc_rsi(candles: List[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(len(candles) - period, len(candles)):
        diff = candles[i].close - candles[i - 1].close
        if diff > 0: gains  += diff
        else:        losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100 - 100 / (1 + rs), 1)


# ── MACD(12,26,9) ─────────────────────────────────────────────────────────────

def calc_macd(closes: List[float]) -> Tuple[float, float, float]:
    if len(closes) < 34:
        return 0.0, 0.0, 0.0
    start = max(26, len(closes) - 30)
    macd_series: List[float] = []
    for i in range(start, len(closes) + 1):
        sub = closes[:i]
        macd_series.append(ema(sub, 12) - ema(sub, 26))
    if len(macd_series) < 9:
        m = macd_series[-1]
        return m, m, 0.0
    macd_line   = macd_series[-1]
    signal_line = ema(macd_series, 9)
    return macd_line, signal_line, macd_line - signal_line


def macd_direction(closes: List[float]) -> str:
    if len(closes) < 35:
        return "—"
    cur  = calc_macd(closes)
    prev = calc_macd(closes[:-1])
    if prev[0] <= prev[1] and cur[0] > cur[1]: return "CROSS↑"
    if prev[0] >= prev[1] and cur[0] < cur[1]: return "CROSS↓"
    if cur[0] > cur[1]: return "BUY"
    if cur[0] < cur[1]: return "SELL"
    return "NEUTRAL"


# ── EMA Stack (20/50) ─────────────────────────────────────────────────────────

def calc_ema_stack(candles: List[Candle]) -> Optional[EmaStack]:
    if len(candles) < 50:
        return None
    closes = [c.close for c in candles]
    price  = closes[-1]
    e20    = ema(closes, 20)
    e50    = ema(closes, 50)
    stack  = EmaStack()
    stack.ema20   = round(e20, 2)
    stack.ema50   = round(e50, 2)
    stack.bullish = price > e20 and e20 > e50
    stack.bearish = price < e20 and e20 < e50
    return stack


# ── Candlestick patterns ──────────────────────────────────────────────────────

def _detect_pattern(c: Candle, prev: Candle, c2: Optional[Candle]) -> Optional[str]:
    body = c.body(); rng = c.range()
    if rng == 0:
        return None
    upper     = (c.high - c.close) if c.is_bullish() else (c.high - c.open)
    lower     = (c.open  - c.low)  if c.is_bullish() else (c.close - c.low)
    prev_body = prev.body()
    if c.is_bullish() and lower >= 2*body and upper <= body*0.5 and body/rng < 0.4 and prev.is_bearish():
        return "Hammer (Bull)"
    if c.is_bearish() and upper >= 2*body and lower <= body*0.5 and body/rng < 0.4 and prev.is_bullish():
        return "Shooting Star (Bear)"
    if (c.is_bullish() and prev.is_bearish()
            and c.open <= prev.close and c.close >= prev.open and body > prev_body * 0.9):
        return "Bull Engulfing"
    if (c.is_bearish() and prev.is_bullish()
            and c.open >= prev.close and c.close <= prev.open and body > prev_body * 0.9):
        return "Bear Engulfing"
    if (c2 and c2.is_bearish() and prev.body() <= c2.body() * 0.4
            and c.is_bullish() and c.close > (c2.open + c2.close) / 2):
        return "Morning Star (Bull)"
    if (c2 and c2.is_bullish() and prev.body() <= c2.body() * 0.4
            and c.is_bearish() and c.close < (c2.open + c2.close) / 2):
        return "Evening Star (Bear)"
    return None


def check_leader_patterns() -> LeaderPatterns:
    from app.state import get_state
    st = get_state()
    lp = LeaderPatterns()
    for stock_name in cfg.LEADER_STOCKS:
        stock = next((s for s in cfg.STOCKS if s.name == stock_name), None)
        if not stock:
            continue
        candles = st.last_n_candles.get(stock.symbol, [])
        if len(candles) < 3:
            continue
        pat = _detect_pattern(candles[-1], candles[-2], candles[-3])
        if not pat:
            continue
        is_bull = any(k in pat for k in ("Bull", "Morning", "Hammer"))
        is_bear = any(k in pat for k in ("Bear", "Evening", "Shooting"))
        if is_bull: lp.bull_count += 1
        if is_bear: lp.bear_count += 1
        lp.matches.append(PatternMatch(stock=stock_name, pattern=pat))
    return lp


# ── BN gate ───────────────────────────────────────────────────────────────────

def check_bn_indicators() -> BNIndicators:
    from app.state import get_state
    st  = get_state()
    ind = BNIndicators()

    with st._bn_ind_lock:
        bn_candles = list(st.bn_indicator_candles)
    if not bn_candles:
        dc         = st.last_n_candles.get(cfg.INDEX_SYMBOL, [])
        bn_candles = list(dc)

    closes = [c.close for c in bn_candles]

    ind.rsi      = calc_rsi(bn_candles, 14)
    ind.macd_dir = macd_direction(closes)
    if len(closes) >= 34:
        m            = calc_macd(closes)
        ind.macd_val = round(m[0], 2)
    ind.ema_stack  = calc_ema_stack(bn_candles)
    ind.leader_pat = check_leader_patterns()

    bull = bear = 0.0

    if ind.rsi is not None:
        if   ind.rsi > 58: bull += 1
        elif ind.rsi < 42: bear += 1
        if   ind.rsi > 72: bear += 0.5   # overbought → reversal pressure
        elif ind.rsi < 28: bull += 0.5   # oversold   → reversal pressure

    macd_score = {"CROSS↑": (2, 0), "CROSS↓": (0, 2), "BUY": (1, 0), "SELL": (0, 1)}
    b, r = macd_score.get(ind.macd_dir or "", (0, 0))
    bull += b; bear += r

    if ind.ema_stack:
        if ind.ema_stack.bullish: bull += 2
        if ind.ema_stack.bearish: bear += 2

    if ind.leader_pat:
        if ind.leader_pat.bull_count >= 2: bull += 2
        if ind.leader_pat.bear_count >= 2: bear += 2

    # Over-extended EMA penalty
    if ind.ema_stack and bn_candles:
        price = bn_candles[-1].close
        if ind.ema_stack.ema20 > 0:
            ext = abs(price - ind.ema_stack.ema20) / ind.ema_stack.ema20 * 100
            if ext > 1.2:
                if price > ind.ema_stack.ema20: bear += 0.5
                else:                           bull += 0.5

    ind.bull    = max(0.0, bull)
    ind.bear    = max(0.0, bear)
    ind.bullish = bull >= 2 and bull > bear + 0.9
    ind.bearish = bear >= 2 and bear > bull + 0.9
    return ind


# ── Sideways filter ───────────────────────────────────────────────────────────

def sideways_range(candles: List[Candle]) -> Optional[float]:
    if not candles or len(candles) < 5:
        return None
    closes = [c.close for c in candles[-5:]]
    return max(closes) - min(closes)


# ── Momentum ──────────────────────────────────────────────────────────────────

def calc_atr(candles: List[Candle], period: int) -> float:
    if len(candles) < period + 1:
        return -1.0
    total = 0.0
    for i in range(len(candles) - period, len(candles)):
        c = candles[i]; prev = candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        total += tr
    return total / period


@dataclass
class MomentumResult:
    ok:     bool
    reason: str


def strong_momentum(candles: List[Candle], interval: str) -> MomentumResult:
    if not candles or len(candles) < 2:
        return MomentumResult(False, "No candles")
    c1 = candles[-1]; c2 = candles[-2]
    fixed = {"3m": 20.0, "5m": 28.0, "15m": 50.0}.get(interval, 15.0)
    atr   = calc_atr(candles, 10)
    threshold = fixed
    if atr > 0:
        atr_thr   = atr * 0.7
        threshold = min(fixed, max(atr_thr, fixed * 0.6))
    c1_move = c1.close - c1.open; c2_move = c2.close - c2.open
    c1_abs  = abs(c1_move);       c2_abs  = abs(c2_move)
    # Case A: single candle ≥ 80 % of threshold
    if c1_abs >= threshold * 0.8:
        return MomentumResult(True, f"C1={c1_abs:.1f} pts")
    # Case B: two same-direction candles combined ≥ threshold
    if c1_move * c2_move > 0 and c1_abs + c2_abs >= threshold:
        return MomentumResult(True, f"2C={c1_abs:.1f}+{c2_abs:.1f} pts")
    # Case C: ATR-based gate
    if atr > 0 and c1_abs >= atr * 0.5 and c1_abs >= fixed * 0.6:
        return MomentumResult(True, f"ATR={atr:.1f} C1={c1_abs:.1f}")
    return MomentumResult(False, f"Weak: C1={c1_abs:.1f} need>={threshold*0.8:.0f}")
