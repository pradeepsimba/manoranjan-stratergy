from __future__ import annotations

"""
Shared Bank Nifty entry/exit decision core — called identically by the live
scheduler and the backtest replay loop (this repo's hard convention: live and
backtest must share one strategy core).

Ported from c.html's checkTradeEntry/checkExit, with one deliberate
deviation (see CLAUDE.md-style callout inline):
  * No JS "pending signal" pre-qualification latch — the caller is expected
    to invoke evaluate_entry exactly once per newly-closed 5m bar (wall-clock
    bar-close detection lives in the caller), which achieves the same
    "fire right at candle close" outcome without extra state to keep in sync.

The "strong quantity" gate is a literal port of c.html's fixed absolute
per-stock STOCK_QTY_THRESHOLD table (now dynamic Settings-page tunables,
see cfg.BN_QTY_THRESHOLD_ATTR / config._DEFAULTS' BN_QTY_THRESHOLD_* keys),
compared against Candle.last_qty — the real per-trade quantity the vendor's
current protocol embeds in each tick's `quote` text (parsed in
market_data.py's _process_tick). An earlier vendor protocol had no such
field at all (only cumulative 5m bar volume, hundreds of thousands — wildly
mismatched against these tens/hundreds-scale thresholds), which made this
gate a permanently-satisfied no-op; that limitation no longer applies now
that a real per-trade qty field exists (confirmed 2026-07-23).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

import app.config as cfg
from app.engine.bn_pricing import black_scholes, estimate_iv, get_atm_strike, get_next_expiry, time_to_expiry_years
from app.engine.bn_signals import (
    bn_composite_indicator,
    leaders_momentum,
    sideways_range,
    strong_momentum,
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


def _confidence(direction_count: int, strong_qty_count: int, n_leaders: int) -> float:
    """Port of c.html's calculateConfidence — 50% weight each on leader-vote and qty-surge breadth."""
    if n_leaders <= 0:
        return 0.0
    return round((direction_count / n_leaders) * 50.0 + (strong_qty_count / n_leaders) * 50.0)


