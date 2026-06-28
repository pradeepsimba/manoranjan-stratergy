from __future__ import annotations

"""
Paper-trading order simulator.
No broker connection — fills are simulated at the signal LTP and exits are
tracked automatically against each 5-minute bar's high/low.
"""

import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.models import Position, PositionStatus
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")


def place_paper_order(
    symbol:        str,
    token:         str,
    quantity:      int,
    entry_price:   float,
    sl_offset:     float,
    target_offset: float,
) -> Position:
    """
    Simulate a BUY bracket order at entry_price.
    Returns the new Position object (already added to AppState).
    """
    order_id = f"PAPER-{int(time.monotonic() * 1000)}"
    now      = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    pos = Position(
        symbol        = symbol,
        token         = token,
        entry_price   = entry_price,
        entry_time    = now,
        quantity      = quantity,
        stop_loss     = round(entry_price - sl_offset,     2),
        target        = round(entry_price + target_offset, 2),
        sl_offset     = sl_offset,
        target_offset = target_offset,
        order_id      = order_id,
        status        = PositionStatus.OPEN,
    )

    st = get_state()
    st.positions[symbol]    = pos
    st.traded_today.add(symbol)

    print(
        f"[PAPER] BUY {symbol} × {quantity} @ {entry_price:.2f} | "
        f"SL={pos.stop_loss:.2f}  TGT={pos.target:.2f}  id={order_id}"
    )
    return pos


def check_paper_exits(bar_token: str, candle_high: float, candle_low: float) -> Optional[Position]:
    """
    Called after each completed 5-minute bar.
    Checks whether the bar's high/low touched the target or stop-loss of the
    position for this token. Returns the closed Position or None.

    Precedence: if both SL and target are hit within the same bar, SL wins
    (conservative assumption — price moved against us first).
    """
    st = get_state()

    # Find position by token (positions are keyed by symbol; look up via watchlist)
    symbol = next(
        (sym for sym, tok in st.active_watchlist.items() if tok == bar_token),
        None,
    )
    if symbol is None:
        return None

    pos = st.positions.get(symbol)
    if pos is None or pos.status != PositionStatus.OPEN:
        return None

    hit_sl  = candle_low  <= pos.stop_loss
    hit_tgt = candle_high >= pos.target

    if not hit_sl and not hit_tgt:
        return None

    exit_price = pos.stop_loss if hit_sl else pos.target
    exit_label = "SL HIT" if hit_sl else "TARGET HIT"
    pnl        = round((exit_price - pos.entry_price) * pos.quantity, 2)

    pos.status     = PositionStatus.CLOSED
    pos.exit_price = exit_price
    pos.exit_time  = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    pos.pnl        = pnl

    st.daily_pnl += pnl

    print(
        f"[PAPER] {exit_label} {symbol} @ {exit_price:.2f} | "
        f"PnL ₹{pnl:+.2f}  (daily ₹{st.daily_pnl:+.2f})"
    )
    return pos
