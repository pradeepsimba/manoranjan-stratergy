from __future__ import annotations

"""
Nifty 50 options pricing — parallel to bn_pricing.py. Strike/expiry/Black-
Scholes math is instrument-agnostic (spot, strike, time-to-expiry, rate, IV
in; premium/greeks out), so those functions are reused directly from
bn_pricing.py rather than copied. Only estimate_iv reads instrument-specific
cfg (NF_IV_* instead of BN_IV_*), so it's the only function duplicated here.
"""

import math
from typing import Optional

import numpy as np

import app.config as cfg
from app.engine.bn_pricing import (  # noqa: F401 — re-exported for nf_entry_exit.py
    black_scholes,
    get_atm_strike,
    get_next_expiry,
    normal_cdf,
    time_to_expiry_years,
)

_BARS_PER_DAY = 75
_TRADING_DAYS_PER_YEAR = 252


def estimate_iv(closes: np.ndarray, manual_override: Optional[float] = None) -> float:
    """NF mirror of bn_pricing.estimate_iv — reads cfg.NF_IV_* instead of cfg.BN_IV_*."""
    if manual_override is not None:
        return manual_override
    if cfg.NF_IV_MANUAL_ENABLED:
        return float(cfg.NF_IV_MANUAL_VALUE)

    lookback = cfg.NF_IV_LOOKBACK_BARS
    tail = closes[-(lookback + 1):] if closes.size > lookback else closes
    if tail.size < 4:
        return float(cfg.NF_IV_DEFAULT)

    log_returns = np.diff(np.log(tail))
    log_returns = log_returns[np.isfinite(log_returns)]
    if log_returns.size < 3:
        return float(cfg.NF_IV_DEFAULT)

    std_per_bar = float(np.std(log_returns, ddof=1))
    annual_vol = std_per_bar * math.sqrt(_BARS_PER_DAY * _TRADING_DAYS_PER_YEAR)
    return max(cfg.NF_IV_MIN, min(cfg.NF_IV_MAX, annual_vol))
