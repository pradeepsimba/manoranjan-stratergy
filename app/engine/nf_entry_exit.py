from __future__ import annotations

"""
Shared Nifty 50 entry/exit decision core — mechanical mirror of
bn_entry_exit.py, called identically by the live scheduler (no backtest
wiring yet — see CLAUDE.md/plan notes on this pass's scope). Reads cfg.NF_*
instead of cfg.BN_*, uses nf_signals.py/nf_pricing.py, produces
NFSignal/NFTrade/NFDiagnostic instead of the BN dataclasses.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

import app.config as cfg
from app.engine.nf_pricing import black_scholes, estimate_iv, get_atm_strike, get_next_expiry, time_to_expiry_years
from app.engine.nf_signals import (
    leaders_momentum,
    nf_composite_indicator,
    sideways_range,
    strong_momentum,
)
from app.models import NFDiagnostic, NFSignal, NFTrade, Candle, PositionStatus


def _stock_qty_threshold(name: str) -> float:
    """NF mirror of bn_entry_exit._stock_qty_threshold."""
    attr = cfg.NF_QTY_THRESHOLD_ATTR.get(name)
    base = getattr(cfg, attr) if attr else 10_000
    return base * cfg.NF_QTY_INTERVAL_MULTIPLIER


def _leader_qty_surge(leader_recent: Dict[str, List[Candle]]) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for name, candles in leader_recent.items():
        out[name] = bool(candles) and candles[-1].volume >= _stock_qty_threshold(name)
    return out


def _confidence(direction_count: int, strong_qty_count: int, n_leaders: int) -> float:
    if n_leaders <= 0:
        return 0.0
    return round((direction_count / n_leaders) * 50.0 + (strong_qty_count / n_leaders) * 50.0)


def evaluate_entry(
    now: datetime,
    nf_recent_candles: List[Candle],
    nf_closes_lookback: np.ndarray,
    leader_recent: Dict[str, List[Candle]],
    last_exit_time: Optional[datetime] = None,
) -> Tuple[Optional[NFSignal], NFDiagnostic]:
    """NF mirror of bn_entry_exit.evaluate_entry — see there for the detailed gate walkthrough."""
    required = cfg.NF_SAME_DIRECTION_REQUIRED
    n_leaders = len(leader_recent)
    leader_last = {name: (candles[-1] if candles else None) for name, candles in leader_recent.items()}

    nf_bar_time = nf_recent_candles[-1].start_time if nf_recent_candles else ""
    nf_close = nf_recent_candles[-1].close if nf_recent_candles else 0.0

    no_trade_reason: Optional[str] = None
    cooldown_ok = True
    if last_exit_time is not None:
        elapsed = (now - last_exit_time).total_seconds()
        if elapsed < cfg.NF_ENTRY_COOLDOWN_S:
            cooldown_ok = False
            no_trade_reason = f"Cooldown {cfg.NF_ENTRY_COOLDOWN_S - elapsed:.0f}s remaining"

    if len(nf_recent_candles) < 2:
        no_trade_reason = no_trade_reason or "Insufficient Nifty 50 candles"

    rng = sideways_range(nf_closes_lookback, bars=5)
    sideways_blocked = rng < cfg.NF_SIDEWAYS_RANGE_MIN
    if no_trade_reason is None and sideways_blocked:
        no_trade_reason = f"Sideways: range {rng:.1f} < {cfg.NF_SIDEWAYS_RANGE_MIN}"

    momentum = strong_momentum(nf_recent_candles) if len(nf_recent_candles) >= 2 else {"ok": False, "reason": "Insufficient candles"}
    if no_trade_reason is None and not momentum["ok"]:
        no_trade_reason = momentum["reason"]

    leaders = leaders_momentum(leader_last)
    if no_trade_reason is None and leaders["signal"] == "Nobuysell":
        no_trade_reason = leaders["reason"]

    qty_surge = _leader_qty_surge(leader_recent)
    strong_qty_count = sum(1 for v in qty_surge.values() if v)
    if no_trade_reason is None and strong_qty_count < required:
        no_trade_reason = f"Only {strong_qty_count}/{n_leaders} leaders show volume surge (need {required})"

    nf_ind = nf_composite_indicator(nf_closes_lookback, leader_recent)
    if no_trade_reason is None and leaders["signal"] == "BUY" and not nf_ind["bullish"]:
        no_trade_reason = "NF composite indicator not bullish"
    if no_trade_reason is None and leaders["signal"] == "SELL" and not nf_ind["bearish"]:
        no_trade_reason = "NF composite indicator not bearish"

    gates_clear = (cooldown_ok and not sideways_blocked and momentum["ok"]
                   and strong_qty_count >= required)
    buy_ready = gates_clear and leaders["signal"] == "BUY" and nf_ind["bullish"]
    sell_ready = gates_clear and leaders["signal"] == "SELL" and nf_ind["bearish"]

    signal: Optional[NFSignal] = None
    atm_strike = atm_premium = atm_iv = None

    if buy_ready or sell_ready:
        direction = "BUY" if buy_ready else "SELL"
        option_type = "CE" if direction == "BUY" else "PE"
        strike = get_atm_strike(nf_close)
        expiry = get_next_expiry(now)
        T = time_to_expiry_years(now, expiry)
        iv = estimate_iv(nf_closes_lookback)
        bs = black_scholes(nf_close, strike, T, cfg.NF_RISK_FREE_RATE, iv, option_type)
        direction_count = leaders["buy_count"] if direction == "BUY" else leaders["sell_count"]

        atm_strike, atm_premium, atm_iv = strike, bs["price"], iv
        signal = NFSignal(
            direction=direction,
            entry_index_price=nf_close,
            bar_time=nf_bar_time,
            confidence=_confidence(direction_count, strong_qty_count, n_leaders),
            green=leaders["buy_count"],
            red=leaders["sell_count"],
            strong_qty=strong_qty_count,
            leader_signal=leaders["signal"],
            bn_bull=nf_ind["bull"],
            bn_bear=nf_ind["bear"],
            strike=strike,
            expiry=expiry.isoformat(),
            entry_premium=bs["price"],
            iv_used=iv,
        )
        no_trade_reason = None

    diagnostic = NFDiagnostic(
        time=nf_bar_time,
        bn_ltp=nf_close,
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
        rsi=nf_ind["rsi"],
        macd_dir=nf_ind["macd_dir"],
        macd_val=nf_ind["macd_val"],
        ema_bullish=nf_ind["ema_bullish"],
        ema_bearish=nf_ind["ema_bearish"],
        bn_bull=nf_ind["bull"],
        bn_bear=nf_ind["bear"],
        bn_bullish=nf_ind["bullish"],
        bn_bearish=nf_ind["bearish"],
        no_trade_reason=no_trade_reason,
        candle_close_ok=True,
        cooldown_ms=0.0 if cooldown_ok else max(0.0, cfg.NF_ENTRY_COOLDOWN_S -
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
    exit_reason: Optional[str] = None


def evaluate_exit(trade: NFTrade, now: datetime, current_index_price: float,
                  nf_closes_lookback: np.ndarray) -> ExitEvaluation:
    """NF mirror of bn_entry_exit.evaluate_exit — reads risk params off the trade, not cfg."""
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
    iv = estimate_iv(nf_closes_lookback)
    bs = black_scholes(current_index_price, trade.strike, T, cfg.NF_RISK_FREE_RATE, iv, trade.option_type)

    return ExitEvaluation(
        new_sl=sl, sl_stage=stage,
        current_premium=bs["price"], current_iv=iv,
        current_delta=bs["delta"], current_theta=bs["theta"],
        should_exit=should_exit, exit_reason=exit_reason,
    )


def open_trade_from_signal(signal: NFSignal, now: datetime, order_id: str = "") -> NFTrade:
    """NF mirror of bn_entry_exit.open_trade_from_signal — freezes cfg.NF_* risk params at entry."""
    stoploss_points = cfg.NF_STOPLOSS_POINTS
    if signal.direction == "BUY":
        target = signal.entry_index_price + cfg.NF_TARGET_POINTS
        initial_sl = signal.entry_index_price - stoploss_points
    else:
        target = signal.entry_index_price - cfg.NF_TARGET_POINTS
        initial_sl = signal.entry_index_price + stoploss_points

    return NFTrade(
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
        breakeven_trigger=cfg.NF_BREAKEVEN_TRIGGER,
        trail_trigger=cfg.NF_TRAIL_TRIGGER,
        trail_distance=cfg.NF_TRAIL_DISTANCE,
        lot_size=cfg.NF_LOT_SIZE,
        order_id=order_id,
        confidence=signal.confidence,
        entry_signal=signal,
    )


def finalize_exit(trade: NFTrade, now: datetime, exit_index_price: float,
                  exit_premium: float) -> NFTrade:
    """NF mirror of bn_entry_exit.finalize_exit."""
    trade.status = PositionStatus.CLOSED
    trade.exit_index_price = round(exit_index_price, 2)
    trade.exit_time = now.isoformat()
    trade.exit_premium = round(exit_premium, 2)
    trade.index_pnl_points = round(
        (exit_index_price - trade.entry_index_price) if trade.direction == "BUY"
        else (trade.entry_index_price - exit_index_price), 2)
    trade.pnl = round((trade.exit_premium - trade.entry_premium) * trade.lot_size, 2)
    return trade
