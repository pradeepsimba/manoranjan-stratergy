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
