from __future__ import annotations

"""
Bank Nifty options pricing — pure, stateless functions, no real option-chain
data anywhere. Strike/expiry/premium are entirely synthetic: a theoretical
Black-Scholes premium computed from the BankNifty SPOT price plus a realized-
volatility estimate off its own 5m closes. No live or historical option-market
data is fetched or needed (confirmed absent from both this repo's data source
and the c.html prototype this strategy is ported from).

Ported from c.html's getATMStrike/getNextExpiryDate/getTimeToExpiry/
estimateHistoricalVol/calcBlackScholes/normalCDF, with one deliberate
deviation: the IV estimator uses 5-minute bar-close log-returns instead of
c.html's per-tick price buffer, so the SAME function replays identically in
backtest (which only has bar closes, never a tick stream) and live.
"""

import math
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

import app.config as cfg

IST = ZoneInfo("Asia/Kolkata")

# Trading bars/year for annualizing 5m log-return volatility: ~75 five-minute
# bars per 375-minute NSE session × 252 trading days/year.
_BARS_PER_DAY = 75
_TRADING_DAYS_PER_YEAR = 252


def get_atm_strike(spot: float) -> int:
    """Nearest 100-point BankNifty strike."""
    return int(round(spot / 100.0) * 100)


def build_monthly_option_symbol(underlying: str, expiry: datetime, strike: int, option_type: str) -> str:
    """
    Real vendor MONTHLY option-instrument symbol, e.g. "BANKNIFTY26SEP56400CE"
    — UNDERLYING + 2-digit year + 3-letter uppercase month + strike + CE/PE,
    with NO day-of-month: a monthly contract's expiry day is implied/fixed
    (see get_next_expiry below), so NSE's real trading-symbol convention
    omits it entirely. Used for BankNifty, which NSE restricted to monthly/
    quarterly expiry only (discontinuing weekly contracts, effective
    2025-09-01 — see CLAUDE.md's "External dependencies" note and
    get_next_expiry below). Feeds the real-option-LTP paper-trading feature
    (2026-09-17/19, explicit user decision; live only, never called from
    backtest) — matches a real 2026-09-18 example row from a user-supplied
    vendor instrument-master export ("BANKNIFTY26SEP22300CE" — "26" is the
    YEAR there, not a day, a mis-read corrected 2026-09-19) but is still
    NOT independently confirmed against the live feed for a CURRENT
    contract — if a subscribed symbol never produces a real tick, suspect
    this format first (same gotcha class as the Kotak Bank naming issue).
    """
    return f"{underlying}{expiry.strftime('%y%b').upper()}{strike}{option_type}"


def get_next_expiry(now: datetime) -> datetime:
    """
    BankNifty's real expiry: MONTHLY, on the LAST TUESDAY of the month,
    15:30 IST — NSE discontinued BankNifty weekly options and moved the
    monthly/quarterly expiry day from Thursday to Tuesday, both effective
    2025-09-01 (confirmed via web search 2026-09-19; see CLAUDE.md). If the
    current month's last Tuesday has already passed (the date is past, or
    it's today but past 15:30), rolls to next month's last Tuesday.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    expiry = _last_tuesday_of_month(now.year, now.month)
    if now > expiry:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        expiry = _last_tuesday_of_month(year, month)
    return expiry


def _last_tuesday_of_month(year: int, month: int) -> datetime:
    """15:30 IST on the last Tuesday of (year, month)."""
    first_of_next = (datetime(year + 1, 1, 1, tzinfo=IST) if month == 12
                     else datetime(year, month + 1, 1, tzinfo=IST))
    last_day = first_of_next - timedelta(days=1)
    offset = (last_day.weekday() - 1) % 7   # Tuesday = 1
    last_tue = last_day - timedelta(days=offset)
    return last_tue.replace(hour=15, minute=30, second=0, microsecond=0)


def time_to_expiry_years(now: datetime, expiry: datetime) -> float:
    """Calendar-day fraction of a year to expiry — matches c.html (365-day convention)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=IST)
    seconds = (expiry - now).total_seconds()
    return max(0.0, seconds / (365.0 * 86400.0))


def normal_cdf(x: float) -> float:
    """Abramowitz & Stegun 7.1.26 approximation of the standard normal CDF."""
    a1, a2, a3, a4, a5 = (0.254829592, -0.284496736, 1.421413741,
                          -1.453152027, 1.061405429)
    p = 0.3275911
    sign = -1.0 if x < 0 else 1.0
    ax = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * ax)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-ax * ax)
    return 0.5 * (1.0 + sign * y)


def black_scholes(S: float, K: float, T: float, r: float, sigma: float,
                  option_type: str) -> dict:
    """
    Standard European Black-Scholes. `option_type` is "CE" (call) or "PE" (put).
    T<=0 (at/after expiry) degenerates to pure intrinsic value.
    Returns {"price", "delta", "gamma", "theta"} (theta per calendar day, ≤ 0).
    """
    is_call = option_type == "CE"
    if T <= 0 or sigma <= 0:
        if is_call:
            price, delta = max(0.0, S - K), (1.0 if S > K else 0.0)
        else:
            price, delta = max(0.0, K - S), (-1.0 if S < K else 0.0)
        return {"price": price, "delta": delta, "gamma": 0.0, "theta": 0.0}

    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    disc = math.exp(-r * T)
    if is_call:
        price = S * normal_cdf(d1) - K * disc * normal_cdf(d2)
        delta = normal_cdf(d1)
    else:
        price = K * disc * normal_cdf(-d2) - S * normal_cdf(-d1)
        delta = normal_cdf(d1) - 1.0
    price = max(0.0, price)

    phi_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma = phi_d1 / (S * sigma * sqrt_t)

    if is_call:
        theta_annual = (-(S * phi_d1 * sigma) / (2 * sqrt_t) - r * K * disc * normal_cdf(d2))
    else:
        theta_annual = (-(S * phi_d1 * sigma) / (2 * sqrt_t) + r * K * disc * normal_cdf(-d2))
    theta = -abs(theta_annual / 365.0)

    return {"price": price, "delta": delta, "gamma": gamma, "theta": theta}


def estimate_iv(closes: np.ndarray, manual_override: Optional[float] = None) -> float:
    """
    Realized-volatility IV proxy from 5m bar-close log-returns — used
    identically by live (fed in-memory candle closes) and backtest (fed
    bars [..t] only, no look-ahead). Clamped to [BN_IV_MIN, BN_IV_MAX].
    """
    if manual_override is not None:
        return manual_override
    if cfg.BN_IV_MANUAL_ENABLED:
        return float(cfg.BN_IV_MANUAL_VALUE)

    lookback = cfg.BN_IV_LOOKBACK_BARS
    tail = closes[-(lookback + 1):] if closes.size > lookback else closes
    if tail.size < 4:
        return float(cfg.BN_IV_DEFAULT)

    log_returns = np.diff(np.log(tail))
    log_returns = log_returns[np.isfinite(log_returns)]
    if log_returns.size < 3:
        return float(cfg.BN_IV_DEFAULT)

    std_per_bar = float(np.std(log_returns, ddof=1))
    annual_vol = std_per_bar * math.sqrt(_BARS_PER_DAY * _TRADING_DAYS_PER_YEAR)
    return max(cfg.BN_IV_MIN, min(cfg.BN_IV_MAX, annual_vol))