def evaluate_entry(
    now: datetime,
    bn_recent_candles: List[Candle],
    bn_closes_lookback: np.ndarray,
    leader_recent: Dict[str, List[Candle]],
    last_exit_time: Optional[datetime] = None,
) -> Tuple[Optional[BNSignal], BNDiagnostic]:
    """
    Evaluate ONE just-closed BankNifty 5m bar for an entry. The caller must
    ensure this bar hasn't already been evaluated and that no trade is
    currently active — this function only decides "would this bar fire?".

    Returns (signal, diagnostic). signal is None when no direction fires;
    diagnostic always carries the full gate breakdown for the dashboard.
    """
    required = cfg.BN_SAME_DIRECTION_REQUIRED
    n_leaders = len(leader_recent)
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

    if len(bn_recent_candles) < 2:
        no_trade_reason = no_trade_reason or "Insufficient BankNifty candles"

    rng = sideways_range(bn_closes_lookback, bars=5)
    sideways_blocked = rng < cfg.BN_SIDEWAYS_RANGE_MIN
    if no_trade_reason is None and sideways_blocked:
        no_trade_reason = f"Sideways: range {rng:.1f} < {cfg.BN_SIDEWAYS_RANGE_MIN}"

    momentum = strong_momentum(bn_recent_candles) if len(bn_recent_candles) >= 2 else {"ok": False, "reason": "Insufficient candles"}
    if no_trade_reason is None and not momentum["ok"]:
        no_trade_reason = momentum["reason"]

    leaders = leaders_momentum(leader_last)
    if no_trade_reason is None and leaders["signal"] == "Nobuysell":
        no_trade_reason = leaders["reason"]

    qty_surge = _leader_qty_surge(leader_recent)
    strong_qty_count = sum(1 for v in qty_surge.values() if v)
    if no_trade_reason is None and strong_qty_count < required:
        no_trade_reason = f"Only {strong_qty_count}/{n_leaders} leaders show volume surge (need {required})"

    bn_ind = bn_composite_indicator(bn_closes_lookback, leader_recent)
    if no_trade_reason is None and leaders["signal"] == "BUY" and not bn_ind["bullish"]:
        no_trade_reason = "BN composite indicator not bullish"
    if no_trade_reason is None and leaders["signal"] == "SELL" and not bn_ind["bearish"]:
        no_trade_reason = "BN composite indicator not bearish"

    gates_clear = (cooldown_ok and not sideways_blocked and momentum["ok"]
                   and strong_qty_count >= required)
    buy_ready = gates_clear and leaders["signal"] == "BUY" and bn_ind["bullish"]
    sell_ready = gates_clear and leaders["signal"] == "SELL" and bn_ind["bearish"]

    signal: Optional[BNSignal] = None
    atm_strike = atm_premium = atm_iv = None

    if buy_ready or sell_ready:
        direction = "BUY" if buy_ready else "SELL"
        option_type = "CE" if direction == "BUY" else "PE"
        strike = get_atm_strike(bn_close)
        expiry = get_next_expiry(now)
        T = time_to_expiry_years(now, expiry)
        iv = estimate_iv(bn_closes_lookback)
        bs = black_scholes(bn_close, strike, T, cfg.BN_RISK_FREE_RATE, iv, option_type)
        direction_count = leaders["buy_count"] if direction == "BUY" else leaders["sell_count"]

        atm_strike, atm_premium, atm_iv = strike, bs["price"], iv
        signal = BNSignal(
            direction=direction,
            entry_index_price=bn_close,
            bar_time=bn_bar_time,
            confidence=_confidence(direction_count, strong_qty_count, n_leaders),
            green=leaders["buy_count"],
            red=leaders["sell_count"],
            strong_qty=strong_qty_count,
            leader_signal=leaders["signal"],
            bn_bull=bn_ind["bull"],
            bn_bear=bn_ind["bear"],
            strike=strike,
            expiry=expiry.isoformat(),
            entry_premium=bs["price"],
            iv_used=iv,
        )
        no_trade_reason = None

    diagnostic = BNDiagnostic(
        time=bn_bar_time,
        bn_ltp=bn_close,
        green=leaders["buy_count"],
        red=leaders["sell_count"],
        strong_qty=strong_qty_count,
        leader_rows=[{"stock": name, "open": c.open if c else None, "close": c.close if c else None,
                      "volume": c.volume if c else None, "surged": qty_surge.get(name, False)}
                     for name, c in leader_last.items()],
        leader_signal=leaders["signal"],
        sideways_range=rng,
        momentum_ok=momentum["ok"],
        momentum_reason=momentum["reason"],
        rsi=bn_ind["rsi"],
        macd_dir=bn_ind["macd_dir"],
        macd_val=bn_ind["macd_val"],
        ema_bullish=bn_ind["ema_bullish"],
        ema_bearish=bn_ind["ema_bearish"],
        bn_bull=bn_ind["bull"],
        bn_bear=bn_ind["bear"],
        bn_bullish=bn_ind["bullish"],
        bn_bearish=bn_ind["bearish"],
        no_trade_reason=no_trade_reason,
        candle_close_ok=True,
        cooldown_ms=0.0 if cooldown_ok else max(0.0, cfg.BN_ENTRY_COOLDOWN_S -
                                                 (now - last_exit_time).total_seconds()) * 1000.0,
        market_open=True,
        atm_strike=atm_strike,
        atm_premium=atm_premium,
        atm_iv=atm_iv,
        cooldown_ok=cooldown_ok,
        sideways_ok=not sideways_blocked,
        dir_count_ok=max(leaders["buy_count"], leaders["sell_count"]) >= required,
        qty_surge_ok=strong_qty_count >= required,
        same_direction_required=required,
        gates_clear=gates_clear,
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

    return BNTrade(
        direction=signal.direction,
        entry_index_price=signal.entry_index_price,
        entry_time=now.isoformat(),
        target=target,
        current_sl=initial_sl,
        strike=signal.strike,
        option_type="CE" if signal.direction == "BUY" else "PE",
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
