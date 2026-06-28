from __future__ import annotations

from typing import Dict, List

import httpx

import app.config as cfg
from app.models import StockInfo
from app.state import get_state


async def load_nse_universe() -> List[StockInfo]:
    """
    Fetch Angel One's public instrument master and filter for NSE equity stocks
    with price >= ₹MIN_PRICE. Returns a list of StockInfo objects.

    The instrument master is a public JSON file — no API key required.
    ADV filtering is skipped here (no intraday volume data pre-market);
    Gemini's AI shortlist acts as the practical liquidity gate.
    """
    st = get_state()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(cfg.INSTRUMENT_MASTER_URL)
        instruments = resp.json()
    except Exception as e:
        print(f"Instrument master fetch failed: {e}")
        st.api_status = f"Instrument fetch error: {e}"
        return []

    universe: List[StockInfo] = []
    for row in instruments:
        # Filter: NSE exchange, EQ instrument type, price >= MIN_PRICE
        if row.get("exch_seg") != "NSE":
            continue
        if row.get("instrumenttype", "").upper() not in ("", "EQ"):
            continue
        symbol = row.get("symbol", "").replace("-EQ", "")
        token  = row.get("token", "")
        name   = row.get("name", symbol)
        try:
            price = float(row.get("last_price", 0) or 0)
        except (ValueError, TypeError):
            price = 0.0
        if price < cfg.MIN_PRICE:
            continue
        if symbol and token:
            universe.append(StockInfo(symbol=symbol, token=token, name=name))

    st.full_universe = [{"symbol": s.symbol, "token": s.token, "name": s.name}
                        for s in universe]
    print(f"NSE universe loaded: {len(universe)} stocks (price ≥ ₹{cfg.MIN_PRICE})")
    return universe


def build_watchlist(universe: List[StockInfo], gemini_symbols: List[str]) -> Dict[str, str]:
    """
    Intersect the Gemini shortlist with the NSE universe token map.
    Returns {symbol: token} for all matched symbols.
    """
    token_map = {s.symbol: s.token for s in universe}
    watchlist: Dict[str, str] = {}
    for sym in gemini_symbols:
        sym_clean = sym.strip().upper().replace("-EQ", "").replace(".NS", "")
        if sym_clean in token_map:
            watchlist[sym_clean] = token_map[sym_clean]
        else:
            print(f"Watchlist: {sym_clean} not found in universe — skipped")
    print(f"Active watchlist: {len(watchlist)} stocks matched from Gemini shortlist")
    return watchlist
