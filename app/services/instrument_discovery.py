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

Each clientstatus row is `[id, name, token, symbol_code, type]` where `type`
is 'EQ' or 'INDEX' — that field is the authoritative asset_type classifier
(NIFTY 50 / BANKNIFTY come back as 'INDEX'; everything else is 'EQ'). Rows
are NOT a fixed-length-3 tuple — don't go back to unpacking `for _id, name,
token in data`, that raises ValueError against the server's real shape and
was silently falling back to the offline seed on every single call.

`symbol_code` (field 4, e.g. 'KOTAKBANK') is the identifier the historical +
WS endpoints match on — the numeric `token` (field 3) does NOT match either
endpoint. So verification fetches history BY symbol_code, and the verified
rows carry symbol_code through to the DB for market_data/historical_data to
send on every external call (token stays the internal key). For indices the
server's symbol_code equals the name ('NIFTY 50'), which is what field 4 holds.
"""

from typing import Any, Dict, List

import httpx

import app.config as cfg
from app.services.historical_data import fetch_indicator_history


async def fetch_catalog() -> Dict[str, Dict[str, str]]:
    """Live {name: {"token", "symbol_code", "asset_type"}} catalog from the
    server; falls back to the static snapshots in cfg.SEED_STOCK_CANDIDATES /
    SEED_INDEX_CANDIDATES if the live call fails. In the seed fallback the true
    symbol_code isn't known for equities, so token is used as a best-effort
    stand-in (degraded mode — historical/live matching may miss until the live
    catalog is reachable again); indices seed their name as symbol_code, which
    is what the server actually uses for them."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            resp = await client.get(cfg.CLIENTSTATUS_URL)
            resp.raise_for_status()
            data = resp.json()
        catalog: Dict[str, Dict[str, str]] = {}
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            name, token = str(row[1]), str(row[2])
            symbol_code = str(row[3]) if len(row) >= 4 and row[3] else token
            asset_type = "INDEX" if len(row) >= 5 and row[4] == "INDEX" else "EQUITY"
            catalog[name] = {"token": token, "symbol_code": symbol_code, "asset_type": asset_type}
        if catalog:
            return catalog
        print("Instrument discovery: clientstatus returned no candidates, using fallback seed")
    except Exception as e:
        print(f"Instrument discovery: clientstatus fetch failed ({e}), using fallback seed")
    fallback = {name: {"token": token, "symbol_code": token, "asset_type": "EQUITY"}
                for name, token in cfg.SEED_STOCK_CANDIDATES.items()}
    fallback.update({name: {"token": token, "symbol_code": name, "asset_type": "INDEX"}
                     for name, token in cfg.SEED_INDEX_CANDIDATES.items()})
    return fallback


async def discover_and_verify(days_back: int = 3) -> List[Dict[str, Any]]:
    """Returns verified instrument rows ready for DatabaseService.upsert_instruments()."""
    catalog = await fetch_catalog()
    candidates = [
        {"token": info["token"], "name": name, "symbol_code": info["symbol_code"]}
        for name, info in catalog.items()
    ]
    hist = await fetch_indicator_history(candidates, cfg.INTERVAL_5M, days_back=days_back)
    verified: List[Dict[str, Any]] = []
    for name, info in catalog.items():
        token = info["token"]
        if hist.get(token):
            verified.append({
                "token": token, "name": name, "display_name": name, "tradable": True,
                "asset_type": info["asset_type"], "symbol_code": info["symbol_code"],
            })
    print(f"Instrument discovery: {len(verified)}/{len(catalog)} candidates verified tradable")
    return verified
