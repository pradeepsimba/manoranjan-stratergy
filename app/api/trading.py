from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel

from app.engine import orders as order_engine
from app.engine.orders import OrderRejected
from app.models import MarketPhase
from app.services.auth import get_current_user, user_id_from_session
from app.state import get_state
from app.ws.account_ws import account_ws_manager

router = APIRouter(prefix="/api")
# Unprefixed — /ws/account must sit at the bare path (matching /ws/market in
# market.py and what app.js's connectWS('/ws/account', ...) actually requests),
# NOT under /api like every REST endpoint on `router` above.
ws_router = APIRouter()

_db = None


def set_db(db) -> None:
    global _db
    _db = db


class PlaceOrderRequest(BaseModel):
    token:        str
    side:         Literal["BUY", "SELL"]
    order_type:   Literal["MARKET", "LIMIT"]
    product:      Literal["CNC", "MIS"]
    qty:          int
    limit_price:  Optional[float] = None


class SetTriggersRequest(BaseModel):
    target_price:    Optional[float] = None
    stop_loss_price: Optional[float] = None
    target_qty:      Optional[int] = None
    stop_loss_qty:   Optional[int] = None


class ExitPositionRequest(BaseModel):
    qty: Optional[int] = None


class SellHoldingRequest(BaseModel):
    qty: Optional[int] = None


def _round(v: Any) -> float:
    return round(float(v), 2) if v is not None else 0.0


def _serialize_order(o: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": o["id"], "token": o["token"], "symbol": o["symbol"], "side": o["side"],
        "orderType": o["order_type"], "product": o["product"], "qty": o["qty"],
        "limitPrice": _round(o["limit_price"]) if o.get("limit_price") is not None else None,
        "status": o["status"],
        "filledPrice": _round(o["filled_price"]) if o.get("filled_price") is not None else None,
        "filledAt": o["filled_at"].isoformat() if o.get("filled_at") else None,
        "rejectReason": o.get("reject_reason"),
        "createdAt": o["created_at"].isoformat() if o.get("created_at") else None,
    }


def _serialize_holding(h: Dict[str, Any], ltp: float) -> Dict[str, Any]:
    qty, avg = int(h["qty"]), float(h["avg_price"])
    pnl = (ltp - avg) * qty if ltp > 0 else 0.0
    return {
        "token": h["token"], "symbol": h["symbol"], "qty": qty,
        "avgPrice": _round(avg), "ltp": _round(ltp),
        "currentValue": _round(ltp * qty), "investedValue": _round(avg * qty),
        "pnl": _round(pnl),
        "targetPrice": _round(h["target_price"]) if h.get("target_price") is not None else None,
        "stopLossPrice": _round(h["stop_loss_price"]) if h.get("stop_loss_price") is not None else None,
        "targetQty": h.get("target_qty"),
        "stopLossQty": h.get("stop_loss_qty"),
    }


def _serialize_position(p: Dict[str, Any], ltp: float) -> Dict[str, Any]:
    qty, avg, side = int(p["qty"]), float(p["avg_price"]), p["side"]
    realized_pnl = float(p["realized_pnl"]) if p.get("realized_pnl") is not None else 0.0
    if p["status"] == "OPEN" and ltp > 0:
        pnl = (ltp - avg) * qty if side == "BUY" else (avg - ltp) * qty
    else:
        pnl = realized_pnl
    return {
        "id": p["id"], "token": p["token"], "symbol": p["symbol"], "side": side,
        "qty": qty, "avgPrice": _round(avg), "ltp": _round(ltp),
        "status": p["status"],
        "exitPrice": _round(p["exit_price"]) if p.get("exit_price") is not None else None,
        "pnl": _round(pnl),
        # Booked P&L from any exit so far (partial or full) — unlike "pnl" above
        # (live unrealized mark while OPEN), this is always populated so a partial
        # exit's profit is visible before the position is fully closed out.
        "realizedPnl": _round(realized_pnl),
        "openedAt": p["opened_at"].isoformat() if p.get("opened_at") else None,
        "closedAt": p["closed_at"].isoformat() if p.get("closed_at") else None,
        "lastExitAt": p["last_exit_at"].isoformat() if p.get("last_exit_at") else None,
        "targetPrice": _round(p["target_price"]) if p.get("target_price") is not None else None,
        "stopLossPrice": _round(p["stop_loss_price"]) if p.get("stop_loss_price") is not None else None,
        "targetQty": p.get("target_qty"),
        "stopLossQty": p.get("stop_loss_qty"),
        "marginUsed": _round(p["margin_used"]) if p.get("margin_used") is not None else 0.0,
    }


# ── Orders ────────────────────────────────────────────────────────────────────

