from __future__ import annotations

"""
Backtest data layer for the Bank Nifty options strategy.

Fetches 5-minute history for BankNifty + the 14 real NIFTY BANK stocks
(fixed universe, app.config.BN_INDEX_TOKEN / BN_ALL_STOCKS — no watchlist/
Gemini selection involved) over the requested range, with extra warmup days
so the realized-vol IV estimate (see bn_pricing.estimate_iv) is valid from
the first bar, and organizes each symbol's bars into a per-day index for
the replay engine.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

import numpy as np

import app.config as cfg
from app.models import Candle
from app.services.historical_data import _fetch_all


@dataclass
class SymbolSeries:
    token:  str
    name:   str
    series: List[Candle]                       # full chronological 5m bars
    by_day:    Dict[str, List[int]]         = field(default_factory=dict)  # "YYYY-MM-DD" -> [idx...]
    at:        Dict[str, Dict[str, int]]    = field(default_factory=dict)  # date -> {"HH:MM": idx}

    # NumPy mirror of `series.close`, built once by index_days(). The replay
    # engine slices this as a zero-copy view instead of rebuilding a float64
    # array from Candle objects on every bar. (highs/lows/vols mirrors were
    # removed 2026-09-22 — confirmed zero readers anywhere in the repo; only
    # .closes is ever actually consumed, by engine.py's IV/lookback slicing.)
    closes: Optional[np.ndarray] = None

    def index_days(self) -> None:
        for i, c in enumerate(self.series):
            d  = c.start_time[:10]
            tm = c.start_time[11:16]
            self.by_day.setdefault(d, []).append(i)
            self.at.setdefault(d, {})[tm] = i

        n = len(self.series)
        self.closes = np.fromiter((c.close for c in self.series), np.float64, n)


def _sort_candles(candles: List[Candle]) -> List[Candle]:
    return sorted(candles, key=lambda c: c.start_time)


def warmup_calendar_days(configured: int, lookback: Optional[int] = None) -> int:
    """
    Enough calendar days before the range for the realized-vol IV estimate
    (see bn_pricing.estimate_iv) to have enough closes to converge on 5m
    bars. `lookback` bars at 5m each → trading days (÷ ~375 session min) →
    calendar days (× 7/5 for weekends), floored at the configured warmup.
    (A `timeframe` parameter used to be here — removed 2026-09-22, it was
    never anything but "5m" at the one call site and the body ignored it.)

    `lookback` is passed explicitly (not read from cfg) because a backtest run
    may OVERRIDE BN_INDICATOR_LOOKBACK_BARS, and that override is only active
    in worker threads — this runs on the event loop where cfg would return
    the global.
    """
    import math
    if lookback is None:
        lookback = cfg.BN_INDICATOR_LOOKBACK_BARS
    mins  = 5
    tdays = math.ceil(lookback * mins / 375.0)
    cdays = math.ceil(tdays * 7.0 / 5.0) + 3
    return max(configured, cdays)


async def load_backtest_data(db, from_d: date, to_d: date,
                             warmup_days: Optional[int] = None,
                             lookback: Optional[int] = None):
    """
    Returns (bn_index, stocks) where:
      bn_index — SymbolSeries for BankNifty (None if no data at all is available)
      stocks   — {token: SymbolSeries} for the 14 real NIFTY BANK stocks

    BankNifty history comes from OUR OWN self-recorded archive (`db`,
    app.services.database.get_bn_index_bars) — the external market-data
    server has no historical archive for the index itself (confirmed
    empirically: every date-range request returns only the current day),
    unlike the stocks, which are fetched from it directly and DO have full
    multi-day history. The archive grows by one day at a time (see
    scheduler._run_eod), so backtest range/depth is bounded by how long the
    live engine has been running, not by this function.

    warmup_days / lookback let a backtest run override the fetch padding
    without touching thread-local config on the event loop.
    """
    tf = cfg.INTERVAL_5M
    if warmup_days is None:
        warmup_days = cfg.BACKTEST_WARMUP_DAYS
    warmup_days = warmup_calendar_days(warmup_days, lookback)
    fetch_from = (from_d - timedelta(days=warmup_days)).isoformat()
    fetch_to   = (to_d + timedelta(days=1)).isoformat()

    stock_list = [{"stockname": n, "stock_symbol": t} for n, t in cfg.BN_ALL_STOCKS.items()]
    stocks_raw, ibars = await asyncio.gather(
        _fetch_all(stock_list, [tf], fetch_from, fetch_to),
        db.get_bn_index_bars(fetch_from, fetch_to),
    )

    bn_index = None
    if ibars:
        bn_index = SymbolSeries(token=cfg.BN_INDEX_TOKEN, name=cfg.BN_INDEX_NAME,
                                series=_sort_candles(ibars))
        bn_index.index_days()

    name_by_token = {t: n for n, t in cfg.BN_ALL_STOCKS.items()}
    stocks: Dict[str, SymbolSeries] = {}
    for token, frames in stocks_raw.items():
        bars = _sort_candles(frames.get(tf, []))
        if not bars:
            continue
        ss = SymbolSeries(token=token, name=name_by_token.get(token, token), series=bars)
        ss.index_days()
        stocks[token] = ss

    return bn_index, stocks
