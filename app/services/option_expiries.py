"""
Real option expiries, read from backend_server_algo (which holds the broker's own instrument
master) instead of being worked out locally from weekday/holiday rules.

`GET /api/option-expiries/?underlying=X` returns each live contract's broker-supplied expiry date
and the broker's own symbol prefix. get_next_expiry/the option-symbol builders in
bn_pricing.py/nf_pricing.py consult this cache first; their old weekday+holiday arithmetic only
remains as an emergency fallback for when the backend has never been reachable (logged loudly).

Live-only: the cache holds today-and-later expiries, so lookups for a `now` before the day the
cache was fetched (a backtest replaying a past day) return None and use the legacy path.
"""
import asyncio
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

import app.config as cfg

IST = ZoneInfo("Asia/Kolkata")
REFRESH_INTERVAL_S = 1800

# underlying -> (fetched_on, [(expiry_date, symbol_prefix), ...] nearest first)
_CACHE: Dict[str, Tuple[date, List[Tuple[date, str]]]] = {}
_warned: set = set()


def _entry(underlying: str):
    return _CACHE.get(underlying.upper())


def next_expiry(underlying: str, now: datetime) -> Optional[datetime]:
    """15:30 IST on the first cached broker expiry not yet past `now`; None if the cache has
    nothing usable for this `now` (never fetched, or `now` is before the cache's fetch day)."""
    entry = _entry(underlying)
    if entry is None:
        return None
    fetched_on, expiries = entry
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    if now.date() < fetched_on:
        return None
    for d, _prefix in expiries:
        close = datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)
        if now <= close:
            return close
    return None


def symbol_prefix(underlying: str, expiry: datetime) -> Optional[str]:
    """The broker's own symbol prefix for this expiry date, or None if unknown."""
    entry = _entry(underlying)
    if entry is None:
        return None
    for d, prefix in entry[1]:
        if d == expiry.date():
            return prefix
    return None


def warn_fallback(underlying: str) -> None:
    """Once per underlying: the broker expiry list is unavailable, so the legacy local
    calculation is being used."""
    if underlying not in _warned:
        _warned.add(underlying)
        print(f"CRITICAL: no broker option-expiry data for {underlying} (backend unreachable or "
              f"instrument master empty) - falling back to local weekday/holiday calculation")


async def refresh(client: httpx.AsyncClient) -> None:
    for underlying in {cfg.BN_OPTION_UNDERLYING, cfg.NF_OPTION_UNDERLYING}:
        url = cfg.OPTION_EXPIRIES_URL_TEMPLATE.format(cfg.API_HOST, underlying)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            expiries = [(date.fromisoformat(r["expiry"]), r["symbol_prefix"]) for r in data]
        except Exception as e:
            print(f"Option-expiry refresh failed for {underlying}: {e}")
            continue
        if expiries:
            _CACHE[underlying.upper()] = (datetime.now(IST).date(), expiries)
            _warned.discard(underlying)


async def refresh_loop() -> None:
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        while True:
            await refresh(client)
            await asyncio.sleep(REFRESH_INTERVAL_S)
