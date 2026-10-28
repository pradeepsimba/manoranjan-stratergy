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
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

import app.config as cfg
from app.services import option_expiries
from app.engine.bn_pricing import (  # noqa: F401 — re-exported for nf_entry_exit.py
    black_scholes,
    normal_cdf,
    time_to_expiry_years,
)
from app.engine.bn_pricing import _last_tuesday_of_month  # reused, not re-exported — see build_weekly_option_symbol
from app.engine.bn_pricing import _previous_trading_day  # reused, not re-exported — see get_next_expiry

_STRIKE_STEP = 50   # Nifty 50's real strike grid — NOT BankNifty's 100-point one (see module docstring)


def get_atm_strike(spot: float) -> int:
    """Nearest 50-point Nifty 50 strike (NF's own grid — see module docstring).
    Round-half-up, not Python's banker's-rounding round() — see
    bn_pricing.get_atm_strike's identical fix (found in review, 2026-09-25)."""
    return int(math.floor(spot / _STRIKE_STEP + 0.5) * _STRIKE_STEP)


def get_itm_strike(spot: float, option_type: str, offset: float) -> int:
    """NF mirror of bn_pricing.get_itm_strike (see there for the full
    wrong-side-of-spot writeup, 2026-10-02 fix, found in review) — falls
    back to floor (CE) / ceil (PE) on THIS module's 50-point grid
    (_STRIKE_STEP), not bn_pricing's 100-point one, ONLY when nearest-
    rounding would land on the wrong side of spot (the danger zone is
    0 < offset < 25 here, half of NF's 50-point grid). offset<=0 is the
    literal exact-ATM case via ordinary nearest-rounding. For every offset
    this repo currently actually uses (150, far outside the danger zone),
    nearest already lands correctly, so this guard never changes the
    selected strike from before this fix."""
    if offset <= 0:
        return get_atm_strike(spot)
    raw = (spot - offset) if option_type == "CE" else (spot + offset)
    nearest = get_atm_strike(raw)
    if option_type == "CE" and nearest >= spot:
        return int(math.floor(raw / _STRIKE_STEP) * _STRIKE_STEP)
    if option_type == "PE" and nearest <= spot:
        return int(math.ceil(raw / _STRIKE_STEP) * _STRIKE_STEP)
    return nearest

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


def _nominal_tuesday(d: date) -> date:
    """The Tuesday a (possibly holiday-shifted-earlier, see get_next_expiry)
    real trading day's weekly contract nominally belongs to — recovers the
    original un-adjusted Tuesday so _is_last_tuesday_of_month can still
    correctly identify month-end contracts after the shift."""
    days_until = (1 - d.weekday()) % 7
    return d + timedelta(days=days_until)


def _is_last_tuesday_of_month(expiry: datetime) -> bool:
    """
    2026-10-15 fix (found in review, alongside the NSE_TRADING_HOLIDAYS
    adjustment in get_next_expiry below): this used to compare `expiry`'s
    OWN date directly against the month's nominal last Tuesday — correct
    when expiry was never holiday-adjusted, but wrong once get_next_expiry
    started shifting a holiday-landing Tuesday back to the previous trading
    day (e.g. 2026-11-24, Guru Nanak Jayanti, is November's real last
    Tuesday — the adjusted expiry lands on 2026-11-23 instead, which no
    longer equals 2026-11-24 under a naive direct comparison, silently
    misclassifying a genuine month-end contract as an ordinary week and
    building the wrong symbol format). Now recovers the NOMINAL Tuesday
    first and compares that instead — the vendor's monthly-vs-weekly symbol
    choice tracks which week's contract this nominally is, not which exact
    day it happens to actually stop trading on.
    """
    nominal = _nominal_tuesday(expiry.date())
    return nominal == _last_tuesday_of_month(nominal.year, nominal.month).date()


