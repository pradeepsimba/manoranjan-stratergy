from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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
    # Session bounds are DYNAMIC settings — read at call time, not hardcoded
    # 09:15/15:30, or a user-edited MARKET_OPEN/SESSION_END would desync this
    # fetch window from the rest of the app (e.g. the "today's open" bar the
    # daily-green gate uses would be the wrong bar after a recovery load).
    today     = datetime.now(IST).date()
    from_date = datetime(today.year, today.month, today.day,
                         cfg.MARKET_OPEN_HOUR, cfg.MARKET_OPEN_MIN,
                         tzinfo=IST).strftime("%Y-%m-%dT%H:%M:%S")
    to_date   = datetime(today.year, today.month, today.day,
                         cfg.SESSION_END_HOUR, cfg.SESSION_END_MIN,
                         tzinfo=IST).strftime("%Y-%m-%dT%H:%M:%S")
    return from_date, to_date


def _day_str(d) -> str:
    """
    Full-datetime string for a bare `date` — the historical API's from_date/
    to_date query params are bound server-side as LocalDateTime and reject a
    plain "YYYY-MM-DD" string, so every call site must format through here
    rather than `date.isoformat()`.
    """
    return d.strftime("%Y-%m-%dT00:00:00")


# ── Core fetch (one batch) ─────────────────────────────────────────────────────

async def _fetch(
    stocks:    List[Dict],
    intervals: List[str],
    from_date: str,
    to_date:   str,
    errors:    Optional[list] = None,
) -> Dict[str, Dict[str, List[Candle]]]:
    """
    `errors` — when _fetch_all runs several batches concurrently it passes a
    shared list: each failing batch appends its error and the AGGREGATE status
    is written once by _fetch_all. Without it (single direct call) this
    function owns api_status itself. Otherwise a later-completing healthy
    batch would overwrite a concurrent batch's error with "API OK" and a day
    running on a partial universe would show a green API status.
    """
    url     = cfg.API_URL_TEMPLATE.format(cfg.API_HOST, from_date, to_date)
    payload = [
        {"stockname": s["stockname"], "stock_symbol": s["stock_symbol"],
         "intervals": intervals}
        for s in stocks
    ]
    try:
        client = await _http()
        resp   = await client.post(url, json=payload)
        resp.raise_for_status()          # treat 4xx/5xx as an error, not as candle data
        # Long ranges decode to tens of MB of candles — run the JSON decode in
        # a worker thread so the event loop (dashboard WS, tick loop) never
        # stalls behind a big fetch.
        data   = await asyncio.to_thread(resp.json)
        if errors is None:
            get_state().api_status = "API OK"
    except Exception as e:
        if errors is not None:
            errors.append(str(e))
        else:
            get_state().api_status = f"API Error: {e}"
        print(f"Historical fetch error: {e}")
        return {}

    if not isinstance(data, list):
        if errors is not None:
            errors.append("unexpected response shape")
        else:
            get_state().api_status = "API Error: unexpected response shape"
        print(f"Historical fetch: expected a list, got {type(data).__name__}: {data!r:.200}")
        return {}

    # Candle construction is pure CPU over the decoded payload — also off-loop.
    return await asyncio.to_thread(_build_result, data, intervals)


def _build_result(data: list, intervals: List[str]) -> Dict[str, Dict[str, List[Candle]]]:
    result: Dict[str, Dict[str, List[Candle]]] = {}
    for node in data:
        if not isinstance(node, dict):
            continue
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
    errors:    Optional[list] = None,
) -> Dict[str, Dict[str, List[Candle]]]:
    """
    Split stocks into HIST_BATCH_SIZE chunks and POST all chunks concurrently.
    For 500 stocks with batch=100: 5 parallel requests instead of one giant one.
    Failed batches are silently dropped so healthy batches still populate state.

    `errors` — when the CALLER runs several _fetch_all's concurrently (e.g. the
    scheduler's session load: 5m + today's 1h + NIFTY in one gather), each call
    writing its own aggregate status would race: a healthy fetch finishing last
    overwrites a concurrent fetch's "API partial". Passing a shared list makes
    the caller the single status writer.
    """
    own_status = errors is None
    if errors is None:
        errors = []
    merged: Dict[str, Dict[str, List[Candle]]] = {}
    if stocks:
        if len(stocks) <= cfg.HIST_BATCH_SIZE:
            merged = await _fetch(stocks, intervals, from_date, to_date, errors)
        else:
            batches = [stocks[i : i + cfg.HIST_BATCH_SIZE]
                       for i in range(0, len(stocks), cfg.HIST_BATCH_SIZE)]
            responses = await asyncio.gather(
                *[_fetch(b, intervals, from_date, to_date, errors) for b in batches],
                return_exceptions=True,
            )
            for r in responses:
                if isinstance(r, Exception):
                    errors.append(str(r))
                elif isinstance(r, dict):
                    merged.update(r)
    # One aggregate status for the whole round — a partial universe must not
    # read as healthy just because the last batch to finish succeeded.
    if own_status:
        get_state().api_status = (
            f"API partial: {len(errors)} batch(es) failed ({errors[0]})"
            if errors else "API OK"
        )
    return merged


# ── Public API ─────────────────────────────────────────────────────────────────

async def fetch_today_candles(
    watchlist: Dict[str, str],
    intervals: Optional[List[str]] = None,
    errors:    Optional[list] = None,
) -> Dict[str, Dict[str, List[Candle]]]:
    if intervals is None:
        intervals = [cfg.INTERVAL_5M, cfg.INTERVAL_1H]
    stocks = [{"stockname": sym, "stock_symbol": tok}
              for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    from_date, to_date = _today_range()
    return await _fetch_all(stocks, intervals, from_date, to_date, errors)


async def fetch_nifty_candles(errors: Optional[list] = None) -> List[Candle]:
    """Today's NIFTY 5m session bars (daily gate + session VWAP)."""
    stocks           = [{"stockname": cfg.NIFTY50_NAME, "stock_symbol": cfg.NIFTY50_TOKEN}]
    today_d, today_t = _today_range()
    today_data       = await _fetch(stocks, [cfg.INTERVAL_5M], today_d, today_t, errors)
    return today_data.get(cfg.NIFTY50_TOKEN, {}).get(cfg.INTERVAL_5M, [])


async def fetch_indicator_history(
    watchlist: Dict[str, str],
    interval:  str = cfg.INTERVAL_5M,
    days_back: int = 5,
    errors:    Optional[list] = None,
) -> Dict[str, List[Candle]]:
    today     = datetime.now(IST).date()
    from_date = _day_str(today - timedelta(days=days_back))
    to_date   = _day_str(today + timedelta(days=1))
    stocks    = [{"stockname": sym, "stock_symbol": tok}
                 for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    data = await _fetch_all(stocks, [interval], from_date, to_date, errors)
    return {tok: node.get(interval, []) for tok, node in data.items()}