@router.post("/orders")
async def place_order(req: PlaceOrderRequest, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    instrument = await _db.get_instrument(req.token)
    if instrument is None:
        raise HTTPException(404, "Unknown instrument")

    st = get_state()
    ltp = st.ltp.get(req.token, 0.0)
    market_open = st.phase == MarketPhase.OPEN

    event = await order_engine.place_order(
        _db, user, req.token, instrument["display_name"], req.side, req.order_type,
        req.product, req.qty, req.limit_price, market_open, ltp,
    )
    event["order"] = _serialize_order(event["order"])
    return event


@router.get("/orders")
async def list_orders(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    if _db is None:
        return []
    rows = await _db.get_user_orders(user["id"])
    return [_serialize_order(r) for r in rows]


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: int, user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    ok = await order_engine.cancel_order(_db, order_id, user["id"])
    if not ok:
        raise HTTPException(400, "Order cannot be cancelled (not found, not yours, or already final)")
    return {"cancelled": order_id}


# ── Holdings (CNC) ────────────────────────────────────────────────────────────

@router.get("/holdings")
async def list_holdings(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    if _db is None:
        return []
    st = get_state()
    rows = await _db.get_user_holdings(user["id"])
    return [_serialize_holding(r, st.ltp.get(r["token"], 0.0)) for r in rows]


@router.post("/holdings/{token}/triggers")
async def set_holding_triggers(token: str, req: SetTriggersRequest,
                               user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    try:
        holding = await order_engine.set_holding_triggers(
            _db, user["id"], token, req.target_price, req.stop_loss_price,
            req.target_qty, req.stop_loss_qty)
    except OrderRejected as e:
        raise HTTPException(400, str(e))
    st = get_state()
    return {"holding": _serialize_holding(holding, st.ltp.get(token, 0.0))}


@router.post("/holdings/{token}/sell")
async def sell_holding(token: str, req: SellHoldingRequest = SellHoldingRequest(),
                       user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    try:
        ltp = st.ltp.get(token, 0.0)
        event = await order_engine.exit_holding(_db, user["id"], token, ltp, req.qty)
    except OrderRejected as e:
        raise HTTPException(400, str(e))
    event["order"] = _serialize_order(event["order"])
    event["holding"] = _serialize_holding(event["holding"], st.ltp.get(token, 0.0))
    return event


# ── Positions (MIS) ───────────────────────────────────────────────────────────

@router.get("/positions")
async def list_positions(status: Optional[str] = None,
                         user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    if _db is None:
        return []
    st = get_state()
    rows = await _db.get_user_positions(user["id"], status)
    return [_serialize_position(r, st.ltp.get(r["token"], 0.0)) for r in rows]


@router.post("/positions/close-all")
async def close_all_positions(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    events = await order_engine.close_all_positions(_db, user["id"], dict(st.ltp))
    return {"closed": len(events)}


@router.post("/positions/{position_id}/exit")
async def exit_position(position_id: int, req: ExitPositionRequest = ExitPositionRequest(),
                        user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    try:
        position = await _db.get_position(position_id)
        if position is None or position["user_id"] != user["id"]:
            raise HTTPException(404, "Position not found")
        ltp = st.ltp.get(position["token"], 0.0)
        event = await order_engine.square_off_position(_db, position_id, user["id"], ltp, req.qty)
    except OrderRejected as e:
        raise HTTPException(400, str(e))
    event["order"] = _serialize_order(event["order"])
    event["position"] = _serialize_position(event["position"], st.ltp.get(position["token"], 0.0))
    return event


@router.post("/positions/{position_id}/triggers")
async def set_position_triggers(position_id: int, req: SetTriggersRequest,
                                user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    try:
        position = await order_engine.set_position_triggers(
            _db, position_id, user["id"], req.target_price, req.stop_loss_price,
            req.target_qty, req.stop_loss_qty)
    except OrderRejected as e:
        raise HTTPException(400, str(e))
    st = get_state()
    return {"position": _serialize_position(position, st.ltp.get(position["token"], 0.0))}


# ── Funds ─────────────────────────────────────────────────────────────────────

@router.get("/funds")
async def get_funds(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    fresh = await _db.get_user(user["id"])
    return {"funds": _round(fresh["funds"])}


# ── Console / Journal (read-only reporting over orders + positions) ─────────────
# All derived — no ledger table. Every fill's exact funds movement is captured
# by the order engine at fill time (see app/engine/orders.py's fill_order calls)
# and persisted on orders.funds_delta, since qty*filled_price is only the true
# cash movement for CNC — a leveraged MIS fill moves margin/refund+P&L instead.

def _order_cash_flow(o: Dict[str, Any]) -> float:
    if o["status"] != "COMPLETE" or o.get("filled_price") is None:
        return 0.0
    if o.get("funds_delta") is not None:
        return float(o["funds_delta"])
    # Legacy row filled before funds_delta existed — best-effort fallback (exact
    # for CNC, an overstatement for MIS since it ignores leverage).
    value = float(o["filled_price"]) * int(o["qty"])
    return value if o["side"] == "SELL" else -value


@router.get("/journal")
async def get_journal(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """Account activity ledger, newest-first, with a running funds balance.

    Balance is walked BACKWARD from the user's current funds: the newest event's
    balance is today's balance, and each older event's balance is recovered by
    undoing the newer events' cash flows. This always reconciles the top row to
    the live funds figure without needing a stored opening balance.
    """
    if _db is None:
        raise HTTPException(503, "Database not ready")
    rows = await _db.get_user_journal(user["id"])
    fresh = await _db.get_user(user["id"])
    running = float(fresh["funds"])

    entries: List[Dict[str, Any]] = []
    for o in rows:
        cash = _order_cash_flow(o)
        at = o.get("filled_at") or o.get("created_at")
        entries.append({
            "id": o["id"], "token": o["token"], "symbol": o["symbol"], "side": o["side"],
            "orderType": o["order_type"], "product": o["product"], "qty": o["qty"],
            "status": o["status"],
            "price": _round(o["filled_price"]) if o.get("filled_price") is not None
                     else (_round(o["limit_price"]) if o.get("limit_price") is not None else None),
            "cashFlow": _round(cash),
            "balance": _round(running),          # balance immediately AFTER this event
            "at": at.isoformat() if at else None,
        })
        running -= cash                          # undo to get the prior balance
    return {"entries": entries, "currentFunds": _round(fresh["funds"])}


@router.get("/console/summary")
async def console_summary(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    stats = await _db.get_trade_stats(user["id"])
    pnl = await _db.get_realized_pnl_total(user["id"])

    # Unrealized = open CNC holdings + open MIS positions, marked to last price.
    unrealized = 0.0
    for h in await _db.get_user_holdings(user["id"]):
        ltp = st.ltp.get(h["token"], 0.0)
        if ltp > 0:
            unrealized += (ltp - float(h["avg_price"])) * int(h["qty"])
    for p in await _db.get_user_positions(user["id"], "OPEN"):
        ltp = st.ltp.get(p["token"], 0.0)
        if ltp > 0:
            qty, avg = int(p["qty"]), float(p["avg_price"])
            unrealized += (ltp - avg) * qty if p["side"] == "BUY" else (avg - ltp) * qty

    closed = int(pnl["closed"]) or 0
    wins = int(pnl["wins"])
    win_rate = (wins / closed * 100.0) if closed else 0.0
    fresh = await _db.get_user(user["id"])

    return {
        "funds":         _round(fresh["funds"]),
        "totalTrades":   int(stats["fills"]),
        "turnover":      _round(stats["turnover"]),
        "buyValue":      _round(stats["buy_value"]),
        "sellValue":     _round(stats["sell_value"]),
        "pending":       int(stats["pending"]),
        "rejected":      int(stats["rejected"]),
        "cancelled":     int(stats["cancelled"]),
        "realizedPnl":   _round(pnl["realized"]),
        "unrealizedPnl": _round(unrealized),
        "closedTrades":  closed,
        "wins":          wins,
        "losses":        int(pnl["losses"]),
        "winRate":       round(win_rate, 1),
    }


@router.get("/console/tradebook")
async def console_tradebook(user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    if _db is None:
        return []
    rows = await _db.get_completed_orders(user["id"])
    out: List[Dict[str, Any]] = []
    for o in rows:
        price = _round(o["filled_price"])
        out.append({
            "id": o["id"], "token": o["token"], "symbol": o["symbol"], "side": o["side"],
            "orderType": o["order_type"], "product": o["product"], "qty": o["qty"],
            "price": price, "value": _round(price * int(o["qty"])),
            "filledAt": o["filled_at"].isoformat() if o.get("filled_at") else None,
        })
    return out


@router.get("/console/pnl")
async def console_pnl(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if _db is None:
        return {"realized": [], "holdings": []}
    st = get_state()
    realized = [
        {"symbol": r["symbol"], "token": r["token"], "trades": int(r["trades"]),
         "realizedPnl": _round(r["realized_pnl"])}
        for r in await _db.get_realized_pnl_by_symbol(user["id"])
    ]
    holdings = [_serialize_holding(h, st.ltp.get(h["token"], 0.0))
                for h in await _db.get_user_holdings(user["id"])]
    return {"realized": realized, "holdings": holdings}


# ── Authenticated account WebSocket (order fills, funds/holdings/positions) ──

@ws_router.websocket("/ws/account")
async def account_ws(websocket: WebSocket) -> None:
    user_id = user_id_from_session(websocket)
    if user_id is None:
        await websocket.close(code=4401)
        return
    await account_ws_manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        account_ws_manager.disconnect(user_id, websocket)