def build_weekly_option_symbol(underlying: str, expiry: datetime, strike: int, option_type: str) -> str:
    """
    Real vendor option-instrument symbol for a Nifty 50 expiry. Two
    different real formats exist depending on whether `expiry` is an
    ORDINARY weekly Tuesday or the LAST Tuesday of its month:

    - Ordinary week: WEEKLY format, e.g. "NIFTY2692223250PE" — UNDERLYING +
      2-digit year + single-char month code (see _WEEKLY_MONTH_CODE) +
      2-digit day + strike + CE/PE. Confirmed 2026-09-19 against a real
      user-supplied example symbol.
    - Last Tuesday of the month: MONTHLY-STYLE format, e.g.
      "NIFTY26SEP23100CE" — UNDERLYING + 2-digit year + 3-letter month +
      strike + CE/PE (bn_pricing.build_monthly_option_symbol's own format,
      reused via _last_tuesday_of_month) — even though this IS still a
      weekly Tuesday expiry under NSE's 2025-09-01 convention (see
      get_next_expiry below), the vendor evidently labels the contract that
      also happens to be the month's last expiry using the monthly-style
      symbol, not the compact weekly numeric-date one.

    **Fixed 2026-09-25, found in review** (a real user-reported "waiting for
    live tick" dashboard screenshot, confirmed against the live vendor REST
    endpoint before changing anything): this function used to always build
    the weekly-numeric-date format unconditionally, which returns ZERO
    candles for any month-end expiry week — the live ATM CE/PE watchlist's
    Nifty panel sat on "waiting for live tick" for the ENTIRE final week of
    every month, not intermittently. This was wrongly diagnosed 2026-09-23
    as "the vendor hasn't populated this week's series yet" (that gotcha
    note is now corrected) — the real cause was requesting a symbol that
    was never going to exist under either format. Confirmed live via direct
    REST query: "NIFTY26SEP23100CE"/"PE" (the correct format for the
    2026-09-29 expiry, which IS September's last Tuesday) returned 152 real
    1m candles; "NIFTY2692923100CE"/"PE" (the old weekly-format symbol for
    the exact same expiry/strike) returned zero, queried the identical way.
    Only affects Nifty 50 — BankNifty has no equivalent ambiguity since it
    only ever has the one (monthly) contract type.
    """
    prefix = option_expiries.symbol_prefix(underlying, expiry)   # the broker's own symbol prefix
    if prefix is not None:
        return f"{prefix}{strike}{option_type}"
    if _is_last_tuesday_of_month(expiry):
        return f"{underlying}{expiry.strftime('%y%b').upper()}{strike}{option_type}"
    month_code = _WEEKLY_MONTH_CODE[expiry.month]
    return f"{underlying}{expiry.strftime('%y')}{month_code}{expiry.strftime('%d')}{strike}{option_type}"


def get_next_expiry(now: datetime) -> datetime:
    """
    Nifty 50's real expiry: WEEKLY, next Tuesday 15:30 IST — NSE moved this
    from Thursday, effective 2025-09-01 (confirmed via web search
    2026-09-19; see CLAUDE.md). Deliberately NOT shared with
    bn_pricing.get_next_expiry any more — BankNifty's real cycle diverged to
    monthly (see there); reusing one function for both would silently be
    wrong for whichever one changed.

    **Holiday adjustment (2026-10-15, found in review, real production
    incident):** if the computed Tuesday is an NSE trading holiday, NSE
    shifts that contract's expiry to the previous trading day — confirmed
    live: the 2026-10-20 weekly expiry is Dussehra, and the vendor's REST
    API returned zero candles across 120 strikes for that Tuesday's symbol,
    while the real, holiday-shifted Monday 2026-10-19 symbol returned
    hundreds of real candles per strike. See
    bn_pricing._previous_trading_day/cfg.NSE_TRADING_HOLIDAYS. The "has this
    week's expiry already passed" check below now compares against the
    ADJUSTED expiry (correct either way: with no holiday in play, the
    adjusted and nominal Tuesday 15:30 are identical, so this is a strict
    generalization of the old now-is-Tuesday-past-15:30 special case, not a
    behavior change for the common path).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    broker = option_expiries.next_expiry(cfg.NF_OPTION_UNDERLYING, now)
    if broker is not None:
        return broker
    option_expiries.warn_fallback(cfg.NF_OPTION_UNDERLYING)
    weekday = now.weekday()          # Mon=0 .. Sun=6; Tuesday=1
    days_until = (1 - weekday) % 7
    tuesday_date = (now + timedelta(days=days_until)).date()
    expiry = _adjusted_weekly_expiry(tuesday_date)
    if now > expiry:
        tuesday_date += timedelta(days=7)
        expiry = _adjusted_weekly_expiry(tuesday_date)
    return expiry


def _adjusted_weekly_expiry(tuesday_date: date) -> datetime:
    """15:30 IST on `tuesday_date`, shifted back to the nearest earlier real
    trading day if that Tuesday is an NSE holiday."""
    adj_date = _previous_trading_day(tuesday_date)
    return datetime(adj_date.year, adj_date.month, adj_date.day, 15, 30, tzinfo=IST)


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
