from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.state import get_state
from app.ws.dashboard_ws import ws_manager

router = APIRouter()

# Injected at startup by main.py
_db      = None
_angel   = None
_sched   = None


def set_services(db, angel, sched) -> None:
    global _db, _angel, _sched
    _db    = db
    _angel = angel
    _sched = sched


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/api/status")
def status() -> Dict[str, Any]:
    st = get_state()
    return {
        "phase":     st.phase.value,
        "wsStatus":  st.ws_status,
        "apiStatus": st.api_status,
        "watchlist": len(st.active_watchlist),
        "dailyPnl":  st.daily_pnl,
    }


# ── Watchlist ─────────────────────────────────────────────────────────────────

@router.get("/api/watchlist")
def watchlist() -> List[Dict[str, str]]:
    st = get_state()
    return [{"symbol": sym, "token": tok}
            for sym, tok in st.active_watchlist.items()]


# ── Positions ─────────────────────────────────────────────────────────────────

@router.get("/api/positions")
async def get_positions() -> List[Dict[str, Any]]:
    if _db:
        return await _db.get_today_positions()
    return []


@router.get("/api/positions/all")
async def get_all_positions() -> List[Dict[str, Any]]:
    if _db:
        return await _db.get_all_positions()
    return []


# ── Scan results ──────────────────────────────────────────────────────────────

@router.get("/api/scans")
def get_scans() -> Dict[str, Any]:
    st = get_state()
    return {
        "lastBarTime": st.last_5m_bar_time,
        "results": [
            {"symbol": sym, **res}
            for sym, res in list(st.last_scan_results.items())[-40:]
        ],
    }


# ── Live prices ───────────────────────────────────────────────────────────────

@router.get("/api/prices")
def get_prices() -> Dict[str, float]:
    return {**get_state().ltp, "NIFTY50": get_state().nifty_ltp}


# ── Dashboard WebSocket ───────────────────────────────────────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
