from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import app.engine.trade_engine as te
from app.models import Trade
from app.state import get_state
from app.ws.dashboard_ws import ws_manager

router = APIRouter()

# Set at startup by main.py — avoids circular imports at import time
_db = None


def set_db(db) -> None:
    global _db
    _db = db


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/api/status")
def status() -> Dict[str, Any]:
    st = get_state()
    return {
        "wsStatus":  st.ws_status,
        "apiStatus": st.api_status,
        "interval":  st.selected_interval,
        "funds":     st.available_funds,
    }


# ── Trades ────────────────────────────────────────────────────────────────────

@router.get("/api/trades")
def today_trades() -> List[Dict[str, Any]]:
    return [_trade_dict(t) for t in _db.get_today_trades()]


@router.get("/api/trades/all")
async def all_trades() -> List[Dict[str, Any]]:
    return [_trade_dict(t) for t in await _db.get_all_trades()]


@router.delete("/api/trades")
async def clear_trades() -> Dict[str, str]:
    await _db.clear_all_trades()
    return {"status": "cleared"}


# ── Manual entry / exit ───────────────────────────────────────────────────────

@router.post("/api/entry")
async def manual_entry(body: Dict[str, Any]) -> Dict[str, Any]:
    trade_type = body.get("type", "")
    price      = float(body.get("price", 0))
    if trade_type not in ("BUY", "SELL"):
        return {"error": "type must be BUY or SELL"}
    st = get_state()
    if st.active_trade is not None:
        return {"error": "trade already active"}
    await te.manual_entry(st, trade_type, price, _db)
    return {"status": "entered", "type": trade_type, "price": price}


@router.post("/api/exit")
async def manual_exit() -> Dict[str, Any]:
    st = get_state()
    if st.active_trade is None:
        return {"error": "no active trade"}
    await te.manual_exit(st, _db)
    return {"status": "exited"}


# ── Interval ──────────────────────────────────────────────────────────────────

@router.post("/api/interval")
def set_interval(body: Dict[str, Any]) -> Dict[str, Any]:
    interval = body.get("interval", "")
    if interval in ("1m", "3m", "5m", "15m"):
        get_state().selected_interval = interval
        return {"status": "ok", "interval": interval}
    return {"error": "invalid interval"}


# ── Funds ─────────────────────────────────────────────────────────────────────

@router.post("/api/funds")
def set_funds(body: Dict[str, Any]) -> Dict[str, Any]:
    funds = float(body.get("funds", 0))
    get_state().available_funds = funds
    return {"status": "ok", "funds": funds}


# ── Big Trades audit ──────────────────────────────────────────────────────────

@router.post("/api/big-trades/check")
async def big_trades_check() -> Dict[str, Any]:
    return await _db.audit_stock_qty_storage(200)


# ── Dashboard WebSocket ───────────────────────────────────────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "SET_INTERVAL":
                    interval = msg.get("interval", "")
                    if interval in ("1m", "3m", "5m", "15m"):
                        get_state().selected_interval = interval
                        print(f"Interval changed to: {interval}")
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ── Helper ────────────────────────────────────────────────────────────────────

def _trade_dict(t: Trade) -> Dict[str, Any]:
    return {
        "id":            t.id,
        "type":          t.type,
        "price":         t.price,
        "time":          t.time,
        "confidence":    t.confidence,
        "pnl":           t.pnl,
        "optionPremium": t.option_premium,
    }
