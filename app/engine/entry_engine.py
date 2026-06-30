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
    symbol:      str,
    token:       str,
    nifty_gates: Tuple[bool, bool],   # (nifty_daily_green, nifty_above_vwap) — precomputed once per bar
    tradeable:   bool = True,         # False for non-Gemini stocks: update indicators but skip entry
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

    # Thread-safe snapshot — hold lock only for the list copy, not for math
    lock = st.candle_lock(token)
    with lock:
        candles_5m = list(st.candles_5m.get(token, []))
        candles_1h = list(st.candles_1h.get(token, []))

    if len(candles_5m) < 30:
        st.record_scan(symbol, {"pass": False, "reason": "Insufficient 5m bars"})
        return None

    # LTP and depth dict writes are GIL-protected in CPython — safe without a lock
    ltp   = st.ltp.get(symbol, candles_5m[-1].close)
    depth = st.depth.get(symbol, {})

    # Today's bars are a contiguous suffix (candles are chronological). Find the
    # suffix start with an O(today) backward walk instead of an O(buffer) scan,
    # so the daily gate is cheap and the session slice is built only if it passes.
    today  = candles_5m[-1].start_time[:10]
    i      = len(candles_5m)
    while i > 0 and candles_5m[i - 1].start_time[:10] == today:
        i -= 1
    day_open = candles_5m[i].open   # i < len: the last bar is always today's

    nifty_daily_green, nifty_above_vwap = nifty_gates

    # Compute indicators on the snapshot — TA-Lib's C layer releases the GIL,
    # giving real parallelism across the thread pool. Always runs (even when the
    # trend gate would block entry) so the live indicators page receives tick-level
    # updates for every scanned stock, not just potential entries.
    session_5m = candles_5m[i:]
    ind        = compute_indicators(candles_5m, candles_1h, session_candles_5m=session_5m)
    _hist      = (round(ind.macd_line - ind.macd_signal_line, 4)
                  if ind.macd_line is not None and ind.macd_signal_line is not None else None)
    st.indicator_snapshot[symbol] = {
        "ltp":         round(ltp, 2),
        "bar_time":    _bar_time(candles_5m),
        "rsi":         round(ind.rsi, 1)               if ind.rsi         is not None else None,
        "adx":         round(ind.adx, 1)               if ind.adx                     else None,
        "plus_di":     round(ind.plus_di, 1)           if ind.plus_di                 else None,
        "minus_di":    round(ind.minus_di, 1)          if ind.minus_di                else None,
        "macd":        round(ind.macd_line, 4)         if ind.macd_line   is not None else None,
        "macd_signal": round(ind.macd_signal_line, 4) if ind.macd_signal_line is not None else None,
        "macd_hist":   _hist,
        "support":     round(ind.support_level, 2)     if ind.support_level           else None,
        "vwap":        round(ind.vwap, 2)              if ind.vwap                    else None,
        "above_vwap":  ind.price_above_vwap,
        "pattern":     ind.candle_pattern,
        # Order-book depth (None when no tick with snap data received yet)
        "bid":         round(depth["bid"],    2) if "bid"    in depth else None,
        "ask":         round(depth["ask"],    2) if "ask"    in depth else None,
        "spread":      depth.get("spread"),
        "buy_qty":     depth.get("buy_qty"),
        "sell_qty":    depth.get("sell_qty"),
        "ratio":       depth.get("ratio"),
    }

    # Non-tradeable stocks (not in Gemini watchlist) only need indicator updates.
    if not tradeable:
        return None

    # Circuit breakers — checked after indicator snapshot so the display always
    # reflects the latest data even when entries are blocked.
    allowed, reason = can_enter(symbol, st.positions, st.traded_today, st.daily_pnl)
    if not allowed:
        st.record_scan(symbol, {"pass": False, "reason": reason})
        return None

    # ── Trend gate (entry pre-filter) ────────────────────────────────────────
    gate = check_trend(ltp, day_open, candles_1h, nifty_daily_green, nifty_above_vwap)
    if not gate.all_clear:
        reason = (
            "Daily not green"       if not gate.daily_green      else
            "Hourly not green"      if not gate.hourly_green      else
            "NIFTY not daily-green" if not gate.nifty_daily_green else
            "NIFTY below VWAP"
        )
        st.record_scan(symbol, {"pass": False, "reason": reason})
        return None

    # ── 8 entry conditions ────────────────────────────────────────────────────
    # depth_bullish: buy-side depth ≥ 40% of total (not heavily sell-skewed).
    # Defaults True when no snap data is available yet so it never blocks on
    # missing data — it only fires when the order book is clearly bearish.
    ratio = depth.get("ratio")
    depth_bullish = (ratio >= 0.4) if ratio is not None else True
    checks = {
        "near_support":    ind.near_support,
        "bullish_pattern": ind.bullish_pattern,
        "adx_ok":          ind.adx_ok,
        "rsi_ok":          ind.rsi_above_30 or ind.rsi_rising,
        "macd_cross":      ind.macd_bullish_cross,
        "volume_surge":    ind.volume_surge,
        "above_vwap":      ind.price_above_vwap,
        "depth_bullish":   depth_bullish,
    }

    failed = [k for k, v in checks.items() if not v]
    if failed:
        st.record_scan(symbol, {
            "pass":   False,
            "reason": f"Failed: {', '.join(failed)}",
            "ind":    {k: v for k, v in checks.items()},
        })
        return None

    # ── Position sizing ───────────────────────────────────────────────────────
    qty, sl_offset, target_offset = calc_quantity(ltp, ind.support_level)
    if qty == 0:
        st.record_scan(symbol, {"pass": False, "reason": "Invalid SL / size=0"})
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

    st.record_scan(symbol, {
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
    })
    return signal
