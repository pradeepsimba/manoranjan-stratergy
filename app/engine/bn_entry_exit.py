from __future__ import annotations

"""
Shared Bank Nifty entry/exit decision core — called identically by the live
scheduler and the backtest replay loop (this repo's hard convention: live
and backtest must share one strategy core).

REWRITTEN 2026-09-21 (explicit user decision) — REPLACES the prior 9-of-14-
same-direction leader vote AND the index-points target/stop entirely with a
Top-8 weighted-basket VWAP-momentum entry + deep-ITM strike + W-OBI
execution filter + a 12-second premium-based target/stop/time-scratch exit.
See config.py's "Static: Scalping strategy" block for every tunable this
introduces, and app/engine/scalp_signals.py / app/engine/wobi.py for the
two new pure-function modules this pulls in.

bn_signals.py (the sideways/momentum/leader-vote/composite-indicator gates
this replaced) and its dedicated config constants were fully DELETED
2026-09-21 — this repo's earlier "kept unused for revert safety" stance
(the 2026-09-19 rewrite's own comments) was explicitly superseded by a
follow-up user decision to actually remove dead code once the replacement
was confirmed working. BN_SAME_DIRECTION_REQUIRED survives in config.py
only because app/backtest/signal_study.py (a standalone, unrelated
historical-analysis tool) still reads it.

_leader_qty_surge/_stock_qty_threshold below are UNRELATED to this
strategy — kept only because scheduler.py separately uses them to annotate
the (informational, non-decision) stockCandles payload's "surged" flag for
the leader stocks. Do not wire them into evaluate_entry.
"""

from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

import numpy as np

import app.config as cfg
from app.engine.bn_pricing import (
    black_scholes,
    build_monthly_option_symbol,
    estimate_iv,
    get_itm_strike,
    get_next_expiry,
    time_to_expiry_years,
)
from app.engine.scalp_signals import compute_basket_reading
from app.engine.wobi import compute_wobi, synthetic_depth
from app.models import BNDiagnostic, BNSignal, BNTrade, Candle, PositionStatus


def _stock_qty_threshold(name: str) -> float:
    """
    A leader stock's live volume-surge threshold — UNRELATED to the scalp
    strategy above; kept only for scheduler.py's informational stockCandles
    "surged" annotation. See cfg.BN_QTY_THRESHOLD_ATTR.
    """
    attr = cfg.BN_QTY_THRESHOLD_ATTR.get(name)
    base = getattr(cfg, attr) if attr else 10_000
    return base * cfg.BN_QTY_INTERVAL_MULTIPLIER


def _leader_qty_surge(leader_recent: Dict[str, List[Candle]]) -> Dict[str, bool]:
    """UNRELATED to the scalp strategy — see _stock_qty_threshold above."""
    out: Dict[str, bool] = {}
    for name, candles in leader_recent.items():
        out[name] = bool(candles) and candles[-1].volume >= _stock_qty_threshold(name)
    return out


def _in_trading_window(now: datetime) -> bool:
    """
    Risk guardrail: entries only inside the two configured windows (default
    09:45-11:15 / 13:45-14:45 IST) — see config.py's SCALP_WINDOW1/2_*.
    Shared by BN and NF (one set of windows, not a pair per instrument).
    """
    t = now.time()
    w1 = (time(cfg.SCALP_WINDOW1_START_HOUR, cfg.SCALP_WINDOW1_START_MIN)
          <= t <= time(cfg.SCALP_WINDOW1_END_HOUR, cfg.SCALP_WINDOW1_END_MIN))
    w2 = (time(cfg.SCALP_WINDOW2_START_HOUR, cfg.SCALP_WINDOW2_START_MIN)
          <= t <= time(cfg.SCALP_WINDOW2_END_HOUR, cfg.SCALP_WINDOW2_END_MIN))
    return w1 or w2


