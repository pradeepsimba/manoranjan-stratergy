from __future__ import annotations

"""
Bank Nifty paper-trading order simulator. No broker connection — the ATM
option leg is entirely synthetic (Black-Scholes over BankNifty spot, see
bn_pricing.py); this module only manages the single active trade's lifecycle
and the paper-account bookkeeping (daily_pnl resets every EOD, funds persists
across days).
"""

import itertools
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

import app.config as cfg
from app.engine.bn_entry_exit import (
    ExitEvaluation,
    _in_trading_window,
    _max_trades_ok,
    evaluate_exit,
    fill_delayed_entry,
    finalize_exit,
    open_trade_from_signal,
    resolve_delayed_exit_premium,
)
from app.engine.bn_pricing import black_scholes, estimate_iv, get_itm_strike, get_next_expiry, time_to_expiry_years
from app.engine.risk_guardrails import trading_window_description as _trading_window_description
from app.models import BNSignal, BNTrade, PendingBNEntry, PositionStatus, TradingPhase, closed_tail_closes, iv_lookback_closes
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
        raise ValueError(f"Outside scalp trading window ({_trading_window_description()})")
    if not _max_trades_ok(st.trades_today_combined):
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


def arm_pending_entry(signal: BNSignal, now: datetime) -> None:
    """
    A fired algo signal does NOT fill instantly (2026-09-24, explicit user
    decision: "300ms Entry Delay: Pause 300ms after a signal to simulate API
    lag"). Parks it as st.pending_entry instead of calling place_paper_order
    directly — scheduler.py's _tick_entries blocks further evaluate_entry
    calls while this is set (same as the existing active_trade is not None
    guard), and retries the fill every subsequent tick via
    try_fill_pending_entry below. Only ever called from the algo path — a
    manual order (place_manual_order below) still fills immediately, a human
    decision, not a signal this feature models lag for.
    """
    st = get_state()
    delay_ms = cfg.BN_ENTRY_FILL_DELAY_MS
    st.pending_entry = PendingBNEntry(
        signal=signal,
        armed_at=now.isoformat(),
        fill_after=(now + timedelta(milliseconds=delay_ms)).isoformat(),
        tick_seq_at_arm=st.bn_index_tick_seq,
    )
    print(f"[PAPER] {signal.direction} signal armed @ {signal.entry_index_price:.2f} — "
          f"filling in {delay_ms:.0f}ms at the next live tick (simulated entry lag)")


