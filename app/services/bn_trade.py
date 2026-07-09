from __future__ import annotations

"""
Bank Nifty paper-trading order simulator. No broker connection — the ATM
option leg is entirely synthetic (Black-Scholes over BankNifty spot, see
bn_pricing.py); this module only manages the single active trade's lifecycle
and the paper-account bookkeeping (daily_pnl resets every EOD, funds persists
across days).
"""

import itertools
from datetime import datetime
from typing import Optional

import numpy as np

from app.engine.bn_entry_exit import ExitEvaluation, evaluate_exit, finalize_exit, open_trade_from_signal
from app.models import BNSignal, BNTrade, PositionStatus
from app.state import get_state

_order_seq = itertools.count(1)


def place_paper_order(signal: BNSignal, now: datetime) -> BNTrade:
    """Open the single active trade from a fired BNSignal. Returns it (already added to AppState)."""
    order_id = f"BN-{now.strftime('%H%M%S')}-{next(_order_seq)}"
    trade = open_trade_from_signal(signal, now, order_id)

    st = get_state()
    st.active_trade = trade
    st.last_trade_candle = signal.bar_time

    print(
        f"[PAPER] {trade.direction} {trade.option_type} {trade.strike} @ premium "
        f"{trade.entry_premium:.2f} | index {trade.entry_index_price:.2f} | "
        f"SL={trade.current_sl:.2f} TGT={trade.target:.2f} id={order_id}"
    )
    return trade


def _settle(trade: BNTrade, now: datetime, exit_index_price: float,
           exit_premium: float, label: str) -> BNTrade:
    finalize_exit(trade, now, exit_index_price, exit_premium)
    st = get_state()
    st.daily_pnl += trade.pnl
    st.funds += trade.pnl
    st.active_trade = None
    st.closed_trades.append(trade)
    st.last_exit_time = now.isoformat()
    print(
        f"[PAPER] {label} {trade.direction} {trade.option_type} {trade.strike} @ premium "
        f"{exit_premium:.2f} | net ₹{trade.pnl:+.2f} (daily ₹{st.daily_pnl:+.2f}, "
        f"funds ₹{st.funds:,.2f})"
    )
    return trade


def check_tick_exit(now: datetime, current_index_price: float,
                    bn_closes_lookback: np.ndarray) -> Optional[BNTrade]:
    """
    Tick-wise exit: ratchet the trailing/breakeven stop and close the trade the
    instant target/stop is touched. Returns the closed trade, or None if still
    open (or nothing is open).
    """
    st = get_state()
    trade = st.active_trade
    if trade is None or trade.status != PositionStatus.OPEN:
        return None

    ev: ExitEvaluation = evaluate_exit(trade, now, current_index_price, bn_closes_lookback)
    trade.current_sl = ev.new_sl
    trade.sl_stage = ev.sl_stage
    trade.current_premium = ev.current_premium   # live mark for the ATM panel, even when not exiting
    trade.current_iv = ev.current_iv

    if ev.should_exit:
        return _settle(trade, now, current_index_price, ev.current_premium, f"{ev.exit_reason} HIT")
    return None


def force_close(now: datetime, current_index_price: float,
                bn_closes_lookback: np.ndarray) -> Optional[BNTrade]:
    """Square off the active trade unconditionally (used for the 15:30 EOD flat)."""
    st = get_state()
    trade = st.active_trade
    if trade is None or trade.status != PositionStatus.OPEN:
        return None
    ev = evaluate_exit(trade, now, current_index_price, bn_closes_lookback)
    return _settle(trade, now, current_index_price, ev.current_premium, "EOD SQUARE-OFF")
