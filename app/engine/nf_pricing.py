from __future__ import annotations

"""
Nifty 50 options pricing — parallel to bn_pricing.py. Black-Scholes math is
instrument-agnostic (spot, strike, time-to-expiry, rate, IV in; premium/
greeks out), so those functions are reused directly from bn_pricing.py
rather than copied. estimate_iv reads instrument-specific cfg (NF_IV_*
instead of BN_IV_*), so it's duplicated here — and, as of 2026-09-19,
get_next_expiry/the option-symbol builder are ALSO no longer shared: NSE's
real expiry rules genuinely diverged between the two indices (see below),
so reusing one function for both would be wrong for at least one of them,
not just redundant.

get_atm_strike/get_itm_strike are ALSO NOT reused from bn_pricing.py (fixed
2026-09-23, found in review): BankNifty's real strike grid is 100 points,
but Nifty 50's real strike grid is 50 points (see NF_ITM_OFFSET_POINTS's own
comment in config.py, "~3 strikes ITM at Nifty 50's real 50-point strike
step") — bn_pricing.get_atm_strike hardcodes `round(spot/100)*100`, which
was being reused here unmodified and silently rounded every NF strike
(entry ITM selection, manual orders, and the live ATM watchlist) to
BankNifty's coarser grid, missing the true nearest 50-point strike roughly
half the time.
"""

import math
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

import app.config as cfg
from app.engine.bn_pricing import (  # noqa: F401 — re-exported for nf_entry_exit.py
    black_scholes,
    normal_cdf,
    time_to_expiry_years,
)

_STRIKE_STEP = 50   # Nifty 50's real strike grid — NOT BankNifty's 100-point one (see module docstring)


def get_atm_strike(spot: float) -> int:
    """Nearest 50-point Nifty 50 strike (NF's own grid — see module docstring)."""
    return int(round(spot / _STRIKE_STEP) * _STRIKE_STEP)


def get_itm_strike(spot: float, option_type: str, offset: float) -> int:
    """NF mirror of bn_pricing.get_itm_strike, rounded via THIS module's
    get_atm_strike (50-point grid), not bn_pricing's (100-point) one."""
    raw = (spot - offset) if option_type == "CE" else (spot + offset)
    return get_atm_strike(raw)

IST = ZoneInfo("Asia/Kolkata")

_BARS_PER_DAY = 75
_TRADING_DAYS_PER_YEAR = 252

# Single-character month code NSE's real weekly-option trading symbols use
# in place of a 3-letter month name (1-9 for Jan-Sep, O/N/D for Oct/Nov/Dec)
# — confirmed 2026-09-19 against a real user-supplied example symbol
# ("NIFTY2692223250PE" = year 26, month code "9" for September, day 22,
# strike 23250, PE).
_WEEKLY_MONTH_CODE = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
                      7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}


def build_weekly_option_symbol(underlying: str, expiry: datetime, strike: int, option_type: str) -> str:
    """
    Real vendor WEEKLY option-instrument symbol, e.g. "NIFTY2692223250PE" —
    UNDERLYING + 2-digit year + single-char month code (see
    _WEEKLY_MONTH_CODE) + 2-digit day + strike + CE/PE. Used for Nifty 50,
    which still has weekly expiry (moved from Thursday to Tuesday — see
    get_next_expiry below). Confirmed 2026-09-19 against a real user-
    supplied example symbol — unlike bn_pricing.build_monthly_option_symbol,
    which remains unconfirmed against the live feed for a current contract.
    Feeds the real-option-LTP paper-trading feature (2026-09-17, explicit
    user decision; live only, never called from backtest).
    """
    month_code = _WEEKLY_MONTH_CODE[expiry.month]
    return f"{underlying}{expiry.strftime('%y')}{month_code}{expiry.strftime('%d')}{strike}{option_type}"


def get_next_expiry(now: datetime) -> datetime:
    """
    Nifty 50's real expiry: WEEKLY, next Tuesday 15:30 IST — NSE moved this
    from Thursday, effective 2025-09-01 (confirmed via web search
    2026-09-19; see CLAUDE.md). If `now` IS a Tuesday past 15:30, that
    week's expiry has already happened intraday — roll to next week's.
    Deliberately NOT shared with bn_pricing.get_next_expiry any more —
    BankNifty's real cycle diverged to monthly (see there); reusing one
    function for both would silently be wrong for whichever one changed.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    weekday = now.weekday()          # Mon=0 .. Sun=6; Tuesday=1
    days_until = (1 - weekday) % 7
    expiry = (now + timedelta(days=days_until)).replace(
        hour=15, minute=30, second=0, microsecond=0)
    if days_until == 0 and now > expiry:
        expiry += timedelta(days=7)
    return expiry


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