def try_fill_pending_entry(now: datetime, current_index_price: float,
                           bn_closes_lookback: np.ndarray) -> Optional[BNTrade]:
    """
    Called every tick while st.pending_entry is set. Returns the newly-opened
    BNTrade once it actually fills — BN_ENTRY_FILL_DELAY_MS has elapsed AND
    either a genuinely new live tick has been observed since the signal was
    armed (realistic slippage: fills at THAT tick's price, not the signal's)
    or BN_FILL_MAX_WAIT_MS has passed since with no new tick (feed
    momentarily idle — fill at whatever price is current rather than
    stalling the single-trade slot indefinitely). Returns None while still
    waiting, or if the fill-time guardrail re-check (trading window/max
    trades — same as place_paper_order always re-checks) rejects it.
    """
    st = get_state()
    pending = st.pending_entry
    if pending is None:
        return None

    fill_after = datetime.fromisoformat(pending.fill_after)
    if now < fill_after:
        return None
    tick_advanced = st.bn_index_tick_seq > pending.tick_seq_at_arm
    max_wait_elapsed = (now - fill_after).total_seconds() * 1000.0 >= cfg.BN_FILL_MAX_WAIT_MS
    if not tick_advanced and not max_wait_elapsed:
        return None

    st.pending_entry = None
    if current_index_price <= 0:
        print("BN pending entry abandoned — no live price at fill time")
        return None

    filled_signal = fill_delayed_entry(pending.signal, now, current_index_price, bn_closes_lookback)
    try:
        return place_paper_order(filled_signal, now)
    except ValueError as e:
        print(f"BN order rejected at fill time: {e}")
        return None


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
    # Same session gate the automated _tick_entries applies (found in
    # review, 2026-09-23) — without this, a manual order could be placed
    # outside 09:30-15:00 (e.g. CLOSED overnight), opening a trade off a
    # stale st.bn_index_ltp (never reset at EOD) with no _tick_exits loop
    # running to ever close it until the next day's ACTIVE phase.
    if st.phase != TradingPhase.ACTIVE:
        raise ValueError("Manual orders are only allowed during the active trading session (09:30-15:00 IST).")
    if st.active_trade is not None:
        raise ValueError("A trade is already active — exit it before placing a new one.")
    # 2026-09-24 fix, found in review: an algo signal can be armed
    # (st.pending_entry set) while st.active_trade is still None — without
    # this guard a manual order placed in that window would set
    # st.active_trade directly, and _tick_entries' `active_trade is not
    # None` check would then skip resolving the pending entry entirely
    # until the manual trade closes, at which point the now-stale armed
    # signal (direction/strike/expiry frozen from potentially minutes
    # earlier) would fire unexpectedly as a surprise trade.
    if st.pending_entry is not None:
        raise ValueError("An algo entry signal is currently pending fill — try again in a moment.")
    if st.bn_index_ltp <= 0:
        raise ValueError("No live BankNifty price yet.")

    with st._bn_index_lock:
        bn_candles = list(st.bn_index_candles_5m)
    # closed_tail_closes() excludes the still-forming bar — a manual
    # entry's premium must be computed on the same basis as the exit-tick
    # checks that will later compare against it, or the tight ₹ target/
    # stop bracket gets blown through by pure IV-estimate noise, not real
    # movement.
    lookback = iv_lookback_closes(bn_candles, cfg.BN_IV_LOOKBACK_BARS)

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
    # Persist the FINAL outcome label onto the trade (2026-09-24, found in
    # review) — `label` used to only ever reach the print() below and then
    # vanish; nothing downstream (dashboard, DB) could ever show WHY a
    # trade closed. See models.py's BNTrade.exit_reason comment.
    trade.exit_reason = label
    # Clear any armed pending-exit bookkeeping (found redundant otherwise —
    # the trade is CLOSED either way now, whether it got here via a filled
    # pending exit or an unconditional force_close that pre-empted one).
    trade.pending_exit_reason = None
    trade.pending_exit_fill_after = None
    trade.pending_exit_tick_seq = None
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

    Does NOT settle the instant target/stop/time-scratch first triggers
    (2026-09-24, explicit user decision: "200ms Exit Delay: Add a 200ms lag
    when closing a trade") — the first trigger ARMS trade.pending_exit_*
    (see _arm_pending_exit) and every subsequent tick retries the actual
    fill via _try_fill_pending_exit, symmetric with the entry-side delay in
    try_fill_pending_entry above: the settlement premium comes from the next
    genuinely-new live tick after the delay, not the premium at the instant
    the condition first fired. Only this automatic path is delayed —
    force_close (EOD square-off / manual Exit) below always settles
    immediately, unconditionally.
    """
    st = get_state()
    trade = st.active_trade
    if trade is None or trade.status != PositionStatus.OPEN:
        return None

    if trade.pending_exit_reason is not None:
        return _try_fill_pending_exit(trade, now, current_index_price, bn_closes_lookback)

    ev: ExitEvaluation = evaluate_exit(trade, now, current_index_price, bn_closes_lookback)
    trade.current_sl = ev.new_sl
    trade.sl_stage = ev.sl_stage
    trade.current_premium = ev.current_premium   # live mark for the ATM panel, even when not exiting
    trade.current_iv = ev.current_iv

    if ev.should_exit:
        _arm_pending_exit(trade, now, ev.exit_reason)
    return None


def _arm_pending_exit(trade: BNTrade, now: datetime, reason: str) -> None:
    st = get_state()
    delay_ms = cfg.BN_EXIT_FILL_DELAY_MS
    trade.pending_exit_reason = reason
    trade.pending_exit_fill_after = (now + timedelta(milliseconds=delay_ms)).isoformat()
    trade.pending_exit_tick_seq = st.bn_index_tick_seq
    print(f"[PAPER] {reason} triggered for {trade.direction} {trade.option_type} {trade.strike} "
          f"— filling in {delay_ms:.0f}ms at the next live tick (simulated exit lag)")


def _try_fill_pending_exit(trade: BNTrade, now: datetime, current_index_price: float,
                          bn_closes_lookback: np.ndarray) -> Optional[BNTrade]:
    st = get_state()
    fill_after = datetime.fromisoformat(trade.pending_exit_fill_after)
    if now < fill_after:
        return None
    tick_advanced = st.bn_index_tick_seq > trade.pending_exit_tick_seq
    max_wait_elapsed = (now - fill_after).total_seconds() * 1000.0 >= cfg.BN_FILL_MAX_WAIT_MS
    if not tick_advanced and not max_wait_elapsed:
        return None

    reason = trade.pending_exit_reason
    premium = resolve_delayed_exit_premium(trade, now, current_index_price, bn_closes_lookback)
    return _settle(trade, now, current_index_price, premium, f"{reason} HIT")


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
