from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine.conditions import build_entry_checks, failed_entry_checks
from app.engine.indicator_engine import compute_indicators
from app.engine.position_manager import calc_quantity, can_enter
from app.engine.trend_filter import check_trend, trend_blockers
from app.models import Candle, EntrySignal
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# An hourly candle whose START is older than this is stale: a healthy 1h feed
# updates the forming bar continuously, so its start_time is at most ~60min
# old. Beyond this, the primary-1h WS connection has died and the "hourly
# green" gate would silently evaluate a frozen candle for the rest of the day.
_STALE_1H_SECONDS = 75 * 60


def _stale_1h(candles_1h: List[Candle]) -> bool:
    if not candles_1h:
        return False
    ts = candles_1h[-1].start_time[:16].replace("T", " ")
    try:
        bar_start = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    except ValueError:
        return False
    return (datetime.now(IST) - bar_start).total_seconds() > _STALE_1H_SECONDS


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

    Multi-indicator alignment required (blueprint §5) — every ENABLED condition
    must pass (see app.engine.conditions; toggles are runtime settings):
      1. Near structural support
      2. Bullish candlestick pattern (Hammer / Engulfing / Strong Close)
      3. ADX > threshold  AND  +DI > -DI
      4. RSI > floor  OR  rising N bars
      5. MACD bullish line-over-signal crossover
      6. Bar volume > multiplier × volume MA
      7. LTP strictly above session VWAP
      8. Order-book depth not sell-skewed (live only)
    """
    st = get_state()

    # Thread-safe snapshot — hold lock only for the list copy, not for math.
    # Only the LAST 1h bar is ever consumed (hourly gate + staleness check),
    # so copy just that ref instead of the whole ~300-bar list. Safe outside
    # the lock afterwards: the WS upsert REPLACES the tail element, never
    # mutates a Candle in place.
    lock = st.candle_lock(token)
    with lock:
        candles_5m = list(st.candles_5m.get(token, []))
        lst_1h     = st.candles_1h.get(token)
        candles_1h = [lst_1h[-1]] if lst_1h else []

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
    ind        = compute_indicators(candles_5m, session_candles_5m=session_5m)
    _hist      = (round(ind.macd_line - ind.macd_signal_line, 4)
                  if ind.macd_line is not None and ind.macd_signal_line is not None else None)
    st.indicator_snapshot[symbol] = {
        "ltp":         round(ltp, 2),
        "bar_time":    _bar_time(candles_5m),
        "rsi":         round(ind.rsi, 1)               if ind.rsi         is not None else None,
        "adx":         round(ind.adx, 1)               if ind.adx      is not None else None,
        "plus_di":     round(ind.plus_di, 1)           if ind.plus_di  is not None else None,
        "minus_di":    round(ind.minus_di, 1)          if ind.minus_di is not None else None,
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

    # ── Trend gate (entry pre-filter; disabled gates can't block) ────────────
    # A frozen 1h candle (dead primary-1h connection) must read as MISSING —
    # check_trend then leaves hourly_green False (fail-closed), same as the
    # empty-list case, instead of gating on hours-old data all day.
    if _stale_1h(candles_1h):
        candles_1h = []
    gate     = check_trend(ltp, day_open, candles_1h, nifty_daily_green, nifty_above_vwap)
    blockers = trend_blockers(gate)
    if blockers:
        st.record_scan(symbol, {"pass": False, "reason": blockers[0]})
        return None

    # ── 8 entry conditions (shared with backtest; toggles are settings) ──────
    checks = build_entry_checks(ind, depth.get("ratio"))
    failed = failed_entry_checks(checks)
    if failed:
        st.record_scan(symbol, {
            "pass":   False,
            "reason": f"Failed: {', '.join(failed)}",
            "ind":    {k: v for k, v in checks.items()},
        })
        return None

    # ── Position sizing ───────────────────────────────────────────────────────
    # Concurrent positions SHARE the account: size from what open positions
    # haven't already committed (value ÷ leverage), matching the backtest's
    # Portfolio.margin_used semantics. list() snapshots the dict atomically
    # (CPython/GIL) — positions are mutated only on the event loop while this
    # runs in a scan-pool worker.
    lev       = cfg.INTRADAY_LEVERAGE
    committed = sum(p.entry_price * p.quantity
                    for p in list(st.positions.values())) / lev
    available = cfg.ACCOUNT_BALANCE - committed
    if available <= 0:
        st.record_scan(symbol, {"pass": False, "reason": "No free capital"})
        return None

    qty, sl_offset, target_offset = calc_quantity(ltp, ind.support_level, available)
    if qty == 0:
        st.record_scan(symbol, {"pass": False, "reason": "Invalid SL / size=0"})
        return None

    capital_needed = (ltp * qty) / lev

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
