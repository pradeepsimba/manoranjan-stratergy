from __future__ import annotations

"""
Nifty 50 paper-trading order simulator — mechanical mirror of bn_trade.py.
Credits/debits the SHARED paper account (st.funds/st.daily_pnl) — BankNifty
and Nifty 50 are two strategies on one account, not two separate pots (an
explicit design decision — see the plan). Per-instrument strategy state
(active_trade_nf/closed_trades_nf/etc.) stays fully separate from BN's.
"""

import itertools
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

import app.config as cfg
from app.engine.nf_entry_exit import (
    ExitEvaluation,
    _in_trading_window,
    _max_trades_ok,
    evaluate_exit,
    fill_delayed_entry,
    finalize_exit,
    open_trade_from_signal,
    resolve_delayed_exit_premium,
)
from app.engine.nf_pricing import black_scholes, estimate_iv, get_itm_strike, get_next_expiry, time_to_expiry_years
from app.engine.risk_guardrails import trading_window_description as _trading_window_description
from app.models import NFSignal, NFTrade, PendingNFEntry, PositionStatus, TradingPhase, closed_tail_closes, iv_lookback_closes
from app.state import get_state

_order_seq = itertools.count(1)


def place_paper_order(signal: NFSignal, now: datetime) -> NFTrade:
    """
    Open the single active NF trade from a fired NFSignal. Returns it
    (already added to AppState). See bn_trade.place_paper_order for why
    the trading-window and SCALP_MAX_TRADES_PER_DAY guardrails are
    re-checked here too, not just inside evaluate_entry.
    """
    st = get_state()
    if not _in_trading_window(now):
        raise ValueError(f"Outside scalp trading window ({_trading_window_description()})")
    if not _max_trades_ok(st.trades_today_combined):
        raise ValueError(f"Max {cfg.SCALP_MAX_TRADES_PER_DAY} trades/day reached")

    order_id = f"NF-{now.strftime('%H%M%S')}-{next(_order_seq)}"
    trade = open_trade_from_signal(signal, now, order_id)

    st.active_trade_nf = trade
    st.nf_trades_today += 1
    # Real-option-LTP subscription/override DISABLED for the scalp strategy —
    # see bn_trade.check_tick_exit's docstring for why (NF mirror).

    print(
        f"[PAPER][NF] {trade.direction} {trade.option_type} {trade.strike} @ premium "
        f"{trade.entry_premium:.2f} | index {trade.entry_index_price:.2f} | "
        f"SL={trade.current_sl:.2f} TGT={trade.target:.2f} id={order_id}"
    )
    return trade


def arm_pending_entry(signal: NFSignal, now: datetime) -> None:
    """NF mirror of bn_trade.arm_pending_entry — see there."""
    st = get_state()
    delay_ms = cfg.NF_ENTRY_FILL_DELAY_MS
    st.pending_entry_nf = PendingNFEntry(
        signal=signal,
        armed_at=now.isoformat(),
        fill_after=(now + timedelta(milliseconds=delay_ms)).isoformat(),
        tick_seq_at_arm=st.nf_index_tick_seq,
    )
    print(f"[PAPER][NF] {signal.direction} signal armed @ {signal.entry_index_price:.2f} — "
          f"filling in {delay_ms:.0f}ms at the next live tick (simulated entry lag)")


def try_fill_pending_entry(now: datetime, current_index_price: float,
                           nf_closes_lookback: np.ndarray) -> Optional[NFTrade]:
    """NF mirror of bn_trade.try_fill_pending_entry — see there."""
    st = get_state()
    pending = st.pending_entry_nf
    if pending is None:
        return None

    fill_after = datetime.fromisoformat(pending.fill_after)
    if now < fill_after:
        return None
    tick_advanced = st.nf_index_tick_seq > pending.tick_seq_at_arm
    max_wait_elapsed = (now - fill_after).total_seconds() * 1000.0 >= cfg.NF_FILL_MAX_WAIT_MS
    if not tick_advanced and not max_wait_elapsed:
        return None

    st.pending_entry_nf = None
    if current_index_price <= 0:
        print("NF pending entry abandoned — no live price at fill time")
        return None

    filled_signal = fill_delayed_entry(pending.signal, now, current_index_price, nf_closes_lookback)
    try:
        return place_paper_order(filled_signal, now)
    except ValueError as e:
        print(f"NF order rejected at fill time: {e}")
        return None


