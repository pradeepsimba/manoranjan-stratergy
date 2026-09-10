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

import app.config as cfg
from app.engine.bn_entry_exit import ExitEvaluation, evaluate_exit, finalize_exit, open_trade_from_signal
from app.engine.bn_pricing import black_scholes, estimate_iv, get_atm_strike, get_next_expiry, time_to_expiry_years
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


def place_manual_order(direction: str, now: datetime) -> BNTrade:
    """
    Manual override from the dashboard's Kite-style order form: places the
    single active trade directly from a human's BUY/SELL click, bypassing
    evaluate_entry's strategy gates entirely (this is a trading-desk
    decision, not an algo signal — same distinction a real trader's manual
    order vs. an algo fill would have). Still goes through
    open_trade_from_signal/place_paper_order so the resulting BNTrade
    freezes its risk parameters from cfg exactly like an algo-fired trade,
    and lot size is always cfg.BN_LOT_SIZE (open_trade_from_signal ignores
    any other quantity — this engine has no position-sizing concept, see
    CLAUDE.md's "Options are cash-only, always exactly 1 lot").

    Raises ValueError on anything that would make the resulting trade
    meaningless (already an open trade, no live price yet) — the caller
    (the /api/manual-order endpoint) turns that into an HTTP 400.
    """
    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")
    st = get_state()
    if st.active_trade is not None:
        raise ValueError("A trade is already active — exit it before placing a new one.")
    if st.bn_index_ltp <= 0:
        raise ValueError("No live BankNifty price yet.")

    with st._bn_index_lock:
        bn_candles = list(st.bn_index_candles_5m)
    closes = (np.fromiter((c.close for c in bn_candles), np.float64, len(bn_candles))
              if bn_candles else np.zeros(0, dtype=np.float64))
    lookback = closes[-cfg.BN_IV_LOOKBACK_BARS:] if closes.size > cfg.BN_IV_LOOKBACK_BARS else closes

    spot = st.bn_index_ltp
    option_type = "CE" if direction == "BUY" else "PE"
    strike = get_atm_strike(spot)
    expiry = get_next_expiry(now)
    T = time_to_expiry_years(now, expiry)
    iv = estimate_iv(lookback)
    bs = black_scholes(spot, strike, T, cfg.BN_RISK_FREE_RATE, iv, option_type)

    signal = BNSignal(
        direction=direction, entry_index_price=spot, bar_time=now.isoformat(),
        confidence=0.0, green=0, red=0, strong_qty=0, leader_signal="MANUAL",
        bn_bull=0.0, bn_bear=0.0, strike=strike, expiry=expiry.isoformat(),
        entry_premium=bs["price"], iv_used=iv,
    )
    return place_paper_order(signal, now)


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
                bn_closes_lookback: np.ndarray, label: str = "EOD SQUARE-OFF") -> Optional[BNTrade]:
    """Square off the active trade unconditionally (used for the 15:30 EOD
    flat, and — with label="MANUAL EXIT" — the dashboard's manual Exit
    button; same target/stop-agnostic close either way, only the log label differs)."""
    st = get_state()
    trade = st.active_trade
    if trade is None or trade.status != PositionStatus.OPEN:
        return None
    ev = evaluate_exit(trade, now, current_index_price, bn_closes_lookback)
    return _settle(trade, now, current_index_price, ev.current_premium, label)
