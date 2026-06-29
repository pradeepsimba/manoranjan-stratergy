from __future__ import annotations

"""
Paper-trading order simulator.
No broker connection — fills are simulated at the signal LTP and exits are
tracked automatically against each 5-minute bar's high/low.
"""

import itertools
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.backtest.fills import round_trip_costs
from app.models import Position, PositionStatus
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# Monotonic counter for unique order ids (time-based ids collide when two fills
# land in the same millisecond — possible within one tick cycle).
_order_seq = itertools.count(1)


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
    now      = datetime.now(IST)
    order_id = f"PAPER-{now.strftime('%H%M%S')}-{next(_order_seq)}"
    now      = now.strftime("%Y-%m-%d %H:%M:%S")

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


def _finalize(pos: Position, exit_price: float, label: str) -> Position:
    """Close a position at exit_price, record net P&L (after costs), and move it out of the open book."""
    st         = get_state()
    buy_value  = pos.entry_price * pos.quantity
    sell_value = exit_price      * pos.quantity
    gross      = round((exit_price - pos.entry_price) * pos.quantity, 2)
    costs      = round(round_trip_costs(buy_value, sell_value), 2)
    pnl        = round(gross - costs, 2)

    pos.status     = PositionStatus.CLOSED
    pos.exit_price = round(exit_price, 2)
    pos.exit_time  = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    pos.pnl        = pnl

    st.daily_pnl += pnl

    # Move out of the open book so `positions` stays a true concurrent set.
    # `traded_today` still blocks same-day re-entry.
    st.positions.pop(pos.symbol, None)
    st.closed_positions.append(pos)

    print(
        f"[PAPER] {label} {pos.symbol} @ {exit_price:.2f} | "
        f"gross ₹{gross:+.2f}  costs ₹{costs:.2f}  net ₹{pnl:+.2f}  (daily ₹{st.daily_pnl:+.2f})"
    )
    return pos


def check_tick_exit(symbol: str, ltp: float) -> Optional[Position]:
    """
    Tick-wise exit: close the position the instant the live price touches the
    stop-loss or target. Filled at the level (the price has crossed it).
    Returns the closed Position or None. SL takes precedence if both are within
    reach on the same tick.
    """
    pos = get_state().positions.get(symbol)
    if pos is None or pos.status != PositionStatus.OPEN:
        return None

    if ltp <= pos.stop_loss:
        return _finalize(pos, pos.stop_loss, "SL HIT")
    if ltp >= pos.target:
        return _finalize(pos, pos.target, "TARGET HIT")
    return None


def force_close(symbol: str, exit_price: float) -> Optional[Position]:
    """Square off an open position at a given price (used for the 15:30 EOD flat)."""
    pos = get_state().positions.get(symbol)
    if pos is None or pos.status != PositionStatus.OPEN:
        return None
    return _finalize(pos, exit_price, "EOD SQUARE-OFF")
