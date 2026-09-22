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
from app.engine.bn_entry_exit import (
    ExitEvaluation,
    _in_trading_window,
    evaluate_exit,
    finalize_exit,
    open_trade_from_signal,
)
from app.engine.bn_pricing import black_scholes, estimate_iv, get_itm_strike, get_next_expiry, time_to_expiry_years
from app.models import BNSignal, BNTrade, PositionStatus, closed_tail_closes
from app.state import get_state

_order_seq = itertools.count(1)


def place_paper_order(signal: BNSignal, now: datetime) -> BNTrade:
    """
    Open the single active trade from a fired BNSignal. Returns it (already
    added to AppState). Re-checks the trading-window and SCALP_MAX_TRADES_
    PER_DAY guardrails here too (not just inside evaluate_entry) so they
    truly cap every executed trade regardless of origin — including
    place_manual_order below, which bypasses evaluate_entry's gates
    entirely (a human decision, not an algo signal) but must not bypass
    these risk limits (config.py's "Risk guardrails" comment already
    claimed this was enforced here — this closes a real gap where only the
    max-trades check actually was).
    """
    st = get_state()
    if not _in_trading_window(now):
        raise ValueError("Outside scalp trading window (09:45-11:15 / 13:45-14:45 IST)")
    if st.bn_trades_today >= cfg.SCALP_MAX_TRADES_PER_DAY:
        raise ValueError(f"Max {cfg.SCALP_MAX_TRADES_PER_DAY} trades/day reached")

    order_id = f"BN-{now.strftime('%H%M%S')}-{next(_order_seq)}"
    trade = open_trade_from_signal(signal, now, order_id)

    st.active_trade = trade
    st.bn_trades_today += 1
    # Real-option-LTP subscription/override DISABLED for the scalp strategy
    # (2026-09-22, explicit user decision) — see check_tick_exit's docstring
    # below for the full incident writeup. trade.option_symbol is still
    # computed/displayed (a useful label for which contract this models),
    # just never subscribed to for a live tick any more.

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
    # closed_tail_closes() excludes the still-forming bar — a manual
    # entry's premium must be computed on the same basis as the exit-tick
    # checks that will later compare against it, or the tight ₹ target/
    # stop bracket gets blown through by pure IV-estimate noise, not real
    # movement.
    lookback = closed_tail_closes(bn_candles, cfg.BN_IV_LOOKBACK_BARS)

    spot = st.bn_index_ltp
    option_type = "CE" if direction == "BUY" else "PE"
    # Deep-ITM, same strike selection the algo strategy uses (2026-09-21) —
    # a manual order shares the same premium-based target/stop economics
    # (BN_SCALP_TARGET_RS/STOP_RS), which only make sense on a similarly
    # deep-ITM contract, not an ATM one with very different premium scale.
    strike = get_itm_strike(spot, option_type, cfg.BN_ITM_OFFSET_POINTS)
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
    Tick-wise exit: close the trade the instant target/stop/time-scratch is
    touched. Returns the closed trade, or None if still open (or nothing is
    open).

    ALWAYS uses evaluate_exit's own synthetic Black-Scholes mark — no real-
    option-LTP override (removed 2026-09-22, explicit user decision). That
    override let a real market tick snap the price straight past the
    strategy's whole ±₹2-3 target/stop bracket the instant one arrived
    (root cause of a real observed bug: same-second entry/exit at a huge,
    inconsistent loss with the index barely moving) — a discontinuity the
    old, much wider index-points bracket could absorb but this one can't.
    Settlement is now consistently synthetic for a trade's entire life,
    matching the basis the bracket was actually calibrated against.
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
    button; same target/stop-agnostic close either way, only the log label differs).
    See check_tick_exit's docstring — always synthetic, no real-LTP override."""
    st = get_state()
    trade = st.active_trade
    if trade is None or trade.status != PositionStatus.OPEN:
        return None
    ev = evaluate_exit(trade, now, current_index_price, bn_closes_lookback)
    return _settle(trade, now, current_index_price, ev.current_premium, label)