def evaluate_entry(
    now: datetime,
    bn_recent_candles: List[Candle],
    bn_closes_lookback: np.ndarray,
    basket_candles: Dict[str, List[Candle]],
    last_exit_time: Optional[datetime] = None,
    trades_today: int = 0,
    current_index_price: Optional[float] = None,
    basket_ltp: Optional[Dict[str, float]] = None,
) -> Tuple[Optional[BNSignal], BNDiagnostic]:
    """
    Evaluate a scalp entry against the CURRENT market state.

    Evaluated every tick (2026-09-22, explicit user decision — was
    previously once per closed 5m bar): `current_index_price`/`basket_ltp`
    let the live scheduler feed the CURRENT live tick price for BankNifty
    and each basket leg, so the score reacts within ~100ms of a real move
    instead of waiting up to 5 minutes for the next bar close. Both
    default to None, in which case this falls back to the last available
    bar close (`bn_recent_candles[-1].close` / each leg's own last close)
    — that fallback is what backtest uses, since it has no tick stream at
    all (see scalp_signals.compute_basket_reading's own docstring) — this
    is the one deliberate, CALLER-level live/backtest divergence this
    function allows, not a fork of the decision logic itself.

    1. Composite score = sum(weight_i * (ltp_i-vwap_i)/vwap_i*100) over
       cfg.BN_SCALP_BASKET's 8 heaviest-weighted BN constituents (VWAP is a
       5m-bar typical-price VWAP, not tick-level — see scalp_signals.py's
       module docstring for why).
    2. Fires BUY when score >= +BN_SCALP_SCORE_THRESHOLD, SELL when score
       <= -BN_SCALP_SCORE_THRESHOLD, AND the 2 heaviest-weighted basket legs
       both deviate in that same direction (top2_direction_ok) — mirrors
       the strategy spec's "top 2 (Reliance & HDFC) moving in the same
       direction" confirmation, generalized to whichever 2 legs are
       heaviest in THIS basket.
    3. Deep-ITM strike off the just-closed close (bn_pricing.get_itm_strike,
       BN_ITM_OFFSET_POINTS).
    4. W-OBI execution filter (app.engine.wobi) on that option's own
       synthetic depth must clear BN_WOBI_MIN_RATIO before the signal
       actually fires.

    `basket_candles` must already be sliced to CLOSED bars only (same
    convention the old leader_recent dict used) — the caller (scheduler.py
    / the backtest engine) builds it from cfg.BN_SCALP_BASKET's 8 tokens.
    `trades_today` is the caller's running count of trades opened so far
    today (the SCALP_MAX_TRADES_PER_DAY guardrail) — mirrors how
    last_exit_time is caller-supplied runtime state, not something this
    pure function tracks itself.
    """
    bn_bar_time = bn_recent_candles[-1].start_time if bn_recent_candles else ""
    bn_close = current_index_price if current_index_price else (
        bn_recent_candles[-1].close if bn_recent_candles else 0.0)

    no_trade_reason: Optional[str] = None

    cooldown_ok = True
    if last_exit_time is not None:
        elapsed = (now - last_exit_time).total_seconds()
        if elapsed < cfg.BN_SCALP_COOLDOWN_S:
            cooldown_ok = False
            no_trade_reason = f"Cooldown {cfg.BN_SCALP_COOLDOWN_S - elapsed:.0f}s remaining"

    window_ok = _in_trading_window(now)
    if no_trade_reason is None and not window_ok:
        no_trade_reason = "Outside scalp trading window (09:45-11:15 / 13:45-14:45 IST)"

    max_trades_ok = trades_today < cfg.SCALP_MAX_TRADES_PER_DAY
    if no_trade_reason is None and not max_trades_ok:
        no_trade_reason = f"Max {cfg.SCALP_MAX_TRADES_PER_DAY} trades/day reached"

    name_by_token = {tok: name for name, tok in cfg.BN_ALL_STOCKS.items()}
    reading = compute_basket_reading(cfg.BN_SCALP_BASKET, basket_candles, name_by_token,
                                     now.date(), ltp_by_token=basket_ltp)
    threshold = cfg.BN_SCALP_SCORE_THRESHOLD

    score_buy_ok = reading.score >= threshold
    score_sell_ok = reading.score <= -threshold
    if no_trade_reason is None and not (score_buy_ok or score_sell_ok):
        no_trade_reason = f"Basket score {reading.score:+.3f} within ±{threshold}"
    elif no_trade_reason is None and not reading.top2_direction_ok:
        no_trade_reason = f"Top-2 ({', '.join(n for n in reading.top2_names if n)}) not confirming direction"

    gates_clear = cooldown_ok and window_ok and max_trades_ok
    buy_ready = gates_clear and score_buy_ok and reading.top2_direction_ok
    sell_ready = gates_clear and score_sell_ok and reading.top2_direction_ok

    # Deep-ITM CE/PE strike/premium — computed unconditionally (both sides)
    # for the live dashboard's "what would this cost right now" display,
    # same convention the old ATM quote used.
    itm_iv: Optional[float] = None
    itm_ce_strike = itm_pe_strike = None
    itm_ce_premium = itm_pe_premium = None
    itm_expiry = get_next_expiry(now)
    if bn_close > 0:
        T = time_to_expiry_years(now, itm_expiry)
        itm_iv = estimate_iv(bn_closes_lookback)
        itm_ce_strike = get_itm_strike(bn_close, "CE", cfg.BN_ITM_OFFSET_POINTS)
        itm_pe_strike = get_itm_strike(bn_close, "PE", cfg.BN_ITM_OFFSET_POINTS)
        itm_ce_premium = black_scholes(bn_close, itm_ce_strike, T, cfg.BN_RISK_FREE_RATE, itm_iv, "CE")["price"]
        itm_pe_premium = black_scholes(bn_close, itm_pe_strike, T, cfg.BN_RISK_FREE_RATE, itm_iv, "PE")["price"]

    signal: Optional[BNSignal] = None
    wobi_value: Optional[float] = None
    if (buy_ready or sell_ready) and bn_close > 0:
        direction = "BUY" if buy_ready else "SELL"
        option_type = "CE" if direction == "BUY" else "PE"
        strike = itm_ce_strike if option_type == "CE" else itm_pe_strike
        premium = itm_ce_premium if option_type == "CE" else itm_pe_premium

        # seed keys on the basket score ITSELF (rounded to 3dp), not
        # bn_bar_time and NOT wall-clock time. Two failed attempts, in
        # order:
        #  1. bn_bar_time (the forming bar's start) — constant for up to 5
        #     minutes, so every tick within the same bar got the IDENTICAL
        #     synthetic depth/W-OBI verdict, silently freezing this gate
        #     even while score/top2 above genuinely re-sample every tick.
        #  2. now.isoformat() (tick-precision wall clock) — swung too far
        #     the other way: a FRESH independent random roll every ~100ms
        #     means a persistent signal gets 10-20+ independent tries per
        #     second, so the cumulative chance of at least one clearing
        #     BN_WOBI_MIN_RATIO approaches ~100% within a second or two —
        #     defeating W-OBI as a real filter (found in review).
        # Keying on the score instead ties W-OBI to something that only
        # changes when the market genuinely moves (score is itself driven
        # by the live tick every cycle — see compute_basket_reading), with
        # no free re-rolls: the same score always yields the same verdict,
        # so a marginal signal can't just wait out a lucky dice roll.
        depth = synthetic_depth(cfg.BN_LOT_SIZE, itm_iv, reading.score,
                                seed=f"BN{option_type}{strike}:{reading.score:.3f}")
        wobi_value = compute_wobi(depth)
        wobi_ok = wobi_value > cfg.BN_WOBI_MIN_RATIO

        if wobi_ok:
            signal = BNSignal(
                direction=direction,
                entry_index_price=bn_close,
                bar_time=bn_bar_time,
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
            no_trade_reason = f"W-OBI {wobi_value:.2f} ≤ {cfg.BN_WOBI_MIN_RATIO} (path not clear)"

    diagnostic = BNDiagnostic(
        time=bn_bar_time,
        bn_ltp=bn_close,
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
        cooldown_ms=0.0 if cooldown_ok else max(0.0, cfg.BN_SCALP_COOLDOWN_S -
                                                 (now - last_exit_time).total_seconds()) * 1000.0,
        market_open=True,
        atm_strike=itm_ce_strike if reading.score >= 0 else itm_pe_strike,
        atm_premium=itm_ce_premium,   # kept for the (currently unused) Entry Loop Monitor UI
        atm_iv=itm_iv,
        atm_ce_premium=itm_ce_premium,
        atm_pe_premium=itm_pe_premium,
        # momentum_ok/macd_dir/ema_bullish/ema_bearish/bn_bullish/bn_bearish
        # are dashboard.js's Entry Loop Monitor fields for the now-removed
        # composite-indicator gate — mirrored onto the basket-score
        # condition (same True/False as dir_count_ok below) rather than
        # left at their False/None dataclass defaults, which would
        # permanently cap the "N/14 gates passed" banner below "ready" even
        # at the exact instant a real trade fires (found in review).
        momentum_ok=score_buy_ok or score_sell_ok,
        macd_dir=("BUY" if score_buy_ok else ("SELL" if score_sell_ok else None)),
        ema_bullish=score_buy_ok,
        ema_bearish=score_sell_ok,
        bn_bullish=score_buy_ok,
        bn_bearish=score_sell_ok,
        cooldown_ok=cooldown_ok,
        sideways_ok=True,    # no longer a real gate — see the module docstring
        dir_count_ok=score_buy_ok or score_sell_ok,
        qty_surge_ok=True,   # no longer a real gate — see the module docstring
        same_direction_required=0,
        gates_clear=gates_clear,
        entry_ready=signal is not None,
        basket_score=round(reading.score, 4),
        score_threshold=threshold,
        top2_ok=reading.top2_direction_ok,
        top2_names=[n for n in reading.top2_names if n],
        wobi=wobi_value,
        wobi_min_ratio=cfg.BN_WOBI_MIN_RATIO,
        window_ok=window_ok,
        trades_today=trades_today,
        max_trades_today=cfg.SCALP_MAX_TRADES_PER_DAY,
        itm_offset_points=cfg.BN_ITM_OFFSET_POINTS,
        target_rs=cfg.BN_SCALP_TARGET_RS,
        stop_rs=cfg.BN_SCALP_STOP_RS,
        time_stop_s=cfg.BN_SCALP_TIME_STOP_S,
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
    exit_reason: Optional[str] = None   # "TARGET" | "STOP" | "TIME_SCRATCH"


def evaluate_exit(trade: BNTrade, now: datetime, current_index_price: float,
                  bn_closes_lookback: np.ndarray,
                  live_premium_override: Optional[float] = None) -> ExitEvaluation:
    """
    12-second premium-based lifecycle (2026-09-21) — REPLACES the old
    index-points target/stop/breakeven/trail state machine entirely.
    trade.target/trade.current_sl are now absolute PREMIUM levels, frozen at
    entry as entry_premium + target_rs / entry_premium - stop_rs (see
    open_trade_from_signal below) — no direction branching needed: this
    strategy is always LONG one option leg (CE or PE), so "premium up
    target_rs = win, premium down stop_rs = loss" applies identically
    either way. If neither is touched before trade.time_stop_s elapses
    since entry, force a TIME_SCRATCH exit at a marketable (spread-crossing)
    price — see BN_SCALP_SCRATCH_SLIPPAGE_RS in config.py.

    `live_premium_override`: when the caller (bn_trade.check_tick_exit) has
    a real option LTP for this trade (premium_synthetic has latched False —
    see CLAUDE.md's "Real-option-LTP paper trading"), it passes that HERE
    so the EXIT DECISION itself uses the real tick, not only the
    Black-Scholes mark this function always computes as a fallback/display
    value. Caller-level handling, per this repo's "don't fork the shared
    evaluate_exit" convention — this function stays live/backtest-identical
    either way (backtest never passes an override).
    """
    expiry = datetime.fromisoformat(trade.expiry)
    T = time_to_expiry_years(now, expiry)
    iv = estimate_iv(bn_closes_lookback)
    bs = black_scholes(current_index_price, trade.strike, T, cfg.BN_RISK_FREE_RATE, iv, trade.option_type)
    premium = live_premium_override if live_premium_override is not None else bs["price"]

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
        settle_premium = max(0.0, premium - cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS)

    return ExitEvaluation(
        new_sl=trade.current_sl, sl_stage=trade.sl_stage,
        current_premium=settle_premium if should_exit else premium,
        current_iv=iv, current_delta=bs["delta"], current_theta=bs["theta"],
        should_exit=should_exit, exit_reason=exit_reason,
    )


def open_trade_from_signal(signal: BNSignal, now: datetime, order_id: str = "") -> BNTrade:
    """
    Convert a fired BNSignal into the single active BNTrade, freezing this
    trade's target_rs/stop_rs/time_stop_s from the CURRENT cfg values (and
    deriving target/current_sl as absolute premium levels from them) — a
    later Settings-page/config change must never retroactively alter an
    already-open trade's economics.
    """
    target_rs = cfg.BN_SCALP_TARGET_RS
    stop_rs = cfg.BN_SCALP_STOP_RS
    time_stop_s = cfg.BN_SCALP_TIME_STOP_S

    option_type = "CE" if signal.direction == "BUY" else "PE"
    option_symbol = build_monthly_option_symbol(
        cfg.BN_OPTION_UNDERLYING, datetime.fromisoformat(signal.expiry), signal.strike, option_type)

    return BNTrade(
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
        basket_score_at_entry=signal.basket_score,
        wobi_at_entry=signal.wobi,
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
    P&L x lot size (unchanged from before this rewrite) — index_pnl_points
    is kept purely as a diagnostic.
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
