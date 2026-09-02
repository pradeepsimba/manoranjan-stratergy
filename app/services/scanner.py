from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.models import Candle
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# Read-only candlestick-pattern screeners over the shared 5m candle buffers —
# same role as the Console/Journal aggregates (app/services/database.py):
# they report on data, never place or influence an order. Not a strategy.
#
# Both patterns are anchored to a stock's FIRST 3 candles of the current
# trading day (e.g. the 09:15/09:20/09:25 bars), not a rolling "latest 3
# candles" window — Chartink's [=1]/[=2]/[=3] here map onto the day's
# 1st/2nd/3rd candle in chronological order (c1 earliest -> c3 latest of the
# three). Once a stock's 3rd candle has closed the result is fixed for the
# rest of the day; candles_5m keeps rolling across multiple days (see
# app/state.py), so today's slice has to be picked out by date first.


def _todays_first_3(candles: List[Candle]) -> Optional[List[Candle]]:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    todays = [c for c in candles if c.start_time.startswith(today)]
    return todays[:3] if len(todays) >= 3 else None


def _first_3_red(c1: Candle, c2: Candle, c3: Candle) -> bool:
    return (
        c2.low < c1.low
        and c3.close < c2.low
        and c2.close <= c2.open
        and c1.close < c1.open
    )


def _first_3_green(c1: Candle, c2: Candle, c3: Candle) -> bool:
    return (
        c2.high > c1.high
        and c3.close > c2.high
        and c2.close >= c2.open
        and c1.close > c1.open
    )


SCANNERS: Dict[str, Dict[str, Any]] = {
    "first_3_red_candles":   {"label": "First 3 Red Candles",   "fn": _first_3_red},
    "first_3_green_candles": {"label": "First 3 Green Candles", "fn": _first_3_green},
}


async def run_scan(db) -> Dict[str, Any]:
    """Evaluate every registered scanner against each tradable instrument's
    first 3 candles of today, returning matches keyed by scanner id plus a
    `meta` block (instrument/candle counts) so an empty result can be told
    apart from "today's 3rd candle hasn't closed yet"."""
    st = get_state()
    rows = await db.get_tradable_instruments()
    matches: Dict[str, List[Dict[str, Any]]] = {key: [] for key in SCANNERS}
    scanned = 0
    skipped_no_candles = 0

    for r in rows:
        token = r["token"]
        with st.candle_lock(token):
            candles = list(st.candles_5m.get(token, []))
        first3 = _todays_first_3(candles)
        if first3 is None:
            skipped_no_candles += 1
            continue
        scanned += 1
        c1, c2, c3 = first3
        ltp = st.ltp.get(token) or candles[-1].close
        for key, spec in SCANNERS.items():
            fn: Callable[[Candle, Candle, Candle], bool] = spec["fn"]
            if fn(c1, c2, c3):
                matches[key].append({
                    "token":     token,
                    "name":      r["display_name"],
                    "ltp":       ltp,
                    "matchedAt": c3.start_time,
                })

    return {
        **matches,
        "meta": {
            "totalInstruments": len(rows),
            "scanned": scanned,
            "skippedInsufficientCandles": skipped_no_candles,
        },
    }
