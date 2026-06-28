from __future__ import annotations

from typing import List, Optional, Tuple

import app.config as cfg
from app.engine.indicator_engine import compute_indicators
from app.engine.position_manager import calc_quantity, can_enter
from app.engine.trend_filter import check_trend
from app.models import Candle, EntrySignal
from app.state import get_state


def _bar_time(candles_5m: List[Candle]) -> str:
    if candles_5m:
        t = candles_5m[-1].start_time
        return t[11:16] if len(t) >= 16 else t
    return ""


def scan_stock(
    symbol:     str,
    token:      str,
    nifty_snap: Tuple,   # (nifty_5m: List[Candle], nifty_1d: List[Candle], nifty_ltp: float)
) -> Optional[EntrySignal]:
    """
    Full 5-minute entry scan for one stock on the most recent completed bar.

    Thread-safe design:
      - Acquires the per-token lock only for the candle list copy (microseconds).
      - All indicator math runs on the snapshot, outside any lock.
      - Safe to run concurrently in a ThreadPoolExecutor for 500 stocks.

    Multi-indicator alignment required (blueprint §5):
      1. Near 10-bar structural support
      2. Bullish candlestick pattern (Hammer / Engulfing / Strong Close)
      3. ADX(14) > 20  AND  +DI > -DI
      4. RSI(14) > 30  OR  rising for 3 bars
      5. MACD bullish line-over-signal crossover
      6. Bar volume > 1.5× 20-bar average
      7. LTP strictly above session VWAP
    """
    st = get_state()

    # Circuit breakers first — cheap before any computation
    allowed, reason = can_enter(symbol)
    if not allowed:
        st.last_scan_results[symbol] = {"pass": False, "reason": reason}
        return None

    # Thread-safe snapshot — hold lock only for the list copy, not for math
    lock = st.candle_lock(token)
    with lock:
        candles_5m = list(st.candles_5m.get(token, []))
        candles_1h = list(st.candles_1h.get(token, []))
        candles_1d = list(st.candles_1d.get(token, []))

    if len(candles_5m) < 30:
        st.last_scan_results[symbol] = {"pass": False, "reason": "Insufficient 5m bars"}
        return None

    # LTP dict writes are GIL-protected in CPython — safe to read without a lock
    ltp = st.ltp.get(symbol, candles_5m[-1].close)

    nifty_5m, nifty_1d, nifty_ltp = nifty_snap

    # Compute indicators on snapshot — pandas-ta/numpy releases GIL, giving
    # real parallelism across the 500-stock thread pool
    ind  = compute_indicators(candles_5m, candles_1h, session_candles_5m=candles_5m)
    gate = check_trend(ltp, candles_1d, candles_1h, nifty_ltp, nifty_1d, nifty_5m)

    # ── Trend gate (hard pre-filter) ──────────────────────────────────────────
    if not gate.all_clear:
        reason = (
            "Daily not green"       if not gate.daily_green      else
            "Hourly not green"      if not gate.hourly_green      else
            "NIFTY not daily-green" if not gate.nifty_daily_green else
            "NIFTY below VWAP"
        )
        st.last_scan_results[symbol] = {"pass": False, "reason": reason}
        return None

    # ── 7 entry conditions ────────────────────────────────────────────────────
    checks = {
        "near_support":    ind.near_support,
        "bullish_pattern": ind.bullish_pattern,
        "adx_ok":          ind.adx_ok,
        "rsi_ok":          ind.rsi_above_30 or ind.rsi_rising,
        "macd_cross":      ind.macd_bullish_cross,
        "volume_surge":    ind.volume_surge,
        "above_vwap":      ind.price_above_vwap,
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
        symbol         = symbol,
        token          = token,
        ltp            = ltp,
        support        = ind.support_level,
        sl_offset      = sl_offset,
        target_offset  = target_offset,
        quantity       = qty,
        capital_needed = capital_needed,
        indicators     = ind,
        trend          = gate,
        bar_time       = _bar_time(candles_5m),
    )

    st.last_scan_results[symbol] = {
        "pass":   True,
        "signal": {
            "ltp":     ltp,
            "support": ind.support_level,
            "sl":      sl_offset,
            "tgt":     target_offset,
            "qty":     qty,
            "adx":     ind.adx,
            "rsi":     ind.rsi,
            "pattern": ind.candle_pattern,
        },
    }
    return signal
