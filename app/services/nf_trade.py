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

import app.config as cfg
from app.engine.nf_entry_exit import ExitEvaluation, evaluate_exit, finalize_exit, open_trade_from_signal
from app.engine.nf_pricing import black_scholes, estimate_iv, get_atm_strike, get_next_expiry, time_to_expiry_years
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
    st.nf_option_ltp = None   # fresh — any stale value from a prior trade must not leak in
    # NF mirror of bn_trade.place_paper_order's real-option-LTP wiring —
    # stockname is the underlying's plain name ("NIFTY"), not the option
    # symbol repeated (see bn_trade.py's comment for why).
    if st.market_data_service is not None:
        st.market_data_service.set_nf_option_symbol(trade.option_symbol, cfg.NF_OPTION_UNDERLYING)

    print(
        f"[PAPER][NF] {trade.direction} {trade.option_type} {trade.strike} @ premium "
        f"{trade.entry_premium:.2f} | index {trade.entry_index_price:.2f} | "
        f"SL={trade.current_sl:.2f} TGT={trade.target:.2f} id={order_id}"
    )
    return trade


def place_manual_order(direction: str, now: datetime) -> NFTrade:
    """NF mirror of bn_trade.place_manual_order — see there for the full explanation."""
    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")
    st = get_state()
    if st.active_trade_nf is not None:
        raise ValueError("A trade is already active — exit it before placing a new one.")
    if st.nf_index_ltp <= 0:
        raise ValueError("No live Nifty 50 price yet.")

    with st._nf_index_lock:
        nf_candles = list(st.nf_index_candles_5m)
    closes = (np.fromiter((c.close for c in nf_candles), np.float64, len(nf_candles))
              if nf_candles else np.zeros(0, dtype=np.float64))
    lookback = closes[-cfg.NF_IV_LOOKBACK_BARS:] if closes.size > cfg.NF_IV_LOOKBACK_BARS else closes

    spot = st.nf_index_ltp
    option_type = "CE" if direction == "BUY" else "PE"
    strike = get_atm_strike(spot)
    expiry = get_next_expiry(now)
    T = time_to_expiry_years(now, expiry)
    iv = estimate_iv(lookback)
    bs = black_scholes(spot, strike, T, cfg.NF_RISK_FREE_RATE, iv, option_type)

    signal = NFSignal(
        direction=direction, entry_index_price=spot, bar_time=now.isoformat(),
        confidence=0.0, green=0, red=0, strong_qty=0, leader_signal="MANUAL",
        bn_bull=0.0, bn_bear=0.0, strike=strike, expiry=expiry.isoformat(),
        entry_premium=bs["price"], iv_used=iv,
    )
    return place_paper_order(signal, now)


def _live_premium(trade: NFTrade, st, bs_premium: float) -> float:
    """NF mirror of bn_trade._live_premium."""
    if trade.option_symbol and not trade.premium_synthetic and st.nf_option_ltp:
        return st.nf_option_ltp
    return bs_premium


def _settle(trade: NFTrade, now: datetime, exit_index_price: float,
           exit_premium: float, label: str) -> NFTrade:
    finalize_exit(trade, now, exit_index_price, exit_premium)
    st = get_state()
    st.daily_pnl += trade.pnl
    st.funds += trade.pnl
    st.active_trade_nf = None
    st.closed_trades_nf.append(trade)
    st.last_exit_time_nf = now.isoformat()
    st.nf_option_ltp = None
    if st.market_data_service is not None:
        st.market_data_service.set_nf_option_symbol(None, None)
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
    premium = _live_premium(trade, st, ev.current_premium)
    trade.current_sl = ev.new_sl
    trade.sl_stage = ev.sl_stage
    trade.current_premium = premium
    trade.current_iv = ev.current_iv

    if ev.should_exit:
        return _settle(trade, now, current_index_price, premium, f"{ev.exit_reason} HIT")
    return None


def force_close(now: datetime, current_index_price: float,
                nf_closes_lookback: np.ndarray, label: str = "EOD SQUARE-OFF") -> Optional[NFTrade]:
    """NF mirror of bn_trade.force_close (used for the 15:30 EOD flat, and
    — with label="MANUAL EXIT" — the dashboard's manual Exit button)."""
    st = get_state()
    trade = st.active_trade_nf
    if trade is None or trade.status != PositionStatus.OPEN:
        return None
    ev = evaluate_exit(trade, now, current_index_price, nf_closes_lookback)
    return _settle(trade, now, current_index_price, _live_premium(trade, st, ev.current_premium), label)
