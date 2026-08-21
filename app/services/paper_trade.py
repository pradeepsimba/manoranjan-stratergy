from __future__ import annotations

"""
Paper-trading order simulator.
No broker connection — fills are simulated at the signal LTP and exits are
tracked automatically against each 5-minute bar's high/low.

Fills carry the SAME SLIPPAGE_BPS haircut as the backtest (buy slipped up,
sell slipped down): both engines share the cost model, and un-slipped live
fills would systematically flatter live paper P&L relative to the backtest
(and to reality — a real market order crosses the spread).
"""

import itertools
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import app.config as cfg
from app.backtest.fills import round_trip_costs
from app.models import STRATEGY_CORE, STRATEGY_SCALP, Position, PositionStatus
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")


def _slip_buy(price: float) -> float:
    return price * (1 + cfg.SLIPPAGE_BPS / 10_000.0)


def _slip_sell(price: float) -> float:
    return price * (1 - cfg.SLIPPAGE_BPS / 10_000.0)

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
    strategy:      str = STRATEGY_CORE,
) -> Position:
    """
    Simulate a BUY bracket order at entry_price.
    Returns the new Position object (already added to AppState).

    `strategy` tags which engine owns the trade (see models.STRATEGY_*). Both
    books live in the same AppState.positions dict so exits, persistence and the
    dashboard stay unforked; the tag is what lets each engine apply its own
    caps, square-off time and loss limit to only its own trades.
    """
    now      = datetime.now(IST)
    # Date-scoped id: the sequence resets on every restart, so a time-only id
    # (HHMMSS-seq) could collide with another DAY's row — order_id backs a
    # unique index that makes DB retries idempotent, so ids must never repeat.
    order_id = f"PAPER-{now.strftime('%Y%m%d-%H%M%S')}-{next(_order_seq)}"
    now      = now.strftime("%Y-%m-%d %H:%M:%S")

    # Slip the buy upward and anchor SL/target on the SLIPPED fill — exactly
    # how the backtest's entry_fill works (stop = fill − sl_offset).
    fill = round(_slip_buy(entry_price), 2)

    pos = Position(
        symbol        = symbol,
        token         = token,
        entry_price   = fill,
        entry_time    = now,
        quantity      = quantity,
        stop_loss     = round(fill - sl_offset,     2),
        target        = round(fill + target_offset, 2),
        sl_offset     = sl_offset,
        target_offset = target_offset,
        order_id      = order_id,
        status        = PositionStatus.OPEN,
        strategy      = strategy,
        # Monotonic clock so the scalper's max-hold time stop can't be warped by
        # a wall-clock adjustment. Not persisted (see Position.opened_at).
        opened_at     = time.monotonic(),
    )

    st = get_state()
    st.positions[symbol]    = pos
    st.traded_today.add(symbol)

    print(
        f"[{'SCALP' if strategy == STRATEGY_SCALP else 'PAPER'}] "
        f"BUY {symbol} × {quantity} @ {fill:.2f} | "
        f"SL={pos.stop_loss:.2f}  TGT={pos.target:.2f}  id={order_id}"
    )
    return pos


def _finalize(pos: Position, exit_price: float, label: str,
              slip: bool = True) -> Position:
    """Close a position at exit_price (slipped downward, matching the
    backtest's sell fills), record net P&L (after costs), and move it out of
    the open book. slip=False for synthetic prices (the EOD entry-price
    fallback when no market price was ever observed) — slipping a fabricated
    price would book a fabricated extra loss."""
    st         = get_state()
    if slip:
        exit_price = _slip_sell(exit_price)
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

    # Scalp book accounting, kept HERE rather than in the scalp engine because a
    # scalp position can be closed from four places (tick SL/target, the time
    # stop, the 14:45 square-off, the 15:30 EOD flat) and every one of them funds
    # the scalper's own loss limit and re-entry cooldown. Anywhere else, one of
    # those paths would silently skip the accounting.
    if pos.strategy == STRATEGY_SCALP:
        st.scalp_pnl += pnl
        st.scalp_last_exit[pos.symbol] = time.monotonic()

    # Move out of the open book so `positions` stays a true concurrent set.
    # `traded_today` still blocks same-day re-entry.
    st.positions.pop(pos.symbol, None)
    st.closed_positions.append(pos)

    # Prefix by owning strategy so a session's scalp activity is greppable as one
    # stream ([SCALP] entries AND exits) rather than split across two tags.
    print(
        f"[{'SCALP' if pos.strategy == STRATEGY_SCALP else 'PAPER'}] "
        f"{label} {pos.symbol} @ {exit_price:.2f} | "
        f"gross ₹{gross:+.2f}  costs ₹{costs:.2f}  net ₹{pnl:+.2f}  (daily ₹{st.daily_pnl:+.2f})"
    )
    return pos


def check_tick_exit(symbol: str, ltp: float) -> Optional[Position]:
    """
    Tick-wise exit: close the position the instant the live price touches the
    stop-loss or target. Filled at the actual tick price (which may be past the
    level — captures gap-through risk, matching the backtest's gap handling).
    Returns the closed Position or None. SL takes precedence if both are within
    reach on the same tick.
    """
    pos = get_state().positions.get(symbol)
    if pos is None or pos.status != PositionStatus.OPEN:
        return None

    if ltp <= pos.stop_loss:
        return _finalize(pos, ltp, "SL HIT")
    if ltp >= pos.target:
        return _finalize(pos, ltp, "TARGET HIT")
    return None


def force_close(symbol: str, exit_price: float, synthetic: bool = False,
                label: str = "EOD SQUARE-OFF") -> Optional[Position]:
    """Square off an open position at a given price (used for the 15:30 EOD
    flat). synthetic=True when exit_price is a FALLBACK (entry price, no
    market price ever observed) — skips the sell-side slippage so the fake
    price doesn't also book a fake slippage loss. `label` names the reason in
    the log (the scalper passes its own: time stop, 14:45 square-off)."""
    pos = get_state().positions.get(symbol)
    if pos is None or pos.status != PositionStatus.OPEN:
        return None
    return _finalize(pos, exit_price, label, slip=not synthetic)
