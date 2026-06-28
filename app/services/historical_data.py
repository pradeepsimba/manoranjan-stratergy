from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

import app.config as cfg
from app.models import Candle
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# Persistent client — connection reuse across all historical fetches.
# Self-signed cert on remote server; verify=False intentional.
_HTTP: Optional[httpx.AsyncClient] = None


async def _http() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None or _HTTP.is_closed:
        _HTTP = httpx.AsyncClient(
            verify=False,
            timeout=60.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _HTTP


# ── Candle parsing ─────────────────────────────────────────────────────────────

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


# ── Date helpers ───────────────────────────────────────────────────────────────

def _today_range() -> Tuple[str, str]:
    today     = date.today()
    from_date = datetime(today.year, today.month, today.day, 9, 15,
                         tzinfo=IST).strftime("%Y-%m-%dT%H:%M:%S")
    to_date   = datetime(today.year, today.month, today.day, 15, 30,
                         tzinfo=IST).strftime("%Y-%m-%dT%H:%M:%S")
    return from_date, to_date


def _week_range() -> Tuple[str, str]:
    today     = date.today()
    from_date = (today - timedelta(days=7)).isoformat()
    to_date   = (today + timedelta(days=1)).isoformat()
    return from_date, to_date


# ── Core fetch (one batch) ─────────────────────────────────────────────────────

async def _fetch(
    stocks:    List[Dict],
    intervals: List[str],
    from_date: str,
    to_date:   str,
) -> Dict[str, Dict[str, List[Candle]]]:
    url     = cfg.API_URL_TEMPLATE.format(cfg.API_HOST, from_date, to_date)
    payload = [
        {"stockname": s["stockname"], "stock_symbol": s["stock_symbol"],
         "intervals": intervals}
        for s in stocks
    ]
    try:
        client = await _http()
        resp   = await client.post(url, json=payload)
        data   = resp.json()
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
            raw = node.get(f"{iv} data", [])
            if isinstance(raw, list):
                result[symbol][iv] = _parse_candles(raw)
    return result


# ── Batched parallel fetch ─────────────────────────────────────────────────────

async def _fetch_all(
    stocks:    List[Dict],
    intervals: List[str],
    from_date: str,
    to_date:   str,
) -> Dict[str, Dict[str, List[Candle]]]:
    """
    Split stocks into HIST_BATCH_SIZE chunks and POST all chunks concurrently.
    For 500 stocks with batch=100: 5 parallel requests instead of one giant one.
    Failed batches are silently dropped so healthy batches still populate state.
    """
    if not stocks:
        return {}
    if len(stocks) <= cfg.HIST_BATCH_SIZE:
        return await _fetch(stocks, intervals, from_date, to_date)

    batches   = [stocks[i : i + cfg.HIST_BATCH_SIZE]
                 for i in range(0, len(stocks), cfg.HIST_BATCH_SIZE)]
    responses = await asyncio.gather(
        *[_fetch(b, intervals, from_date, to_date) for b in batches],
        return_exceptions=True,
    )
    merged: Dict[str, Dict[str, List[Candle]]] = {}
    for r in responses:
        if isinstance(r, dict):
            merged.update(r)
    return merged


# ── Public API ─────────────────────────────────────────────────────────────────

async def fetch_today_candles(
    watchlist: Dict[str, str],
    intervals: Optional[List[str]] = None,
) -> Dict[str, Dict[str, List[Candle]]]:
    if intervals is None:
        intervals = [cfg.INTERVAL_5M, cfg.INTERVAL_1H, cfg.INTERVAL_1D]
    stocks = [{"stockname": sym, "stock_symbol": tok}
              for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    from_date, to_date = _today_range()
    return await _fetch_all(stocks, intervals, from_date, to_date)


async def fetch_nifty_candles() -> Tuple[List[Candle], List[Candle]]:
    stocks       = [{"stockname": cfg.NIFTY50_NAME, "stock_symbol": cfg.NIFTY50_TOKEN}]
    from_d, to_d = _week_range()
    data         = await _fetch(stocks, [cfg.INTERVAL_5M, cfg.INTERVAL_1D], from_d, to_d)
    node         = data.get(cfg.NIFTY50_TOKEN, {})
    today_d, today_t = _today_range()
    today_data   = await _fetch(stocks, [cfg.INTERVAL_5M], today_d, today_t)
    today_5m     = today_data.get(cfg.NIFTY50_TOKEN, {}).get(cfg.INTERVAL_5M, [])
    return node.get(cfg.INTERVAL_1D, []), today_5m


async def fetch_indicator_history(
    watchlist: Dict[str, str],
    interval:  str = cfg.INTERVAL_5M,
    days_back: int = 5,
) -> Dict[str, List[Candle]]:
    today     = date.today()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date   = (today + timedelta(days=1)).isoformat()
    stocks    = [{"stockname": sym, "stock_symbol": tok}
                 for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    data = await _fetch_all(stocks, [interval], from_date, to_date)
    return {sym: node.get(interval, []) for sym, node in data.items()}
