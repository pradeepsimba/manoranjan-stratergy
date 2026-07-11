from __future__ import annotations

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
        [[rank, stockname, token], ...]
    e.g. [[1, "WIPRO", "3787"], [2, "BAJAJ AUTO", "16669"], ...]

    Returns {stockname: token} for every entry, excluding known index names.
    Index instruments (NIFTY 50 etc.) are handled separately by the trend gate.
    """
    st = get_state()
    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            resp = await client.get(cfg.CLIENT_STATUS_URL)
        data = resp.json()
        st.api_status = "API OK"
    except Exception as e:
        st.api_status = f"Client status error: {e}"
        print(f"Client status fetch failed: {e}")
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

    rows = sorted((e for e in data if isinstance(e, (list, tuple)) and len(e) >= 3),
                  key=_rank)

    watchlist: Dict[str, str] = {}
    for entry in rows:
        # Normalise non-breaking spaces: Gemini echoes names back with regular
        # spaces, so a raw \xa0 in the name would never map to a token again.
        stockname = str(entry[1]).replace("\xa0", " ").strip()
        token     = str(entry[2]).strip()
        if not stockname or not token:
            continue
        if stockname.upper() in _INDEX_NAMES:
            continue
        if stockname in watchlist and watchlist[stockname] != token:
            print(f"Client status: duplicate name '{stockname}' "
                  f"({watchlist[stockname]} vs {token}) — keeping the first")
            continue
        watchlist[stockname] = token

    print(f"Active watchlist from client status: {len(watchlist)} stocks")
    return watchlist
