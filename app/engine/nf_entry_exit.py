from __future__ import annotations

"""
Shared Nifty 50 entry/exit decision core — mechanical mirror of
bn_entry_exit.py (REWRITTEN 2026-09-21 alongside it — see there for the
full rationale). Reads cfg.NF_*/SCALP_WINDOW*/SCALP_MAX_TRADES_PER_DAY
instead of cfg.BN_*, uses nf_pricing.py, produces NFSignal/NFTrade/
NFDiagnostic instead of the BN dataclasses. Trading-window and
max-trades/day guardrails are SHARED across both instruments (one set of
limits — see config.py), not duplicated per instrument.

nf_signals.py (the sideways/momentum/leader-vote/composite-indicator gates
this replaced) was fully DELETED 2026-09-21, same as bn_signals.py — see
bn_entry_exit.py's module docstring. Unlike BN_SAME_DIRECTION_REQUIRED, NF's
own version had no other reader (no NF equivalent of signal_study.py
exists), so it's gone entirely, not kept for any standalone tool.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

import app.config as cfg
from app.engine.nf_pricing import (
    black_scholes,
    build_weekly_option_symbol,
    estimate_iv,
    get_itm_strike,
    get_next_expiry,
    time_to_expiry_years,
)
from app.engine.risk_guardrails import in_trading_window as _in_trading_window
from app.engine.risk_guardrails import max_trades_ok as _max_trades_ok
from app.engine.scalp_signals import compute_basket_reading
from app.engine.wobi import compute_wobi, synthetic_depth
from app.models import NFDiagnostic, NFSignal, NFTrade, Candle, PositionStatus


def _stock_qty_threshold(name: str) -> float:
    """UNRELATED to the scalp strategy — see bn_entry_exit._stock_qty_threshold."""
    attr = cfg.NF_QTY_THRESHOLD_ATTR.get(name)
    base = getattr(cfg, attr) if attr else 10_000
    return base * cfg.NF_QTY_INTERVAL_MULTIPLIER


def _leader_qty_surge(leader_recent: Dict[str, List[Candle]]) -> Dict[str, bool]:
    """UNRELATED to the scalp strategy — see bn_entry_exit._leader_qty_surge."""
    out: Dict[str, bool] = {}
    for name, candles in leader_recent.items():
        out[name] = bool(candles) and candles[-1].volume >= _stock_qty_threshold(name)
    return out


# _in_trading_window/_max_trades_ok moved to app.engine.risk_guardrails
# 2026-09-23 — see bn_entry_exit.py's identical note.


def evaluate_entry(
    now: datetime,
    nf_recent_candles: List[Candle],
    nf_closes_lookback: np.ndarray,
    basket_candles: Dict[str, List[Candle]],
    last_exit_time: Optional[datetime] = None,
    trades_today: int = 0,
    current_index_price: Optional[float] = None,
    basket_ltp: Optional[Dict[str, float]] = None,
) -> Tuple[Optional[NFSignal], NFDiagnostic]:
    """NF mirror of bn_entry_exit.evaluate_entry — see there for the full gate
    walkthrough and the current_index_price/basket_ltp tick-wise-evaluation note."""
    nf_bar_time = nf_recent_candles[-1].start_time if nf_recent_candles else ""
    nf_close = current_index_price if current_index_price else (
        nf_recent_candles[-1].close if nf_recent_candles else 0.0)

    no_trade_reason: Optional[str] = None

    cooldown_ok = True
    if last_exit_time is not None:
        elapsed = (now - last_exit_time).total_seconds()
        if elapsed < cfg.NF_SCALP_COOLDOWN_S:
            cooldown_ok = False
            no_trade_reason = f"Cooldown {cfg.NF_SCALP_COOLDOWN_S - elapsed:.0f}s remaining"

    window_ok = _in_trading_window(now)
    if no_trade_reason is None and not window_ok:
        no_trade_reason = "Outside scalp trading window (09:45-11:15 / 13:45-14:45 IST)"

    max_trades_ok = _max_trades_ok(trades_today)
    if no_trade_reason is None and not max_trades_ok:
        no_trade_reason = f"Max {cfg.SCALP_MAX_TRADES_PER_DAY} trades/day reached"

    name_by_token = cfg.NF_NAME_BY_TOKEN
    reading = compute_basket_reading(cfg.NF_SCALP_BASKET, basket_candles, name_by_token,
                                     now.date(), ltp_by_token=basket_ltp)
    threshold = cfg.NF_SCALP_SCORE_THRESHOLD

    score_buy_ok = reading.score >= threshold
    score_sell_ok = reading.score <= -threshold
    if no_trade_reason is None and not (score_buy_ok or score_sell_ok):
        no_trade_reason = f"Basket score {reading.score:+.3f} within ±{threshold}"
    elif no_trade_reason is None and not reading.top2_direction_ok:
        no_trade_reason = f"Top-2 ({', '.join(n for n in reading.top2_names if n)}) not confirming direction"

    gates_clear = cooldown_ok and window_ok and max_trades_ok
    buy_ready = gates_clear and score_buy_ok and reading.top2_direction_ok
    sell_ready = gates_clear and score_sell_ok and reading.top2_direction_ok

    itm_iv: Optional[float] = None
    itm_ce_strike = itm_pe_strike = None
    itm_ce_premium = itm_pe_premium = None
    itm_expiry = get_next_expiry(now)
    if nf_close > 0:
        T = time_to_expiry_years(now, itm_expiry)
        itm_iv = estimate_iv(nf_closes_lookback)
        itm_ce_strike = get_itm_strike(nf_close, "CE", cfg.NF_ITM_OFFSET_POINTS)
        itm_pe_strike = get_itm_strike(nf_close, "PE", cfg.NF_ITM_OFFSET_POINTS)
        itm_ce_premium = black_scholes(nf_close, itm_ce_strike, T, cfg.NF_RISK_FREE_RATE, itm_iv, "CE")["price"]
        itm_pe_premium = black_scholes(nf_close, itm_pe_strike, T, cfg.NF_RISK_FREE_RATE, itm_iv, "PE")["price"]

    signal: Optional[NFSignal] = None
    wobi_value: Optional[float] = None
    if (buy_ready or sell_ready) and nf_close > 0:
        direction = "BUY" if buy_ready else "SELL"
        option_type = "CE" if direction == "BUY" else "PE"
        strike = itm_ce_strike if option_type == "CE" else itm_pe_strike
        premium = itm_ce_premium if option_type == "CE" else itm_pe_premium

        # See bn_entry_exit.evaluate_entry's identical comment — seed keys
        # on the score itself (ties W-OBI to genuine market movement, no
        # free re-rolls), not nf_bar_time (froze for 5min) or wall-clock
        # time (let a persistent signal dice-roll its way past the filter).
        depth = synthetic_depth(cfg.NF_LOT_SIZE, itm_iv, reading.score,
                                seed=f"NF{option_type}{strike}:{reading.score:.3f}")
        wobi_value = compute_wobi(depth)
        wobi_ok = wobi_value > cfg.NF_WOBI_MIN_RATIO

        if wobi_ok:
            signal = NFSignal(
                direction=direction,
                entry_index_price=nf_close,
                bar_time=nf_bar_time,
                confidence=round(min(100.0, abs(reading.score) / threshold * 50.0), 1),
                green=0, red=0, strong_qty=0,
                leader_signal=direction,
                bn_bull=0.0, bn_bear=0.0,
                strike=strike,
                expiry=itm_expiry.isoformat(),
                entry_premium=premium,
                iv_used=itm_iv,
                basket_score=reading.score,
                wobi=wobi_value,
            )
        else:
            no_trade_reason = f"W-OBI {wobi_value:.2f} ≤ {cfg.NF_WOBI_MIN_RATIO} (path not clear)"

    diagnostic = NFDiagnostic(
        time=nf_bar_time,
        bn_ltp=nf_close,
        green=0, red=0, strong_qty=0,
        leader_rows=[
            {"stock": leg.name, "weight": round(leg.weight, 4),
             "vwap": round(leg.vwap, 2) if leg.vwap is not None else None,
             "ltp": leg.ltp,
             "deviationPct": round(leg.deviation_pct, 4) if leg.deviation_pct is not None else None,
             "open": None, "close": leg.ltp, "volume": None, "surged": False}
            for leg in reading.legs
        ],
        leader_signal=(signal.direction if signal is not None else "Nobuysell"),
        no_trade_reason=no_trade_reason,
        candle_close_ok=True,
        cooldown_ms=0.0 if cooldown_ok else max(0.0, cfg.NF_SCALP_COOLDOWN_S -
                                                 (now - last_exit_time).total_seconds()) * 1000.0,
        market_open=True,
        itm_strike=itm_ce_strike if reading.score >= 0 else itm_pe_strike,
        itm_premium=itm_ce_premium,
        itm_iv=itm_iv,
        itm_ce_premium=itm_ce_premium,
        itm_pe_premium=itm_pe_premium,
        # See bn_entry_exit.evaluate_entry's identical comment — mirrors
        # the basket-score condition instead of leaving these permanently
        # False/None (found in review).
        momentum_ok=score_buy_ok or score_sell_ok,
        macd_dir=("BUY" if score_buy_ok else ("SELL" if score_sell_ok else None)),
        ema_bullish=score_buy_ok,
        ema_bearish=score_sell_ok,
        bn_bullish=score_buy_ok,
        bn_bearish=score_sell_ok,
        cooldown_ok=cooldown_ok,
        sideways_ok=True,
        dir_count_ok=score_buy_ok or score_sell_ok,
        qty_surge_ok=True,
        same_direction_required=0,
        gates_clear=gates_clear,
        entry_ready=signal is not None,
        basket_score=round(reading.score, 4),
        score_threshold=threshold,
        top2_ok=reading.top2_direction_ok,
        top2_names=[n for n in reading.top2_names if n],
        wobi=wobi_value,
        wobi_min_ratio=cfg.NF_WOBI_MIN_RATIO,
        window_ok=window_ok,
        trades_today=trades_today,
        max_trades_today=cfg.SCALP_MAX_TRADES_PER_DAY,
        itm_offset_points=cfg.NF_ITM_OFFSET_POINTS,
        target_rs=cfg.NF_SCALP_TARGET_RS,
        stop_rs=cfg.NF_SCALP_STOP_RS,
        time_stop_s=cfg.NF_SCALP_TIME_STOP_S,
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
    """NF mirror of bn_entry_exit.evaluate_exit — see there for the full lifecycle
    walkthrough, including why the live_premium_override this used to accept was removed."""
    expiry = datetime.fromisoformat(trade.expiry)
    T = time_to_expiry_years(now, expiry)
    iv = estimate_iv(nf_closes_lookback)
    bs = black_scholes(current_index_price, trade.strike, T, cfg.NF_RISK_FREE_RATE, iv, trade.option_type)
    premium = bs["price"]

    entry_time = datetime.fromisoformat(trade.entry_time)
    elapsed_s = (now - entry_time).total_seconds()

    should_exit = False
    exit_reason: Optional[str] = None
    settle_premium = premium
    if premium >= trade.target:
        should_exit, exit_reason = True, "TARGET"
    elif premium <= trade.current_sl:
        should_exit, exit_reason = True, "STOP"
    elif elapsed_s >= trade.time_stop_s:
        should_exit, exit_reason = True, "TIME_SCRATCH"
        settle_premium = max(0.0, premium - trade.scratch_slippage_rs)

    return ExitEvaluation(
        new_sl=trade.current_sl, sl_stage=trade.sl_stage,
        current_premium=settle_premium if should_exit else premium,
        current_iv=iv, current_delta=bs["delta"], current_theta=bs["theta"],
        should_exit=should_exit, exit_reason=exit_reason,
    )


def open_trade_from_signal(signal: NFSignal, now: datetime, order_id: str = "") -> NFTrade:
    """NF mirror of bn_entry_exit.open_trade_from_signal."""
    target_rs = cfg.NF_SCALP_TARGET_RS
    stop_rs = cfg.NF_SCALP_STOP_RS
    time_stop_s = cfg.NF_SCALP_TIME_STOP_S
    scratch_slippage_rs = cfg.NF_SCALP_SCRATCH_SLIPPAGE_RS

    option_type = "CE" if signal.direction == "BUY" else "PE"
    option_symbol = build_weekly_option_symbol(
        cfg.NF_OPTION_UNDERLYING, datetime.fromisoformat(signal.expiry), signal.strike, option_type)

    return NFTrade(
        direction=signal.direction,
        entry_index_price=signal.entry_index_price,
        entry_time=now.isoformat(),
        target=signal.entry_premium + target_rs,
        current_sl=signal.entry_premium - stop_rs,
        strike=signal.strike,
        option_type=option_type,
        expiry=signal.expiry,
        entry_premium=signal.entry_premium,
        target_rs=target_rs,
        stop_rs=stop_rs,
        time_stop_s=time_stop_s,
        scratch_slippage_rs=scratch_slippage_rs,
        basket_score_at_entry=signal.basket_score,
        wobi_at_entry=signal.wobi,
        lot_size=cfg.NF_LOT_SIZE,
        order_id=order_id,
        confidence=signal.confidence,
        entry_signal=signal,
        option_symbol=option_symbol,
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
