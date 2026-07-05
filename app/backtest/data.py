from __future__ import annotations

"""
Backtest data layer.

Pulls the trading universe from the live client-status endpoint, fetches 5-minute
history for every symbol (plus NIFTY) over the requested range — with extra
warmup days so indicators are valid from the first bar — and organizes each
symbol's bars into a per-day index for the replay engine.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

import numpy as np

import app.config as cfg
from app.engine.watchlist import fetch_active_watchlist
from app.models import Candle
from app.services.historical_data import _fetch_all


@dataclass
class SymbolSeries:
    token:  str
    name:   str
    series: List[Candle]                       # full chronological 5m bars
    by_day:    Dict[str, List[int]]         = field(default_factory=dict)  # "YYYY-MM-DD" -> [idx...]
    at:        Dict[str, Dict[str, int]]    = field(default_factory=dict)  # date -> {"HH:MM": idx}
    hour_open: Dict[str, Dict[str, float]]  = field(default_factory=dict)  # date -> {HH: open}

    # NumPy mirrors of `series`, built once by index_days(). The replay engine
    # slices these as zero-copy views instead of rebuilding float64 arrays from
    # Candle objects on every scan. cum_pv/cum_v are prefix sums that make the
    # session VWAP an O(1) subtraction (see session_vwap_from_cumsums).
    closes: Optional[np.ndarray] = None
    highs:  Optional[np.ndarray] = None
    lows:   Optional[np.ndarray] = None
    vols:   Optional[np.ndarray] = None
    cum_pv: Optional[np.ndarray] = None   # cumsum of (H+L+C)·V — VWAP numerator ×3
    cum_v:  Optional[np.ndarray] = None   # cumsum of V

    def index_days(self) -> None:
        for i, c in enumerate(self.series):
            d  = c.start_time[:10]
            tm = c.start_time[11:16]
            hr = c.start_time[11:13]
            self.by_day.setdefault(d, []).append(i)
            self.at.setdefault(d, {})[tm] = i
            self.hour_open.setdefault(d, {}).setdefault(hr, self.series[i].open)

        n = len(self.series)
        self.closes = np.fromiter((c.close  for c in self.series), np.float64, n)
        self.highs  = np.fromiter((c.high   for c in self.series), np.float64, n)
        self.lows   = np.fromiter((c.low    for c in self.series), np.float64, n)
        self.vols   = np.fromiter((c.volume for c in self.series), np.float64, n)
        self.cum_pv = ((self.highs + self.lows + self.closes) * self.vols).cumsum()
        self.cum_v  = self.vols.cumsum()


def _sort_candles(candles: List[Candle]) -> List[Candle]:
    return sorted(candles, key=lambda c: c.start_time)


def warmup_calendar_days(timeframe: str, configured: int,
                         lookback: Optional[int] = None) -> int:
    """
    Enough calendar days before the range for indicators to converge at the
    chosen timeframe. `lookback` bars at `timeframe` minutes → trading days
    (÷ ~375 session min) → calendar days (× 7/5 for weekends), floored at the
    configured warmup so intraday TFs keep today's ≥7-day default.

    `lookback` is passed explicitly (not read from cfg) because a backtest run
    may OVERRIDE TALIB_LOOKBACK, and that override is only active in worker
    threads — this runs on the event loop where cfg would return the global.
    """
    import math
    if lookback is None:
        lookback = cfg.TALIB_LOOKBACK
    mins  = cfg.TIMEFRAME_MINUTES.get(timeframe, 5)
    tdays = math.ceil(lookback * mins / 375.0)
    cdays = math.ceil(tdays * 7.0 / 5.0) + 3
    return max(configured, cdays)


async def load_backtest_data(from_d: date, to_d: date,
                             warmup_days: Optional[int] = None,
                             timeframe: Optional[str] = None,
                             lookback: Optional[int] = None):
    """
    Returns (universe, symbols, nifty) where:
      universe — {name: token} from client status
      symbols  — {token: SymbolSeries}
      nifty    — SymbolSeries for NIFTY 50

    warmup_days / timeframe let a backtest run override the fetch padding and
    bar interval without touching thread-local config on the event loop.
    """
    universe = await fetch_active_watchlist()           # {name: token}
    if not universe:
        return {}, {}, None

    tf = timeframe or cfg.BACKTEST_TIMEFRAME
    if warmup_days is None:
        warmup_days = cfg.BACKTEST_WARMUP_DAYS
    warmup_days = warmup_calendar_days(tf, warmup_days, lookback)
    fetch_from = (from_d - timedelta(days=warmup_days)).isoformat()
    fetch_to   = (to_d + timedelta(days=1)).isoformat()

    stocks = [{"stockname": n, "stock_symbol": t} for n, t in universe.items()]
    # Universe and NIFTY fetches are independent — run them concurrently.
    raw, nifty_raw = await asyncio.gather(
        _fetch_all(stocks, [tf], fetch_from, fetch_to),
        _fetch_all(
            [{"stockname": cfg.NIFTY50_NAME, "stock_symbol": cfg.NIFTY50_TOKEN}],
            [tf], fetch_from, fetch_to,
        ),
    )

    symbols: Dict[str, SymbolSeries] = {}
    name_by_token = {t: n for n, t in universe.items()}
    for token, frames in raw.items():
        bars = _sort_candles(frames.get(tf, []))
        if not bars:
            continue
        ss = SymbolSeries(token=token, name=name_by_token.get(token, token), series=bars)
        ss.index_days()
        symbols[token] = ss

    # NIFTY index
    nifty = None
    nframes = nifty_raw.get(cfg.NIFTY50_TOKEN, {})
    nbars   = _sort_candles(nframes.get(tf, []))
    if nbars:
        nifty = SymbolSeries(token=cfg.NIFTY50_TOKEN, name=cfg.NIFTY50_NAME, series=nbars)
        nifty.index_days()

    return universe, symbols, nifty
