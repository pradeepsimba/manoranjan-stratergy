from __future__ import annotations

import asyncio
from typing import Dict

import httpx

import app.config as cfg
from app.state import get_state

# Instruments that are indices — skip them from the trading watchlist.
_INDEX_NAMES = frozenset({
    "NIFTY 50", "NIFTY50", "NIFTY BANK", "BANKNIFTY",
    "FINNIFTY", "MIDCPNIFTY", "NIFTY MIDCAP", "SENSEX",
})


async def fetch_active_watchlist() -> Dict[str, str]:
    """
    GET https://35.234.219.141:8000/api/clientstatus/

    Response format:
        [[id, name, exchange_token, trading_symbol, instrumental_type], ...]
    e.g. [[3456, "TATA MOTORS", "3456", "TATAMOTORS", "EQ"], ...]

    Returns {name: trading_symbol}. The historical-data/WS API keys every
    request and response row by `stock_symbol` — it must be the real NSE
    trading_symbol (e.g. "TATAMOTORS"), NOT `exchange_token`: mangling `name`
    into a guessed symbol breaks on real symbols like "M&M" or "BAJAJ-AUTO",
    and this app used to send exchange_token there entirely, which the market
    data server doesn't resolve to any instrument. Everything downstream
    (candles_5m/dirty_ticks keying, candle_lock, token_to_name, WS filters)
    treats this value as an opaque string key, so trading_symbol drops in
    with no other code change needed. Excludes known index names — index
    instruments (NIFTY 50 etc.) are handled separately by the trend gate.
    """
    st = get_state()
    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            resp = await client.get(cfg.CLIENT_STATUS_URL)
        # Full-universe responses can be a multi-thousand-row array; decoding it inline would
        # block the event loop (WS tick receive, the 100ms tick loop, the dashboard push loop) for
        # the parse duration. Matches historical_data.py's fetch_candles, which offloads for the
        # same reason.
        data = await asyncio.to_thread(resp.json)
        st.api_status = "API OK"
    except Exception as e:
        # str(e) is empty for some httpx/anyio connect-level errors (e.g. a bare
        # ConnectTimeout/ConnectError with no message attached to the OSError it
        # wraps) — prefix the exception type so the dashboard never shows a
        # blank reason after "Client status error: ".
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        st.api_status = f"Client status error: {detail}"
        print(f"Client status fetch failed: {detail}")
        return {}

    # A JSON error object ({"detail": ...}) would iterate its KEYS here, fail
    # every length-3 check, and read as a healthy-but-empty universe with
    # api_status already set to "API OK" above — surface it as an error.
    if not isinstance(data, list):
        st.api_status = "Client status error: unexpected response shape"
        print(f"Client status: expected a list, got {type(data).__name__}")
        return {}

    # Sort by rank (entry[0]) — the Gemini-failure fallback trades "the first
    # GEMINI_MAX_STOCKS of the full list", which is only meaningful if the
    # dict is built in rank order; don't trust raw server ordering.
    def _rank(entry):
        try:
            return float(entry[0])
        except (TypeError, ValueError, IndexError):
            return float("inf")

    rows = sorted((e for e in data if isinstance(e, (list, tuple)) and len(e) >= 4),
                  key=_rank)

    watchlist: Dict[str, str] = {}
    for entry in rows:
        # Normalise non-breaking spaces: Gemini echoes names back with regular
        # spaces, so a raw \xa0 in the name would never map to a symbol again.
        stockname = str(entry[1]).replace("\xa0", " ").strip()
        symbol    = str(entry[3]).strip()
        instrumental_type = str(entry[4]).strip().upper() if len(entry) >= 5 else ""

        if stockname.upper() in _INDEX_NAMES:
            # Capture NIFTY 50's real trading_symbol here rather than trusting
            # the static cfg.NIFTY50_TOKEN default (that constant is actually
            # AngelOne's exchange token, "99926000" — a different field from
            # trading_symbol; see state.nifty_token()'s docstring). MUST run
            # before the instrumental_type=="EQ" filter below — an index's own
            # instrumental_type is never "EQ", so that filter would otherwise
            # skip this row before this capture ever ran. Every index name in
            # _INDEX_NAMES is still dropped from the trading watchlist either
            # way; this is purely additive bookkeeping.
            if stockname.upper() == cfg.NIFTY50_NAME.upper() and symbol:
                st.nifty_symbol = symbol
            continue
        # Defensive extra filter alongside _INDEX_NAMES: skip anything the
        # server itself flags as non-equity when instrumental_type is present
        # (5th element) — don't rely solely on matching the display name.
        if instrumental_type and instrumental_type != "EQ":
            continue
        if not stockname or not symbol:
            continue
        if stockname in watchlist and watchlist[stockname] != symbol:
            print(f"Client status: duplicate name '{stockname}' "
                  f"({watchlist[stockname]} vs {symbol}) — keeping the first")
            continue
        watchlist[stockname] = symbol

    print(f"Active watchlist from client status: {len(watchlist)} stocks")
    return watchlist
