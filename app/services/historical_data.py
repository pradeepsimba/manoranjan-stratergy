from __future__ import annotations

import asyncio
from datetime import date as _date
from datetime import datetime, timedelta
from typing import Dict, List, Optional
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
    out: List[Candle] = []
    for n in arr:
        if not isinstance(n, dict):
            continue
        # A vendor response with an OHLC key PRESENT but explicit JSON
        # `null` (a realistic partial-data bar) used to make float(None)
        # raise TypeError here uncaught — and since this whole function ran
        # as one list comprehension with no per-candle guard, ONE malformed
        # candle for ONE stock crashed _build_result entirely, dropping
        # every OTHER stock's perfectly good data in the same batch (this
        # app's BN/NF universes both fit in a single batch, so that meant
        # the WHOLE historical refresh, not just one stock — found in
        # review, 2026-09-23). A null price field is genuinely "no data for
        # this bar", not "zero" — drop the candle rather than fabricate a
        # 0.0 price that could pollute VWAP/IV downstream (session_vwap
        # only skips on volume<=0, not on price validity). `.get(key, 0.0)`
        # for a genuinely ABSENT key is unchanged/pre-existing behavior.
        if any(n.get(k) is None for k in ("open", "close", "high", "low")):
            print(f"Historical fetch: skipping candle with null OHLC field: {n!r:.200}")
            continue
        try:
            out.append(Candle(
                start_time=n.get("start_time", ""),
                open=float(n.get("open", 0.0)),
                close=float(n.get("close", 0.0)),
                high=float(n.get("high", 0.0)),
                low=float(n.get("low", 0.0)),
                volume=float(n.get("volume", 0.0)) if n.get("volume") is not None else 0.0,
            ))
        except (TypeError, ValueError) as e:
            print(f"Historical fetch: skipping malformed candle {n!r:.200}: {e}")
    return out


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
        resp.raise_for_status()          # treat 4xx/5xx as an error, not as candle data
        # Long ranges decode to tens of MB of candles — run the JSON decode in
        # a worker thread so the event loop (dashboard WS, tick loop) never
        # stalls behind a big fetch.
        data   = await asyncio.to_thread(resp.json)
        get_state().api_status = "API OK"
    except Exception as e:
        get_state().api_status = f"API Error: {e}"
        print(f"Historical fetch error: {e}")
        return {}

    if not isinstance(data, list):
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
        try:
            result[symbol] = {}
            for iv in intervals:
                raw = node.get(f"{iv} data", [])
                if isinstance(raw, list):
                    result[symbol][iv] = _parse_candles(raw)
        except Exception as e:
            # Defense-in-depth alongside _parse_candles' own per-candle
            # guard above (found in review, 2026-09-23) — any OTHER
            # unexpected shape for this one symbol's node must not take
            # down every other symbol's already-parsed data in the batch.
            print(f"Historical fetch: skipping malformed node for {symbol!r}: {e}")
            result.pop(symbol, None)
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
        else:
            # _fetch's own except block already logs a network/HTTP-level
            # failure, but a parse-level exception surfacing here (found in
            # review, 2026-09-23 — before _build_result got its own guard
            # above, a malformed candle could raise all the way out to this
            # gather) was silently swallowed with zero log line, contra this
            # function's own docstring claim of "silently dropped so healthy
            # batches still populate state" (true for _fetch's network
            # errors, was NOT true for this path). Log it too.
            print(f"Historical fetch: batch failed and was dropped: {r!r}")
    return merged


# ── Public API ─────────────────────────────────────────────────────────────────

async def fetch_indicator_history(
    watchlist: Dict[str, str],
    interval:  str = cfg.INTERVAL_5M,
    days_back: int = 5,
) -> Dict[str, List[Candle]]:
    today     = datetime.now(IST).date()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date   = (today + timedelta(days=1)).isoformat()
    stocks    = [{"stockname": sym, "stock_symbol": tok}
                 for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    data = await _fetch_all(stocks, [interval], from_date, to_date)
    return {tok: node.get(interval, []) for tok, node in data.items()}


async def fetch_candles_for_date(
    watchlist:   Dict[str, str],
    target_date: _date,
    interval:    str = cfg.INTERVAL_5M,
) -> Dict[str, List[Candle]]:
    """
    One specific calendar day's bars for each stock in `watchlist` (keyed by
    our internal display name -> stock_symbol, same convention as
    fetch_indicator_history). Backs the Stock Candles panel's date picker
    (2026-09-16) — a targeted single-day fetch, unlike
    fetch_indicator_history's rolling "last N days" window for the live
    buffer. Only individual stocks have real vendor history at all — the
    BankNifty/Nifty 50 index itself never does (see CLAUDE.md); callers
    needing the index's own history for a past date must read it from this
    app's self-recorded bn_index_bars/nf_index_bars table instead.
    """
    from_date = target_date.isoformat()
    to_date   = (target_date + timedelta(days=1)).isoformat()
    stocks    = [{"stockname": sym, "stock_symbol": tok}
                 for sym, tok in watchlist.items()]
    if not stocks:
        return {}
    data = await _fetch_all(stocks, [interval], from_date, to_date)
    return {tok: node.get(interval, []) for tok, node in data.items()}
