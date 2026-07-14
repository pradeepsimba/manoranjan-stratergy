from __future__ import annotations

"""
Mechanical order execution — deliberately NOT a trading strategy: nothing
here ever decides WHAT to trade. It only decides whether an order the user
already placed can execute right now (funds/qty checks, price-crossing
checks for a resting limit order) and keeps the funds/holdings/positions
ledger consistent when it does.

Funds ledger rule (uniform across CNC and MIS, long and short — this is the
one rule that keeps the whole ledger self-consistent without a separate
margin/reservation system):
    BUY  fill  ->  funds -= qty * fill_price
    SELL fill  ->  funds += qty * fill_price

For a plain long round-trip this nets to the obvious (price_exit - price_entry)
* qty. For a short (MIS SELL to open, no existing long), the SELL-credits/
BUY-debits rule nets to (price_entry - price_exit) * qty when covered — the
correct sign for a short — with no separate short-selling code path needed.

No leverage/margin modeling anywhere (matches this repo's long-standing
"cash-only, no margin" convention) — a BUY always requires the full
qty*price in funds up front, and a fresh MIS short is allowed without a
funds prerequisite (selling generates cash, it doesn't require it).
"""

from typing import Any, Dict, Optional

from app.services.database import DatabaseService


class OrderRejected(Exception):
    pass


async def place_order(
    db: DatabaseService,
    user: Dict[str, Any],
    token: str,
    symbol: str,
    side: str,          # "BUY" | "SELL"
    order_type: str,    # "MARKET" | "LIMIT"
    product: str,       # "CNC" | "MIS"
    qty: int,
    limit_price: Optional[float],
    market_open: bool,
    ltp: Optional[float],
) -> Dict[str, Any]:
    """
    Always writes an order row first (even a rejection is a real, visible
    order-book entry — matches how a real broker shows failed attempts),
    then validates and either fills immediately (MARKET) or leaves it
    resting (LIMIT).
    """
    order = await db.create_order(user["id"], token, symbol, side, order_type, product,
                                  qty, limit_price)
    try:
        if not market_open:
            raise OrderRejected("Market is closed")
        if qty <= 0:
            raise OrderRejected("Quantity must be positive")
        if order_type == "LIMIT" and (limit_price is None or limit_price <= 0):
            raise OrderRejected("Limit price is required for a LIMIT order")

        if product == "CNC" and side == "SELL":
            holding = await db.get_holding(user["id"], token)
            if not holding or holding["qty"] < qty:
                raise OrderRejected("Insufficient holdings to sell")

        if side == "BUY":
            price_est = limit_price if order_type == "LIMIT" else ltp
            if not price_est or price_est <= 0:
                raise OrderRejected("No live price available yet")
            if float(user["funds"]) < qty * price_est:
                raise OrderRejected("Insufficient funds")

        if order_type == "MARKET":
            if not ltp or ltp <= 0:
                raise OrderRejected("No live price available yet")
            return await execute_fill(db, order, ltp)

        return {"type": "ORDER_UPDATE", "order": order}

    except OrderRejected as e:
        await db.reject_order(order["id"], str(e))
        order["status"] = "REJECTED"
        order["reject_reason"] = str(e)
        return {"type": "ORDER_UPDATE", "order": order}


async def execute_fill(db: DatabaseService, order: Dict[str, Any], price: float) -> Dict[str, Any]:
    user_id = order["user_id"]
    token   = order["token"]
    symbol  = order["symbol"]
    side    = order["side"]
    product = order["product"]
    qty     = int(order["qty"])

    # Authoritative funds check — placement-time check used an estimate;
    # a resting LIMIT order may have had funds move (other fills) since then.
    if side == "BUY":
        user = await db.get_user(user_id)
        if float(user["funds"]) < qty * price:
            await db.reject_order(order["id"], "Insufficient funds at fill time")
            order["status"] = "REJECTED"
            order["reject_reason"] = "Insufficient funds at fill time"
            return {"type": "ORDER_UPDATE", "order": order}
        await db.update_funds(user_id, -qty * price)
    else:
        await db.update_funds(user_id, qty * price)

    result: Dict[str, Any] = {}
    if product == "CNC":
        if side == "BUY":
            result["holding"] = await db.upsert_holding_buy(user_id, token, symbol, qty, price)
        else:
            result["holding"] = await db.reduce_holding_sell(user_id, token, qty)
    else:
        result["position"] = await _apply_mis_fill(db, user_id, token, symbol, side, qty, price)

    filled = await db.fill_order(order["id"], price)
    result["type"]  = "ORDER_UPDATE"
    result["order"] = filled
    return result


