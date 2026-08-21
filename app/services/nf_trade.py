from __future__ import annotations

"""
Nifty 50 paper-trading order simulator — mechanical mirror of bn_trade.py.
Credits/debits the SHARED paper account (st.funds/st.daily_pnl) — BankNifty
and Nifty 50 are two strategies on one account, not two separate pots (an
explicit design decision — see the plan). Per-instrument strategy state
(active_trade_nf/closed_trades_nf/etc.) stays fully separate from BN's.
"""

import itertools
from datetime import datetime
from typing import Optional

import numpy as np

from app.engine.nf_entry_exit import ExitEvaluation, evaluate_exit, finalize_exit, open_trade_from_signal
from app.models import NFSignal, NFTrade, PositionStatus
from app.state import get_state

_order_seq = itertools.count(1)


def place_paper_order(signal: NFSignal, now: datetime) -> NFTrade:
    """Open the single active NF trade from a fired NFSignal. Returns it (already added to AppState)."""
    order_id = f"NF-{now.strftime('%H%M%S')}-{next(_order_seq)}"
    trade = open_trade_from_signal(signal, now, order_id)

    st = get_state()
    st.active_trade_nf = trade
    st.last_trade_candle_nf = signal.bar_time

    print(
        f"[PAPER][NF] {trade.direction} {trade.option_type} {trade.strike} @ premium "
        f"{trade.entry_premium:.2f} | index {trade.entry_index_price:.2f} | "
        f"SL={trade.current_sl:.2f} TGT={trade.target:.2f} id={order_id}"
    )
    return trade


def _settle(trade: NFTrade, now: datetime, exit_index_price: float,
           exit_premium: float, label: str) -> NFTrade:
    finalize_exit(trade, now, exit_index_price, exit_premium)
    st = get_state()
    st.daily_pnl += trade.pnl
    st.funds += trade.pnl
    st.active_trade_nf = None
    st.closed_trades_nf.append(trade)
    st.last_exit_time_nf = now.isoformat()
    print(
        f"[PAPER][NF] {label} {trade.direction} {trade.option_type} {trade.strike} @ premium "
        f"{exit_premium:.2f} | net ₹{trade.pnl:+.2f} (daily ₹{st.daily_pnl:+.2f}, "
        f"funds ₹{st.funds:,.2f})"
    )
    return trade


def check_tick_exit(now: datetime, current_index_price: float,
                    nf_closes_lookback: np.ndarray) -> Optional[NFTrade]:
    """NF mirror of bn_trade.check_tick_exit."""
    st = get_state()
    trade = st.active_trade_nf
    if trade is None or trade.status != PositionStatus.OPEN:
        return None

    ev: ExitEvaluation = evaluate_exit(trade, now, current_index_price, nf_closes_lookback)
    trade.current_sl = ev.new_sl
    trade.sl_stage = ev.sl_stage
    trade.current_premium = ev.current_premium
    trade.current_iv = ev.current_iv

    if ev.should_exit:
        return _settle(trade, now, current_index_price, ev.current_premium, f"{ev.exit_reason} HIT")
    return None


def force_close(now: datetime, current_index_price: float,
                nf_closes_lookback: np.ndarray) -> Optional[NFTrade]:
    """NF mirror of bn_trade.force_close (used for the 15:30 EOD flat)."""
    st = get_state()
    trade = st.active_trade_nf
    if trade is None or trade.status != PositionStatus.OPEN:
        return None
    ev = evaluate_exit(trade, now, current_index_price, nf_closes_lookback)
    return _settle(trade, now, current_index_price, ev.current_premium, "EOD SQUARE-OFF")
