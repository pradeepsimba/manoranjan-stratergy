from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

import app.config as cfg
from app.models import Candle
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# Self-signed cert on remote server — verify=False intentional
_CLIENT_KWARGS = dict(verify=False, timeout=30.0)


# ── Candle parsing ────────────────────────────────────────────────────────────

def _parse_candles(arr: list) -> List[Candle]:
    return [
        Candle(
            start_time=n.get("start_time", ""),
            open=float(n.get("open",   0.0)),
            close=float(n.get("close", 0.0)),
            high=float(n.get("high",   0.0)),
            low=float(n.get("low",     0.0)),
            volume=float(n.get("volume", 0.0)),
        )
        for n in arr
        if isinstance(n, dict)
    ]


# ── Date helpers ──────────────────────────────────────────────────────────────

def _today_range() -> Tuple[str, str]:
    """Return (from_date, to_date) covering today's full session in IST."""
    today     = date.today()
    from_date = datetime(today.year, today.month, today.day, 9, 15,
                         tzinfo=IST).strftime("%Y-%m-%dT%H:%M:%S")
    to_date   = datetime(today.year, today.month, today.day, 15, 30,
                         tzinfo=IST).strftime("%Y-%m-%dT%H:%M:%S")
    return from_date, to_date


def _week_range() -> Tuple[str, str]:
    """Return a 7-day window ending today (for daily + hourly candles)."""
    today     = date.today()
    from_date = (today - timedelta(days=7)).isoformat()
    to_date   = (today + timedelta(days=1)).isoformat()
    return from_date, to_date


# ── Core fetch ────────────────────────────────────────────────────────────────

async def _fetch(
    stocks: List[Dict],
    intervals: List[str],
    from_date: str,
    to_date: str,
) -> Dict[str, Dict[str, List[Candle]]]:
    """
    POST to the custom API. Returns {symbol: {interval: [Candle]}}.
    stocks format: [{"stockname": str, "stock_symbol": str}]
    """
    url     = cfg.API_URL_TEMPLATE.format(cfg.API_HOST, from_date, to_date)
    payload = [
        {"stockname": s["stockname"], "stock_symbol": s["stock_symbol"],
         "intervals": intervals}
        for s in stocks
    ]
    try:
        async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        get_state().api_status = "API OK"
    except Exception as e:
        get_state().api_status = f"API Error: {e}"
        print(f"Historical fetch error: {e}")
        return {}

    result: Dict[str, Dict[str, List[Candle]]] = {}
    for node in data:
        symbol = node.get("stock_symbol", "")
        if not symbol:
            continue
        result[symbol] = {}
        for iv in intervals:
            key = f"{iv} data"
            raw = node.get(key, [])
            if isinstance(raw, list):
                result[symbol][iv] = _parse_candles(raw)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_today_candles(
    watchlist: Dict[str, str],          # {symbol: token}
    intervals: Optional[List[str]] = None,
) -> Dict[str, Dict[str, List[Candle]]]:
    """
    Load today's session candles for all watchlist stocks at the given intervals.
    Default: 5m, 1h, and 1d.
    """
    if intervals is None:
        intervals = [cfg.INTERVAL_5M, cfg.INTERVAL_1H, cfg.INTERVAL_1D]

    stocks = [
        {"stockname": sym, "stock_symbol": tok}
        for sym, tok in watchlist.items()
    ]
    if not stocks:
        return {}

    from_date, to_date = _today_range()
    return await _fetch(stocks, intervals, from_date, to_date)


async def fetch_nifty_candles() -> Tuple[List[Candle], List[Candle]]:
    """
    Fetch NIFTY 50 candles at 1d and 5m for the trend gate and VWAP.
    Returns (candles_1d, candles_5m).
    """
    stocks = [{"stockname": cfg.NIFTY50_NAME, "stock_symbol": cfg.NIFTY50_TOKEN}]
    from_d, to_d = _week_range()
    data = await _fetch(stocks, [cfg.INTERVAL_5M, cfg.INTERVAL_1D], from_d, to_d)
    node = data.get(cfg.NIFTY50_TOKEN, {})
    # For today's 5m VWAP — re-fetch with today's range
    today_d, today_t = _today_range()
    today_data = await _fetch(stocks, [cfg.INTERVAL_5M], today_d, today_t)
    today_5m = today_data.get(cfg.NIFTY50_TOKEN, {}).get(cfg.INTERVAL_5M, [])
    return node.get(cfg.INTERVAL_1D, []), today_5m


async def fetch_indicator_history(
    watchlist: Dict[str, str],
    interval: str = cfg.INTERVAL_5M,
    days_back: int = 5,
) -> Dict[str, List[Candle]]:
    """
    Fetch a multi-day history for indicator computation (ADX needs 29+ bars).
    Returns {symbol: [Candle]}.
    """
    today     = date.today()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date   = (today + timedelta(days=1)).isoformat()
    stocks    = [{"stockname": sym, "stock_symbol": tok}
                 for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    data = await _fetch(stocks, [interval], from_date, to_date)
    return {sym: node.get(interval, []) for sym, node in data.items()}