async def _apply_mis_fill(db: DatabaseService, user_id: int, token: str, symbol: str,
                          side: str, qty: int, price: float) -> Dict[str, Any]:
    existing = await db.get_open_position(user_id, token)

    if existing is None:
        return await db.open_position(user_id, token, symbol, side, qty, price)

    if existing["side"] == side:
        # Same direction — just growing the position, no realized P&L yet.
        return await db.add_to_position(existing["id"], qty, price)

    # Opposite direction — this fill closes/reduces the existing position
    # (and, for a spillover, flips into a fresh position the other way).
    close_qty = min(qty, existing["qty"])
    if existing["side"] == "BUY":
        pnl = (price - float(existing["avg_price"])) * close_qty
    else:
        pnl = (float(existing["avg_price"]) - price) * close_qty

    if close_qty >= existing["qty"]:
        position = await db.close_position(existing["id"], price, pnl)
    else:
        position = await db.reduce_position(existing["id"], close_qty, pnl)

    remaining = qty - close_qty
    if remaining > 0:
        position = await db.open_position(user_id, token, symbol, side, remaining, price)
    return position


async def match_pending_limit_orders(db: DatabaseService, token: str, ltp: float) -> list:
    """Called for any token that just ticked — fills qualifying resting LIMIT orders."""
    events = []
    for order in await db.get_pending_orders_for_token(token):
        if order["order_type"] != "LIMIT" or order["limit_price"] is None:
            continue
        limit_price = float(order["limit_price"])
        crosses = (
            (order["side"] == "BUY" and ltp <= limit_price) or
            (order["side"] == "SELL" and ltp >= limit_price)
        )
        if crosses:
            events.append(await execute_fill(db, order, limit_price))
    return events


async def cancel_order(db: DatabaseService, order_id: int, user_id: int) -> bool:
    return await db.cancel_order(order_id, user_id)


async def square_off_position(db: DatabaseService, position_id: int, user_id: int,
                              ltp: float) -> Dict[str, Any]:
    """Manual 'Exit' — closes one MIS position at the current live price."""
    position = await db.get_position(position_id)
    if position is None or position["user_id"] != user_id:
        raise OrderRejected("Position not found")
    if position["status"] != "OPEN":
        raise OrderRejected("Position is already closed")
    if not ltp or ltp <= 0:
        raise OrderRejected("No live price available yet")

    qty = int(position["qty"])
    if position["side"] == "BUY":
        pnl = (ltp - float(position["avg_price"])) * qty
        await db.update_funds(user_id, qty * ltp)
        closing_side = "SELL"
    else:
        pnl = (float(position["avg_price"]) - ltp) * qty
        await db.update_funds(user_id, -qty * ltp)
        closing_side = "BUY"

    closed = await db.close_position(position_id, ltp, pnl)
    order = await db.create_order(user_id, position["token"], position["symbol"],
                                  closing_side, "MARKET", "MIS", qty, None)
    order = await db.fill_order(order["id"], ltp)
    return {"type": "POSITIONS_UPDATE", "position": closed, "order": order}


async def eod_square_off_all_mis(db: DatabaseService, price_lookup: Dict[str, float]) -> list:
    """Auto square-off every user's open MIS position at end of day."""
    events = []
    for position in await db.get_all_open_positions():
        price = price_lookup.get(position["token"])
        if not price or price <= 0:
            print(f"EOD square-off: no live price for token {position['token']}, skipping "
                  f"position {position['id']} (will retry next EOD pass)")
            continue

        qty = int(position["qty"])
        user_id = position["user_id"]
        if position["side"] == "BUY":
            pnl = (price - float(position["avg_price"])) * qty
            await db.update_funds(user_id, qty * price)
            closing_side = "SELL"
        else:
            pnl = (float(position["avg_price"]) - price) * qty
            await db.update_funds(user_id, -qty * price)
            closing_side = "BUY"

        closed = await db.close_position(position["id"], price, pnl)
        order = await db.create_order(user_id, position["token"], position["symbol"],
                                      closing_side, "MARKET", "MIS", qty, None)
        order = await db.fill_order(order["id"], price)
        events.append({"type": "POSITIONS_UPDATE", "position": closed, "order": order,
                       "user_id": user_id})
    return events
