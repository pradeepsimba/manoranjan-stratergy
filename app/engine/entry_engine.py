from __future__ import annotations

from typing import Optional

import app.config as cfg
from app.engine.indicator_engine import compute_indicators
from app.engine.position_manager import calc_quantity, can_enter
from app.engine.trend_filter import check_trend
from app.models import EntrySignal
from app.state import get_state


def _bar_time(candles_5m) -> str:
    if candles_5m:
        t = candles_5m[-1].start_time
        return t[11:16] if len(t) >= 16 else t
    return ""


def scan_stock(symbol: str, token: str) -> Optional[EntrySignal]:
    """
    Run the full 5-minute entry scan for one stock on the most recent
    completed bar. Returns an EntrySignal if ALL conditions align, else None.

    Multi-indicator alignment required (blueprint §5):
      1. Near 10-bar structural support
      2. Bullish candlestick pattern (Hammer / Engulfing / Strong Close)
      3. ADX(14) > 20  AND  +DI > -DI
      4. RSI(14) crossed above 30  OR  rose for 3 consecutive bars
      5. MACD bullish line-over-signal crossover
      6. Bar volume > 1.5× 20-bar average
      7. LTP strictly above session VWAP
    """
    st = get_state()

    # Circuit breakers first (cheap check before any computation)
    allowed, reason = can_enter(symbol)
    if not allowed:
        st.last_scan_results[symbol] = {"pass": False, "reason": reason}
        return None

    # Candle stores are keyed by token (stock_symbol from WS tick)
    candles_5m = st.candles_5m.get(token, [])
    candles_1h = st.candles_1h.get(token, [])

    if len(candles_5m) < 30:
        st.last_scan_results[symbol] = {"pass": False, "reason": "Insufficient 5m bars"}
        return None

    # Session candles (today from 09:15) for VWAP — the full 5m series is used
    ind  = compute_indicators(candles_5m, candles_1h, session_candles_5m=candles_5m)
    gate = check_trend(symbol, token)

    # LTP store keyed by stockname (symbol), candles keyed by token
    ltp = st.ltp.get(symbol, candles_5m[-1].close)

    # ── Trend gate (hard pre-filter) ──────────────────────────────────────────
    if not gate.all_clear:
        reason = (
            "Daily not green"     if not gate.daily_green      else
            "Hourly not green"    if not gate.hourly_green      else
            "NIFTY not daily-green" if not gate.nifty_daily_green else
            "NIFTY below VWAP"
        )
        st.last_scan_results[symbol] = {"pass": False, "reason": reason}
        return None

    # ── 7 entry conditions ────────────────────────────────────────────────────
    checks = {
        "near_support":      ind.near_support,
        "bullish_pattern":   ind.bullish_pattern,
        "adx_ok":            ind.adx_ok,
        "rsi_ok":            ind.rsi_above_30 or ind.rsi_rising,
        "macd_cross":        ind.macd_bullish_cross,
        "volume_surge":      ind.volume_surge,
        "above_vwap":        ind.price_above_vwap,
    }

    failed = [k for k, v in checks.items() if not v]
    if failed:
        st.last_scan_results[symbol] = {
            "pass":   False,
            "reason": f"Failed: {', '.join(failed)}",
            "ind":    {k: v for k, v in checks.items()},
        }
        return None

    # ── Position sizing ───────────────────────────────────────────────────────
    qty, sl_offset, target_offset = calc_quantity(ltp, ind.support_level)
    if qty == 0:
        st.last_scan_results[symbol] = {"pass": False, "reason": "Invalid SL / size=0"}
        return None

    capital_needed = (ltp * qty) / cfg.INTRADAY_LEVERAGE

    signal = EntrySignal(
        symbol        = symbol,
        token         = token,
        ltp           = ltp,
        support       = ind.support_level,
        sl_offset     = sl_offset,
        target_offset = target_offset,
        quantity      = qty,
        capital_needed = capital_needed,
        indicators    = ind,
        trend         = gate,
        bar_time      = _bar_time(candles_5m),
    )

    st.last_scan_results[symbol] = {
        "pass":   True,
        "signal": {
            "ltp":    ltp,
            "support": ind.support_level,
            "sl":     sl_offset,
            "tgt":    target_offset,
            "qty":    qty,
            "adx":    ind.adx,
            "rsi":    ind.rsi,
            "pattern": ind.candle_pattern,
        },
    }
    return signal
