from __future__ import annotations

"""
Builds the tradable instrument universe by cross-checking the market-data
server's own instrument catalog (`/api/clientstatus/`) against its
historical-data endpoint, so nothing is added to `instruments` on a guess.

Two known server quirks drive this design (see CLAUDE.md):
  1. The historical-data endpoint matches by `stockname` TEXT, not token — an
     unverified/guessed name can silently return zero candles for an
     otherwise-correct token. Sourcing names straight from `/api/clientstatus/`
     (rather than guessing) sidesteps this entirely.
  2. Being listed in the catalog doesn't guarantee historical OHLC exists
     (e.g. a very recent listing) — so every candidate is still verified by
     actually fetching a few days of candles before being marked tradable.
"""

from typing import Any, Dict, List

import httpx

import app.config as cfg
from app.services.historical_data import fetch_indicator_history

_INDEX_NAMES = {"NIFTY 50", "BANKNIFTY"}   # indices, not individually tradable equities


async def fetch_catalog() -> Dict[str, str]:
    """Live {name: token} catalog from the server; falls back to the static
    snapshot in cfg.SEED_STOCK_CANDIDATES if the live call fails."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            resp = await client.get(cfg.CLIENTSTATUS_URL)
            resp.raise_for_status()
            data = resp.json()
        catalog = {
            str(name): str(token)
            for _id, name, token in data
            if name not in _INDEX_NAMES
        }
        if catalog:
            return catalog
        print("Instrument discovery: clientstatus returned no candidates, using fallback seed")
    except Exception as e:
        print(f"Instrument discovery: clientstatus fetch failed ({e}), using fallback seed")
    return dict(cfg.SEED_STOCK_CANDIDATES)


async def discover_and_verify(days_back: int = 3) -> List[Dict[str, Any]]:
    """Returns verified instrument rows ready for DatabaseService.upsert_instruments()."""
    catalog = await fetch_catalog()
    hist = await fetch_indicator_history(catalog, cfg.INTERVAL_5M, days_back=days_back)
    verified: List[Dict[str, Any]] = []
    for name, token in catalog.items():
        if hist.get(token):
            verified.append({
                "token": token, "name": name, "display_name": name, "tradable": True,
            })
    print(f"Instrument discovery: {len(verified)}/{len(catalog)} candidates verified tradable")
    return verified
