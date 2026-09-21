from __future__ import annotations

"""
Realistic fill + cost model for the Bank Nifty options backtest.

Two distinct things are being "filled" here:
  * The underlying BankNifty index price that determines WHEN target/stop is
    touched — gap-at-open + intrabar high/low, same convention this repo's
    equity backtest used (SL wins a same-bar tie).
  * The option PREMIUM actually traded — slippage is applied here (the index
    price is a model input, not a tradable leg).

Options cost model (brokerage/STT/txn/GST/SEBI on premium turnover) uses
PLACEHOLDER rates — confirm current India options charges before trusting
absolute backtest ₹ P&L; relative signal quality isn't sensitive to this.
"""

from typing import Optional, Tuple

import app.config as cfg
from app.models import Candle


# ── Underlying index touch resolution (gap-at-open + intrabar) ──────────────

def resolve_index_touch(direction: str, sl_level: float, target_level: float,
                        bar: Candle) -> Optional[Tuple[float, str]]:
    """
    Whether THIS bar touches `sl_level`/`target_level` (as they stood BEFORE
    the bar), gap-at-open aware. Returns (exit_index_price, outcome) or None.
    SL wins a same-bar tie (assume the adverse move came first).
    """
    if direction == "BUY":
        if bar.open <= sl_level:
            return bar.open, "STOP"
        if bar.open >= target_level:
            return bar.open, "TARGET"
        if bar.low <= sl_level:
            return sl_level, "STOP"
        if bar.high >= target_level:
            return target_level, "TARGET"
    else:
        if bar.open >= sl_level:
            return bar.open, "STOP"
        if bar.open <= target_level:
            return bar.open, "TARGET"
        if bar.high >= sl_level:
            return sl_level, "STOP"
        if bar.low <= target_level:
            return target_level, "TARGET"
    return None


# ── Option-premium touch resolution (2026-09-21 scalp-strategy rewrite) ────

def resolve_premium_touch(sl_level: float, target_level: float,
                          premium_open: float, premium_high: float,
                          premium_low: float) -> Optional[Tuple[float, str]]:
    """
    Generic touch resolution on a PREMIUM range, gap-at-open + intrabar
    aware — same convention as resolve_index_touch above, generalized off
    an already-direction-normalized level series instead of the underlying
    index specifically. This strategy's target/stop are always "premium up
    = win, premium down = loss" regardless of CE/PE (see
    bn_entry_exit.evaluate_exit), so there is only ONE branch here, unlike
    resolve_index_touch's BUY/SELL split. STOP wins a same-bar tie (assume
    the adverse move came first).

    Caller computes premium_high/premium_low from Black-Scholes at the
    bar's index high/low (max/min of the two, since a CE's premium rises
    with the index and a PE's falls — see engine.py's _try_exit) — this
    function only ever compares against already-resolved levels.

    KNOWN LIMITATION: this strategy's real lifecycle is a hard 12-SECOND
    window (BN_SCALP_TIME_STOP_S/NF_SCALP_TIME_STOP_S); this repo has no
    historical data at sub-5-minute granularity anywhere (see CLAUDE.md).
    A 5m bar's OHLC-implied premium range is therefore a coarse proxy for
    "did target/stop get touched sometime in this window", not a faithful
    reconstruction of what happened in the specific first 12 seconds after
    entry — engine.py's _try_exit documents the resulting approximation
    (effectively: resolve on the FIRST bar after entry, via this touch
    check, else a forced TIME_SCRATCH at that bar's close since 12s has by
    then long since elapsed relative to the bar).
    """
    if premium_open <= sl_level:
        return sl_level, "STOP"
    if premium_open >= target_level:
        return target_level, "TARGET"
    if premium_low <= sl_level:
        return sl_level, "STOP"
    if premium_high >= target_level:
        return target_level, "TARGET"
    return None


# ── Option premium slippage ───────────────────────────────────────────────────

def slip_buy_premium(premium: float, bps: float) -> float:
    return premium * (1 + bps / 10_000.0)


def slip_sell_premium(premium: float, bps: float) -> float:
    return max(0.0, premium * (1 - bps / 10_000.0))


# ── Options costs ──────────────────────────────────────────────────────────────

def round_trip_costs_options(buy_premium_value: float, sell_premium_value: float) -> float:
    """Total transaction cost for one buy + one sell leg on OPTION premium turnover (absolute ₹)."""
    brokerage = cfg.BN_COST_BROKERAGE_FLAT * 2   # flat per executed order, both legs
    stt   = cfg.BN_COST_STT_SELL_PCT * sell_premium_value
    txn   = cfg.BN_COST_TXN_PCT * (buy_premium_value + sell_premium_value)
    gst   = cfg.BN_COST_GST_PCT * (brokerage + txn)
    sebi  = cfg.BN_COST_SEBI_PCT * (buy_premium_value + sell_premium_value)
    return brokerage + stt + txn + gst + sebi