def place_manual_order(direction: str, now: datetime) -> NFTrade:
    """NF mirror of bn_trade.place_manual_order — see there for the full explanation."""
    if direction not in ("BUY", "SELL"):
        raise ValueError("direction must be BUY or SELL")
    st = get_state()
    # See bn_trade.place_manual_order's identical comment — same session
    # gate the automated _tick_entries_nf applies (found in review, 2026-09-23).
    if st.phase != TradingPhase.ACTIVE:
        raise ValueError("Manual orders are only allowed during the active trading session (09:30-15:00 IST).")
    if st.active_trade_nf is not None:
        raise ValueError("A trade is already active — exit it before placing a new one.")
    # See bn_trade.place_manual_order's identical 2026-09-24 fix (found in
    # review) — guards against an armed-but-not-yet-filled algo signal
    # (st.pending_entry_nf) getting orphaned by a manual order that slips
    # into st.active_trade_nf while pending_entry_nf is still set.
    if st.pending_entry_nf is not None:
        raise ValueError("An algo entry signal is currently pending fill — try again in a moment.")
    if st.nf_index_ltp <= 0:
        raise ValueError("No live Nifty 50 price yet.")

    with st._nf_index_lock:
        nf_candles = list(st.nf_index_candles_5m)
    # See bn_trade.place_manual_order's identical comment —
    # closed_tail_closes() excludes the still-forming bar so a manual
    # entry's premium is computed on the same basis the exit-tick checks
    # will later compare against.
    lookback = iv_lookback_closes(nf_candles, cfg.NF_IV_LOOKBACK_BARS)

    spot = st.nf_index_ltp
    option_type = "CE" if direction == "BUY" else "PE"
    # Deep-ITM, same strike selection the algo strategy uses — see
    # bn_trade.place_manual_order's equivalent comment.
    strike = get_itm_strike(spot, option_type, cfg.NF_ITM_OFFSET_POINTS)
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


def _settle(trade: NFTrade, now: datetime, exit_index_price: float,
           exit_premium: float, label: str) -> NFTrade:
    finalize_exit(trade, now, exit_index_price, exit_premium)
    # See bn_trade._settle's identical comment — clears any armed
    # pending-exit bookkeeping now that the trade is CLOSED either way.
    trade.pending_exit_reason = None
    trade.pending_exit_fill_after = None
    trade.pending_exit_tick_seq = None
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
    """NF mirror of bn_trade.check_tick_exit — always synthetic, no real-LTP
    override; same 2026-09-24 pending-exit fill-delay arm/retry pattern."""
    st = get_state()
    trade = st.active_trade_nf
    if trade is None or trade.status != PositionStatus.OPEN:
        return None

    if trade.pending_exit_reason is not None:
        return _try_fill_pending_exit(trade, now, current_index_price, nf_closes_lookback)

    ev: ExitEvaluation = evaluate_exit(trade, now, current_index_price, nf_closes_lookback)
    trade.current_sl = ev.new_sl
    trade.sl_stage = ev.sl_stage
    trade.current_premium = ev.current_premium
    trade.current_iv = ev.current_iv

    if ev.should_exit:
        _arm_pending_exit(trade, now, ev.exit_reason)
    return None


def _arm_pending_exit(trade: NFTrade, now: datetime, reason: str) -> None:
    st = get_state()
    delay_ms = cfg.NF_EXIT_FILL_DELAY_MS
    trade.pending_exit_reason = reason
    trade.pending_exit_fill_after = (now + timedelta(milliseconds=delay_ms)).isoformat()
    trade.pending_exit_tick_seq = st.nf_index_tick_seq
    print(f"[PAPER][NF] {reason} triggered for {trade.direction} {trade.option_type} {trade.strike} "
          f"— filling in {delay_ms:.0f}ms at the next live tick (simulated exit lag)")


def _try_fill_pending_exit(trade: NFTrade, now: datetime, current_index_price: float,
                          nf_closes_lookback: np.ndarray) -> Optional[NFTrade]:
    st = get_state()
    fill_after = datetime.fromisoformat(trade.pending_exit_fill_after)
    if now < fill_after:
        return None
    tick_advanced = st.nf_index_tick_seq > trade.pending_exit_tick_seq
    max_wait_elapsed = (now - fill_after).total_seconds() * 1000.0 >= cfg.NF_FILL_MAX_WAIT_MS
    if not tick_advanced and not max_wait_elapsed:
        return None

    reason = trade.pending_exit_reason
    premium = resolve_delayed_exit_premium(trade, now, current_index_price, nf_closes_lookback)
    return _settle(trade, now, current_index_price, premium, f"{reason} HIT")


def force_close(now: datetime, current_index_price: float,
                nf_closes_lookback: np.ndarray, label: str = "EOD SQUARE-OFF") -> Optional[NFTrade]:
    """NF mirror of bn_trade.force_close (used for the 15:30 EOD flat, and
    — with label="MANUAL EXIT" — the dashboard's manual Exit button)."""
    st = get_state()
    trade = st.active_trade_nf
    if trade is None or trade.status != PositionStatus.OPEN:
        return None
    ev = evaluate_exit(trade, now, current_index_price, nf_closes_lookback)
    return _settle(trade, now, current_index_price, ev.current_premium, label)
