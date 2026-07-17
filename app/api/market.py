from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel

import app.config as cfg
import app.services.settings as settings
from app.services.auth import user_id_from_session
from app.state import get_state
from app.ws.market_ws import market_ws_manager

router = APIRouter()

IST = ZoneInfo("Asia/Kolkata")

_db = None


def set_db(db) -> None:
    global _db
    _db = db


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/api/status")
def status() -> Dict[str, Any]:
    st = get_state()
    return {
        "clock":     datetime.now(IST).strftime("%H:%M:%S"),
        "phase":     st.phase.value,
        "wsStatus":  st.ws_status,
        "apiStatus": st.api_status,
    }


# ── Settings (runtime tunables) ───────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    changes: Dict[str, Any]


class SettingsReset(BaseModel):
    keys: List[str] | None = None   # None = reset everything


@router.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    return settings.describe()


@router.put("/api/settings")
async def update_settings(req: SettingsUpdate, request: Request) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    if not req.changes:
        raise HTTPException(400, "No changes supplied")
    try:
        result = await settings.apply_and_persist(_db, req.changes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # STARTING_FUNDS only seeds new registrations by default — but changing it
    # from Settings clearly means "I want my own balance at this number now",
    # not just future signups, so apply it to the account that changed it
    # (only that one — this must never silently touch every other user's funds).
    if "STARTING_FUNDS" in req.changes:
        user_id = user_id_from_session(request)
        if user_id is not None:
            new_funds = await _db.set_funds(user_id, cfg.STARTING_FUNDS)
            result["yourFunds"] = new_funds
    return result


@router.post("/api/settings/reset")
async def reset_settings(req: SettingsReset) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    try:
        return await settings.reset(_db, req.keys)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Instruments / live prices ─────────────────────────────────────────────────

def _day_change(token: str, ltp: float, st) -> Dict[str, Any]:
    with st.candle_lock(token):
        candles = list(st.candles_5m.get(token, []))
    day_open = candles[0].open if candles else 0.0
    last_close = candles[-1].close if candles else 0.0
    price = ltp if ltp > 0 else last_close
    change = price - day_open if day_open > 0 else 0.0
    change_pct = (change / day_open * 100) if day_open > 0 else 0.0
    return {"ltp": price, "change": round(change, 2), "changePct": round(change_pct, 2)}


def _depth_top(token: str, st) -> Dict[str, Any]:
    """Best bid/ask (+qty) for a compact watchlist-row display — the full
    5-level book (see /api/instruments/{token}/depth) is overkill for a list
    of hundreds of rows."""
    d = st.depth.get(token)
    bid = d["bids"][0] if d and d.get("bids") else None
    ask = d["asks"][0] if d and d.get("asks") else None
    return {
        "bestBid":    bid["price"] if bid else None,
        "bestBidQty": bid["qty"]   if bid else None,
        "bestAsk":    ask["price"] if ask else None,
        "bestAskQty": ask["qty"]   if ask else None,
        "ltpQty":     d.get("ltpQty") if d else None,
    }


@router.get("/api/instruments")
async def list_instruments() -> List[Dict[str, Any]]:
    if _db is None:
        return []
    st = get_state()
    rows = await _db.get_tradable_instruments()
    out = []
    for r in rows:
        token = r["token"]
        info = _day_change(token, st.ltp.get(token, 0.0), st)
        out.append({
            "token":       token,
            "name":        r["display_name"],
            **info,
            **_depth_top(token, st),
        })
    return out


@router.get("/api/instruments/{token}/depth")
def get_depth(token: str) -> Dict[str, Any]:
    """Full Level-1 depth snapshot (5 bid/ask levels + buy/sell qty + OI +
    circuit limits) for the one instrument currently selected in the terminal
    — see MarketDataService._parse_snap for how this is derived from the feed."""
    st = get_state()
    d = st.depth.get(token)
    return d or {"ltpQty": None, "buyQty": None, "sellQty": None, "oi": None, "oiChangePct": None,
                 "upperCircuit": None, "lowerCircuit": None, "bids": [], "asks": []}


@router.get("/api/instruments/{token}/candles")
def get_candles(token: str, limit: int = 100) -> List[Dict[str, Any]]:
    st = get_state()
    with st.candle_lock(token):
        candles = list(st.candles_5m.get(token, []))[-limit:]
    return [
        {"startTime": c.start_time, "open": c.open, "close": c.close,
         "high": c.high, "low": c.low, "volume": c.volume}
        for c in candles
    ]


# ── Public market WebSocket (shared prices/candles/status only) ─────────────

@router.websocket("/ws/market")
async def market_ws(websocket: WebSocket) -> None:
    await market_ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        market_ws_manager.disconnect(websocket)
