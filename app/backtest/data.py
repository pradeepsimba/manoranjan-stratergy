from __future__ import annotations

"""
Backtest data layer.

Pulls the trading universe from the live client-status endpoint, fetches 5-minute
history for every symbol (plus NIFTY) over the requested range — with extra
warmup days so indicators are valid from the first bar — and organizes each
symbol's bars into a per-day index for the replay engine.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List

import app.config as cfg
from app.engine.watchlist import fetch_active_watchlist
from app.models import Candle
from app.services.historical_data import _fetch_all


@dataclass
class SymbolSeries:
    token:  str
    name:   str
    series: List[Candle]                       # full chronological 5m bars
    by_day: Dict[str, List[int]] = field(default_factory=dict)   # "YYYY-MM-DD" -> [global idx...]
    at:     Dict[str, Dict[str, int]] = field(default_factory=dict)  # date -> {"HH:MM": global idx}

    def index_days(self) -> None:
        for i, c in enumerate(self.series):
            d  = c.start_time[:10]
            tm = c.start_time[11:16]
            self.by_day.setdefault(d, []).append(i)
            self.at.setdefault(d, {})[tm] = i


def _sort_candles(candles: List[Candle]) -> List[Candle]:
    return sorted(candles, key=lambda c: c.start_time)


async def load_backtest_data(from_d: date, to_d: date):
    """
    Returns (universe, symbols, nifty) where:
      universe — {name: token} from client status
      symbols  — {token: SymbolSeries}
      nifty    — SymbolSeries for NIFTY 50
    """
    universe = await fetch_active_watchlist()           # {name: token}
    if not universe:
        return {}, {}, None

    fetch_from = (from_d - timedelta(days=cfg.BACKTEST_WARMUP_DAYS)).isoformat()
    fetch_to   = (to_d + timedelta(days=1)).isoformat()

    stocks = [{"stockname": n, "stock_symbol": t} for n, t in universe.items()]
    raw    = await _fetch_all(stocks, [cfg.INTERVAL_5M], fetch_from, fetch_to)

    symbols: Dict[str, SymbolSeries] = {}
    name_by_token = {t: n for n, t in universe.items()}
    for token, frames in raw.items():
        bars = _sort_candles(frames.get(cfg.INTERVAL_5M, []))
        if not bars:
            continue
        ss = SymbolSeries(token=token, name=name_by_token.get(token, token), series=bars)
        ss.index_days()
        symbols[token] = ss

    # NIFTY index
    nifty_raw = await _fetch_all(
        [{"stockname": cfg.NIFTY50_NAME, "stock_symbol": cfg.NIFTY50_TOKEN}],
        [cfg.INTERVAL_5M], fetch_from, fetch_to,
    )
    nifty = None
    nframes = nifty_raw.get(cfg.NIFTY50_TOKEN, {})
    nbars   = _sort_candles(nframes.get(cfg.INTERVAL_5M, []))
    if nbars:
        nifty = SymbolSeries(token=cfg.NIFTY50_TOKEN, name=cfg.NIFTY50_NAME, series=nbars)
        nifty.index_days()

    return universe, symbols, nifty
