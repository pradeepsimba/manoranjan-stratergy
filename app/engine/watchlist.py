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

    watchlist: Dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        stockname = str(entry[1]).strip()
        token     = str(entry[2]).strip()
        if not stockname or not token:
            continue
        if stockname.upper() in _INDEX_NAMES or stockname in _INDEX_NAMES:
            continue
        watchlist[stockname] = token

    print(f"Active watchlist from client status: {len(watchlist)} stocks")
    return watchlist
