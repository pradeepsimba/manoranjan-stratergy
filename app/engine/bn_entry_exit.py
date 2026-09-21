from __future__ import annotations

"""
Shared Bank Nifty entry/exit decision core — called identically by the live
scheduler and the backtest replay loop (this repo's hard convention: live and
backtest must share one strategy core).

Originally ported from c.html's checkTradeEntry/checkExit (sideways-range +
momentum + leader-vote + volume-surge + RSI/MACD/EMA composite-indicator
gates). evaluate_entry's CONDITION was replaced entirely on 2026-09-19
(explicit user decision) with a much simpler rule — see its docstring below
— but evaluate_exit/open_trade_from_signal/finalize_exit (target/stop/
breakeven/trailing management) are UNCHANGED c.html ports; only how a trade
gets OPENED changed, not how an open trade is managed.

One deliberate deviation from c.html retained from the original port:
  * No JS "pending signal" pre-qualification latch — the caller is expected
    to invoke evaluate_entry exactly once per newly-closed 5m bar (wall-clock
    bar-close detection lives in the caller), which achieves the same
    "fire right at candle close" outcome without extra state to keep in sync.

_leader_qty_surge/_stock_qty_threshold below are NOT part of evaluate_entry
any more — they're kept only because scheduler.py separately uses them to
annotate the (informational, non-decision) stockCandles payload's "surged"
flag for the leader stocks.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

import app.config as cfg
from app.engine.bn_pricing import (
    black_scholes,
    build_monthly_option_symbol,
    estimate_iv,
    get_atm_strike,
    get_next_expiry,
    time_to_expiry_years,
)
from app.models import BNDiagnostic, BNSignal, BNTrade, Candle, PositionStatus


def _stock_qty_threshold(name: str) -> float:
    """
    A leader stock's live volume-surge threshold: its dynamic
    BN_QTY_THRESHOLD_* setting * cfg.BN_QTY_INTERVAL_MULTIPLIER (both
    live-editable from the Settings page — see cfg.BN_QTY_THRESHOLD_ATTR).
    Shared by _leader_qty_surge (latest bar only) and scheduler.py's
    per-historical-bar "surged" flag on the stockCandles payload (Big
    Trades table), so both read the identical threshold.
    """
    attr = cfg.BN_QTY_THRESHOLD_ATTR.get(name)
    base = getattr(cfg, attr) if attr else 10_000
    return base * cfg.BN_QTY_INTERVAL_MULTIPLIER


def _leader_qty_surge(leader_recent: Dict[str, List[Candle]]) -> Dict[str, bool]:
    """
    Per-stock volume-surge check: the latest bar's cumulative traded volume
    (Candle.volume — the same figure shown in the Entry Loop Monitor's
    VOLUME column and the Big Trades panel, both sourced from this same
    field) vs. _stock_qty_threshold(name). No averaging, no history window.

    Deliberate deviation from c.html's original single-latest-trade-qty
    check: this vendor's per-trade qty field (Candle.last_qty) runs 1-380
    on this feed, while c.html's STOCK_QTY_THRESHOLD table (900-2000) was
    calibrated for a very different qty scale — comparing it against
    last_qty made this gate an almost permanent no-op. Bar volume is the
    metric the user confirmed should drive this gate instead (2026-07-27).
    """
    out: Dict[str, bool] = {}
    for name, candles in leader_recent.items():
        out[name] = bool(candles) and candles[-1].volume >= _stock_qty_threshold(name)
    return out


def evaluate_entry(
    now: datetime,
    bn_recent_candles: List[Candle],
    bn_closes_lookback: np.ndarray,
    leader_recent: Dict[str, List[Candle]],
    last_exit_time: Optional[datetime] = None,
) -> Tuple[Optional[BNSignal], BNDiagnostic]:
    """
    Evaluate ONE just-closed BankNifty 5m bar for an entry.

    Simplified rule (2026-09-19, explicit user decision — REPLACES the
    prior sideways-range + momentum + per-leader volume-surge + RSI/MACD/
    EMA composite-indicator gate sequence entirely): if at least
    cfg.BN_SAME_DIRECTION_REQUIRED (default 9) of the 14 real NIFTY BANK
    stocks (`leader_recent`, now built by the caller from cfg.BN_ALL_STOCKS,
    not just the 6 leaders) closed green on the just-closed bar, fire BUY
    (long ATM Call); same count red fires SELL (long ATM Put). Target/stop
    are cfg.BN_TARGET_POINTS/BN_STOPLOSS_POINTS as always (now 1/10 points
    respectively) — only the ENTRY condition changed, not how exits work.

    The caller must ensure this bar hasn't already been evaluated and that
    no trade is currently active — this function only decides "would this
    bar fire?". Returns (signal, diagnostic); signal is None when nothing
    fires. Cooldown is the only gate retained from before (a basic entry-
    rate-limit, not part of the "condition" being replaced).
    """
    required = cfg.BN_SAME_DIRECTION_REQUIRED
    n_stocks = len(leader_recent)
    leader_last = {name: (candles[-1] if candles else None) for name, candles in leader_recent.items()}

    bn_bar_time = bn_recent_candles[-1].start_time if bn_recent_candles else ""
    bn_close = bn_recent_candles[-1].close if bn_recent_candles else 0.0

    no_trade_reason: Optional[str] = None
    cooldown_ok = True
    if last_exit_time is not None:
        elapsed = (now - last_exit_time).total_seconds()
        if elapsed < cfg.BN_ENTRY_COOLDOWN_S:
            cooldown_ok = False
            no_trade_reason = f"Cooldown {cfg.BN_ENTRY_COOLDOWN_S - elapsed:.0f}s remaining"

    if len(bn_recent_candles) < 1:
        no_trade_reason = no_trade_reason or "Insufficient BankNifty candles"

    green_count = sum(1 for c in leader_last.values() if c and c.close > c.open)
    red_count = sum(1 for c in leader_last.values() if c and c.close < c.open)

    if no_trade_reason is None and max(green_count, red_count) < required:
        no_trade_reason = f"Only {max(green_count, red_count)}/{n_stocks} stocks agree (need {required})"

    buy_ready = cooldown_ok and green_count >= required
    sell_ready = cooldown_ok and red_count >= required

    signal: Optional[BNSignal] = None
    # Live ATM CE/PE quote — computed unconditionally, regardless of whether
    # the vote above actually fires (2026-09-18, explicit user decision), so
    # the dashboard can show "what this would cost right now" even with no
    # trade open and no signal about to fire. Cheap: Black-Scholes is
    # closed-form, this runs once per closed bar either way.
    atm_strike = atm_iv = atm_ce_premium = atm_pe_premium = None
    atm_expiry = get_next_expiry(now)
    if bn_close > 0:
        atm_strike = get_atm_strike(bn_close)
        T = time_to_expiry_years(now, atm_expiry)
        atm_iv = estimate_iv(bn_closes_lookback)
        atm_ce_premium = black_scholes(bn_close, atm_strike, T, cfg.BN_RISK_FREE_RATE, atm_iv, "CE")["price"]
        atm_pe_premium = black_scholes(bn_close, atm_strike, T, cfg.BN_RISK_FREE_RATE, atm_iv, "PE")["price"]

    if buy_ready or sell_ready:
        direction = "BUY" if buy_ready else "SELL"
        option_type = "CE" if direction == "BUY" else "PE"
        premium = atm_ce_premium if option_type == "CE" else atm_pe_premium
        direction_count = green_count if direction == "BUY" else red_count

        signal = BNSignal(
            direction=direction,
            entry_index_price=bn_close,
            bar_time=bn_bar_time,
            confidence=round(direction_count / n_stocks * 100.0) if n_stocks else 0.0,
            green=green_count,
            red=red_count,
            strong_qty=0,
            leader_signal=direction,
            bn_bull=0.0,
            bn_bear=0.0,
            strike=atm_strike,
            expiry=atm_expiry.isoformat(),
            entry_premium=premium,
            iv_used=atm_iv,
        )
        no_trade_reason = None

    diagnostic = BNDiagnostic(
        time=bn_bar_time,
        bn_ltp=bn_close,
        green=green_count,
        red=red_count,
        strong_qty=0,
        leader_rows=[{"stock": name, "open": c.open if c else None, "close": c.close if c else None,
                      "volume": c.volume if c else None, "surged": False}
                     for name, c in leader_last.items()],
        leader_signal="BUY" if buy_ready else ("SELL" if sell_ready else "Nobuysell"),
        no_trade_reason=no_trade_reason,
        candle_close_ok=True,
        cooldown_ms=0.0 if cooldown_ok else max(0.0, cfg.BN_ENTRY_COOLDOWN_S -
                                                 (now - last_exit_time).total_seconds()) * 1000.0,
        market_open=True,
        atm_strike=atm_strike,
        atm_premium=atm_ce_premium,   # kept for the (currently unused) Entry Loop Monitor UI
        atm_iv=atm_iv,
        atm_ce_premium=atm_ce_premium,
        atm_pe_premium=atm_pe_premium,
        cooldown_ok=cooldown_ok,
        sideways_ok=True,   # no longer a real gate — see the function docstring
        dir_count_ok=max(green_count, red_count) >= required,
        qty_surge_ok=True,  # no longer a real gate — see the function docstring
        same_direction_required=required,
        gates_clear=cooldown_ok,
        entry_ready=buy_ready or sell_ready,
    )
    return signal, diagnostic


@dataclass(slots=True)
class ExitEvaluation:
    new_sl: float
    sl_stage: str
    current_premium: float
    current_iv: float
    current_delta: float
    current_theta: float
    should_exit: bool
    exit_reason: Optional[str] = None   # "TARGET" | "STOP"


def evaluate_exit(trade: BNTrade, now: datetime, current_index_price: float,
                  bn_closes_lookback: np.ndarray) -> ExitEvaluation:
    """
    Port of c.html's checkExit — target/breakeven/trailing state machine on
    the underlying BankNifty index price, plus a live theoretical option
    premium mark (Black-Scholes at the current spot) used both for the ATM
    panel display and — when should_exit — as the settlement premium.

    Reads risk parameters (target/breakeven/trail) from the TRADE, not from
    cfg — they were frozen at entry (see open_trade_from_signal) so a live
    Settings change never retroactively alters an already-open trade.
    """
    entry  = trade.entry_index_price
    target = trade.target
    sl     = trade.current_sl
    stage  = trade.sl_stage

    if trade.direction == "BUY":
        pnl_pts = current_index_price - entry
        if pnl_pts >= trade.trail_trigger:
            candidate = current_index_price - trade.trail_distance
            if candidate > sl:
                sl, stage = candidate, "Trail"
        elif pnl_pts >= trade.breakeven_trigger:
            if entry > sl:
                sl, stage = entry, "Breakeven"
        should_exit = current_index_price >= target or current_index_price <= sl
        exit_reason = "TARGET" if current_index_price >= target else ("STOP" if should_exit else None)
    else:
        pnl_pts = entry - current_index_price
        if pnl_pts >= trade.trail_trigger:
            candidate = current_index_price + trade.trail_distance
            if candidate < sl:
                sl, stage = candidate, "Trail"
        elif pnl_pts >= trade.breakeven_trigger:
            if entry < sl:
                sl, stage = entry, "Breakeven"
        should_exit = current_index_price <= target or current_index_price >= sl
        exit_reason = "TARGET" if current_index_price <= target else ("STOP" if should_exit else None)

    expiry = datetime.fromisoformat(trade.expiry)
    T = time_to_expiry_years(now, expiry)
    iv = estimate_iv(bn_closes_lookback)
    bs = black_scholes(current_index_price, trade.strike, T, cfg.BN_RISK_FREE_RATE, iv, trade.option_type)

    return ExitEvaluation(
        new_sl=sl, sl_stage=stage,
        current_premium=bs["price"], current_iv=iv,
        current_delta=bs["delta"], current_theta=bs["theta"],
        should_exit=should_exit, exit_reason=exit_reason,
    )


def open_trade_from_signal(signal: BNSignal, now: datetime, order_id: str = "") -> BNTrade:
    """
    Convert a fired BNSignal into the single active BNTrade, freezing this
    trade's risk parameters (target/breakeven/trail) from the CURRENT cfg
    values — a later Settings-page change must not retroactively alter an
    already-open trade's economics.
    """
    stoploss_points = cfg.BN_STOPLOSS_POINTS
    if signal.direction == "BUY":
        target = signal.entry_index_price + cfg.BN_TARGET_POINTS
        initial_sl = signal.entry_index_price - stoploss_points
    else:
        target = signal.entry_index_price - cfg.BN_TARGET_POINTS
        initial_sl = signal.entry_index_price + stoploss_points

    option_type = "CE" if signal.direction == "BUY" else "PE"
    # Real vendor option symbol this trade's leg will be subscribed under
    # (live-only real-LTP feature — see BNTrade.option_symbol in models.py).
    # Harmless to compute unconditionally: backtest never reads this field.
    option_symbol = build_monthly_option_symbol(
        cfg.BN_OPTION_UNDERLYING, datetime.fromisoformat(signal.expiry), signal.strike, option_type)

    return BNTrade(
        direction=signal.direction,
        entry_index_price=signal.entry_index_price,
        entry_time=now.isoformat(),
        target=target,
        current_sl=initial_sl,
        strike=signal.strike,
        option_type=option_type,
        expiry=signal.expiry,
        entry_premium=signal.entry_premium,
        stoploss_points=stoploss_points,
        breakeven_trigger=cfg.BN_BREAKEVEN_TRIGGER,
        trail_trigger=cfg.BN_TRAIL_TRIGGER,
        trail_distance=cfg.BN_TRAIL_DISTANCE,
        lot_size=cfg.BN_LOT_SIZE,
        order_id=order_id,
        confidence=signal.confidence,
        entry_signal=signal,
        option_symbol=option_symbol,
    )


def finalize_exit(trade: BNTrade, now: datetime, exit_index_price: float,
                  exit_premium: float) -> BNTrade:
    """
    Close `trade` in place and return it. Settlement is the OPTION PREMIUM
    P&L × lot size — matching c.html's exitTrade exactly — not the raw index
    points (index_pnl_points is kept purely as a diagnostic).
    """
    trade.status = PositionStatus.CLOSED
    trade.exit_index_price = round(exit_index_price, 2)
    trade.exit_time = now.isoformat()
    trade.exit_premium = round(exit_premium, 2)
    trade.index_pnl_points = round(
        (exit_index_price - trade.entry_index_price) if trade.direction == "BUY"
        else (trade.entry_index_price - exit_index_price), 2)
    trade.pnl = round((trade.exit_premium - trade.entry_premium) * trade.lot_size, 2)
    return trade
