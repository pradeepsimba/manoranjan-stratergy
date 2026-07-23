from __future__ import annotations

"""
Mechanical order execution — deliberately NOT a trading strategy: nothing
here ever decides WHAT to trade. It only decides whether an order the user
already placed can execute right now (funds/qty checks, price-crossing
checks for a resting limit order) and keeps the funds/holdings/positions
ledger consistent when it does.

Funds ledger rules — CNC (delivery) is always cash-only/unleveraged; MIS
(intraday) is leveraged against cfg.MIS_LEVERAGE:

    CNC BUY  fill  ->  funds -= qty * fill_price   (full value, no leverage)
    CNC SELL fill  ->  funds += qty * fill_price

    MIS open/add   ->  funds -= (qty * fill_price) / MIS_LEVERAGE   (margin blocked)
    MIS close/reduce -> funds += margin_refund + realized_pnl        (margin released + P&L)

A position's `margin_used` tracks exactly how much of the user's funds is
currently blocked against it — leverage is captured at open/add time (so a
later change to cfg.MIS_LEVERAGE never retroactively changes an already-open
position's margin, same principle as cfg.STARTING_FUNDS only seeding NEW
registrations). Both a fresh MIS long and a fresh MIS short need margin
up front — shorts are no longer "free to open" as they were under the old
cash-only model.
"""

from typing import Any, Dict, Optional

import app.config as cfg
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

        price_est = limit_price if order_type == "LIMIT" else ltp

        if product == "CNC":
            instrument = await db.get_instrument(token)
            if instrument and instrument.get("asset_type") == "INDEX":
                raise OrderRejected("CNC is not available for index instruments — use MIS")
            if side == "SELL":
                holding = await db.get_holding(user["id"], token)
                if not holding or holding["qty"] < qty:
                    raise OrderRejected("Insufficient holdings to sell")
            if side == "BUY":
                if not price_est or price_est <= 0:
                    raise OrderRejected("No live price available yet")
                if float(user["funds"]) < qty * price_est:
                    raise OrderRejected("Insufficient funds")
        else:
            # MIS is leveraged — margin only applies to the qty that OPENS or
            # ADDS to exposure; a fill that closes/reduces an existing opposite
            # position needs no fresh margin for the closing portion. This is
            # an ESTIMATE (position state can shift before a resting LIMIT
            # actually fills) — execute_fill's _apply_mis_fill does the
            # authoritative, position-aware check at fill time.
            if not price_est or price_est <= 0:
                raise OrderRejected("No live price available yet")
            existing = await db.get_open_position(user["id"], token)
            if existing is None or existing["side"] == side:
                margin_needed = (qty * price_est) / cfg.MIS_LEVERAGE
            else:
                remaining = max(0, qty - existing["qty"])
                margin_needed = (remaining * price_est) / cfg.MIS_LEVERAGE
            if float(user["funds"]) < margin_needed:
                raise OrderRejected("Insufficient margin")

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

    result: Dict[str, Any] = {}
    if product == "CNC":
        # Cash-only, no leverage: the authoritative check here (placement-time
        # used an estimate; a resting LIMIT order may have had funds move via
        # other fills since then) is the full order value, same as always.
        if side == "BUY":
            user = await db.get_user(user_id)
            if float(user["funds"]) < qty * price:
                await db.reject_order(order["id"], "Insufficient funds at fill time")
                order["status"] = "REJECTED"
                order["reject_reason"] = "Insufficient funds at fill time"
                return {"type": "ORDER_UPDATE", "order": order}
            await db.update_funds(user_id, -qty * price)
            result["holding"] = await db.upsert_holding_buy(user_id, token, symbol, qty, price)
            funds_delta = -qty * price
        else:
            await db.update_funds(user_id, qty * price)
            result["holding"] = await db.reduce_holding_sell(user_id, token, qty)
            funds_delta = qty * price
    else:
        # MIS: leveraged, and open/close/flip each move funds differently
        # (margin blocked vs. margin+P&L released) — _apply_mis_fill owns the
        # authoritative check and every funds movement itself, since only it
        # knows (from the existing position, if any) which case applies.
        try:
            result["position"], funds_delta = await _apply_mis_fill(db, user_id, token, symbol, side, qty, price)
        except OrderRejected as e:
            await db.reject_order(order["id"], str(e))
            order["status"] = "REJECTED"
            order["reject_reason"] = str(e)
            return {"type": "ORDER_UPDATE", "order": order}

    filled = await db.fill_order(order["id"], price, funds_delta)
    result["type"]  = "ORDER_UPDATE"
    result["order"] = filled
    return result


async def _apply_mis_fill(db: DatabaseService, user_id: int, token: str, symbol: str,
                          side: str, qty: int, price: float) -> tuple:
    """Leveraged MIS fill — opening/adding blocks fresh margin (qty*price/leverage);
    closing/reducing releases the proportional share of margin plus realized P&L.
    Leverage is captured from cfg.MIS_LEVERAGE at the moment margin is blocked, so a
    later change to the setting never retroactively alters an already-open position.

    Returns (position, funds_delta) — funds_delta is the exact signed total this
    fill moved through the user's funds (may combine a close/reduce credit AND a
    flip-open debit in the spillover case), for the order engine to persist
    alongside the fill so Console/Journal cash-flow reporting reads the true
    figure instead of re-deriving qty*price (wrong for a leveraged fill)."""
    leverage = float(cfg.MIS_LEVERAGE)
    existing = await db.get_open_position(user_id, token)
    funds_delta = 0.0

    if existing is None or existing["side"] == side:
        margin_needed = (qty * price) / leverage
        user = await db.get_user(user_id)
        if float(user["funds"]) < margin_needed:
            raise OrderRejected("Insufficient margin at fill time")
        await db.update_funds(user_id, -margin_needed)
        funds_delta = -margin_needed
        if existing is None:
            position = await db.open_position(user_id, token, symbol, side, qty, price, margin_needed)
        else:
            position = await db.add_to_position(existing["id"], qty, price, margin_needed)
        return position, funds_delta

    # Opposite direction — this fill closes/reduces the existing position
    # (and, for a spillover, flips into a fresh position the other way).
    close_qty = min(qty, existing["qty"])
    if existing["side"] == "BUY":
        pnl = (price - float(existing["avg_price"])) * close_qty
    else:
        pnl = (float(existing["avg_price"]) - price) * close_qty

    existing_qty    = int(existing["qty"])
    existing_margin = float(existing["margin_used"])
    margin_refund   = existing_margin * (close_qty / existing_qty)
    await db.update_funds(user_id, margin_refund + pnl)
    funds_delta = margin_refund + pnl

    if close_qty >= existing_qty:
        position = await db.close_position(existing["id"], price, pnl)
    else:
        position = await db.reduce_position(existing["id"], close_qty, pnl, margin_refund)

    remaining = qty - close_qty
    if remaining > 0:
        margin_needed = (remaining * price) / leverage
        user = await db.get_user(user_id)
        if float(user["funds"]) >= margin_needed:
            await db.update_funds(user_id, -margin_needed)
            funds_delta -= margin_needed
            position = await db.open_position(user_id, token, symbol, side, remaining, price, margin_needed)
        # else: the close/reduce above already happened (its margin+P&L already
        # landed in funds) — just not enough left to also open the flip side,
        # so the excess qty beyond the close simply isn't opened.
    return position, funds_delta


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
                              ltp: float, qty: Optional[int] = None) -> Dict[str, Any]:
    """Manual/auto 'Exit' — closes a MIS position at the current live price, fully
    (qty=None or qty == open qty) or partially (qty < open qty, leaving the remainder
    open at the same side/avg_price) — reuses the same close_qty/pnl math _apply_mis_fill
    already uses for its own partial-close branch. The audit-trail order records only
    the exited qty."""
    position = await db.get_position(position_id)
    if position is None or position["user_id"] != user_id:
        raise OrderRejected("Position not found")
    if position["status"] != "OPEN":
        raise OrderRejected("Position is already closed")
    if not ltp or ltp <= 0:
        raise OrderRejected("No live price available yet")

    open_qty = int(position["qty"])
    exit_qty = open_qty if qty is None else int(qty)
    if exit_qty <= 0 or exit_qty > open_qty:
        raise OrderRejected(f"Exit quantity must be between 1 and {open_qty}")

    margin_used   = float(position["margin_used"])
    margin_refund = margin_used * (exit_qty / open_qty)
    if position["side"] == "BUY":
        pnl = (ltp - float(position["avg_price"])) * exit_qty
        closing_side = "SELL"
    else:
        pnl = (float(position["avg_price"]) - ltp) * exit_qty
        closing_side = "BUY"
    await db.update_funds(user_id, margin_refund + pnl)

    if exit_qty >= open_qty:
        position_after = await db.close_position(position_id, ltp, pnl)
    else:
        position_after = await db.reduce_position(position_id, exit_qty, pnl, margin_refund)

    order = await db.create_order(user_id, position["token"], position["symbol"],
                                  closing_side, "MARKET", "MIS", exit_qty, None)
    order = await db.fill_order(order["id"], ltp, margin_refund + pnl)
    return {"type": "POSITIONS_UPDATE", "position": position_after, "order": order}


async def exit_holding(db: DatabaseService, user_id: int, token: str,
                       ltp: float, qty: Optional[int] = None) -> Dict[str, Any]:
    """Quick 'Sell' for a CNC holding — sells up to the full held qty at the current
    live price. Mirrors execute_fill's CNC SELL path (funds credit + reduce_holding_sell)
    plus an audit-trail MARKET order. Reused by both the holdings-page quick-sell action
    and the TP/SL auto-trigger below."""
    holding = await db.get_holding(user_id, token)
    if holding is None or int(holding["qty"]) <= 0:
        raise OrderRejected("No holding to sell")
    if not ltp or ltp <= 0:
        raise OrderRejected("No live price available yet")

    open_qty = int(holding["qty"])
    sell_qty = open_qty if qty is None else int(qty)
    if sell_qty <= 0 or sell_qty > open_qty:
        raise OrderRejected(f"Sell quantity must be between 1 and {open_qty}")

    await db.update_funds(user_id, sell_qty * ltp)
    holding_after = await db.reduce_holding_sell(user_id, token, sell_qty)

    order = await db.create_order(user_id, token, holding["symbol"], "SELL", "MARKET",
                                  "CNC", sell_qty, None)
    order = await db.fill_order(order["id"], ltp, sell_qty * ltp)
    return {"type": "ORDER_UPDATE", "order": order, "holding": holding_after}


# ── Target / Stop-Loss triggers ──────────────────────────────────────────────
# Pure bookkeeping + a mechanical crossing-check — never picks a price, side, or
# qty. The user chooses both the trigger price and the action (a full exit) when
# they set it; match_triggers below only decides WHETHER that pre-chosen exit
# fires on a given tick, exactly like match_pending_limit_orders decides whether
# a resting LIMIT order fills now.

def _validate_triggers(side: str, avg_price: float, open_qty: int,
                       target_price: Optional[float], stop_loss_price: Optional[float],
                       target_qty: Optional[int], stop_loss_qty: Optional[int]) -> None:
    """side='BUY' means long (an MIS long position OR any holding — CNC never shorts);
    side='SELL' means an MIS short position. A long profits as price rises, so its
    target must sit above entry and its stop-loss below; a short is the mirror image.
    Each trigger's qty (if given) is how much to exit when THAT trigger fires, leaving
    the rest open — must be a positive qty no larger than what's currently open."""
    if target_price is not None and target_price <= 0:
        raise OrderRejected("Target price must be positive")
    if stop_loss_price is not None and stop_loss_price <= 0:
        raise OrderRejected("Stop-loss price must be positive")
    is_long = side == "BUY"
    if target_price is not None:
        if is_long and target_price <= avg_price:
            raise OrderRejected("Target price must be above the average entry price")
        if not is_long and target_price >= avg_price:
            raise OrderRejected("Target price must be below the average entry price")
    if stop_loss_price is not None:
        if is_long and stop_loss_price >= avg_price:
            raise OrderRejected("Stop-loss price must be below the average entry price")
        if not is_long and stop_loss_price <= avg_price:
            raise OrderRejected("Stop-loss price must be above the average entry price")
    if target_qty is not None and not (0 < target_qty <= open_qty):
        raise OrderRejected(f"Target quantity must be between 1 and {open_qty}")
    if stop_loss_qty is not None and not (0 < stop_loss_qty <= open_qty):
        raise OrderRejected(f"Stop-loss quantity must be between 1 and {open_qty}")


async def set_position_triggers(db: DatabaseService, position_id: int, user_id: int,
                                target_price: Optional[float],
                                stop_loss_price: Optional[float],
                                target_qty: Optional[int] = None,
                                stop_loss_qty: Optional[int] = None) -> Dict[str, Any]:
    position = await db.get_position(position_id)
    if position is None or position["user_id"] != user_id:
        raise OrderRejected("Position not found")
    if position["status"] != "OPEN":
        raise OrderRejected("Position is already closed")
    _validate_triggers(position["side"], float(position["avg_price"]), int(position["qty"]),
                       target_price, stop_loss_price, target_qty, stop_loss_qty)
    updated = await db.set_position_triggers(position_id, user_id, target_price, stop_loss_price,
                                             target_qty, stop_loss_qty)
    if updated is None:
        raise OrderRejected("Position not found")
    return updated


async def set_holding_triggers(db: DatabaseService, user_id: int, token: str,
                               target_price: Optional[float],
                               stop_loss_price: Optional[float],
                               target_qty: Optional[int] = None,
                               stop_loss_qty: Optional[int] = None) -> Dict[str, Any]:
    holding = await db.get_holding(user_id, token)
    if holding is None or int(holding["qty"]) <= 0:
        raise OrderRejected("No holding to set a target/stop-loss on")
    _validate_triggers("BUY", float(holding["avg_price"]), int(holding["qty"]),
                       target_price, stop_loss_price, target_qty, stop_loss_qty)
    updated = await db.set_holding_triggers(user_id, token, target_price, stop_loss_price,
                                            target_qty, stop_loss_qty)
    if updated is None:
        raise OrderRejected("No holding to set a target/stop-loss on")
    return updated


def _trigger_hit(side: str, ltp: float, target_price: Any, stop_loss_price: Any) -> Optional[str]:
    """Returns 'SL' | 'TARGET' | None for one position/holding row. A long (BUY position,
    or any holding — CNC never shorts) profits as price rises: target hit when
    ltp >= target, stop-loss hit when ltp <= stop_loss. A short MIS position (side SELL)
    is the mirror image. If both would fire on the same tick, stop-loss wins the tie
    (risk management over profit-taking) — in practice this never happens since
    validation always keeps target above avg_price and stop-loss below it (or the
    mirror for a short), so a single LTP can't cross both at once."""
    is_long = side == "BUY"
    sl  = float(stop_loss_price) if stop_loss_price is not None else None
    tgt = float(target_price)    if target_price    is not None else None
    if sl is not None and ((is_long and ltp <= sl) or (not is_long and ltp >= sl)):
        return "SL"
    if tgt is not None and ((is_long and ltp >= tgt) or (not is_long and ltp <= tgt)):
        return "TARGET"
    return None


async def match_triggers(db: DatabaseService, token: str, ltp: float) -> list:
    """Called for any token that just ticked — auto-exits (fully or, if that specific
    trigger has its own qty, partially) any MIS position / CNC holding on this token
    whose pre-set target_price/stop_loss_price the LTP has just crossed. Only the side
    that fired (target or stop-loss) is cleared afterward — the other side, if set,
    stays active to keep watching the remaining open qty."""
    events = []

    for position in await db.get_open_positions_with_triggers(token):
        hit = _trigger_hit(position["side"], ltp,
                           position.get("target_price"), position.get("stop_loss_price"))
        if hit is None:
            continue
        open_qty = int(position["qty"])
        configured_qty = position.get("target_qty") if hit == "TARGET" else position.get("stop_loss_qty")
        exit_qty = min(int(configured_qty), open_qty) if configured_qty is not None else None
        try:
            events.append(await square_off_position(db, position["id"], position["user_id"], ltp, exit_qty))
        except OrderRejected:
            continue
        await db.clear_position_trigger(position["id"], "target" if hit == "TARGET" else "stop_loss")

    for holding in await db.get_holdings_with_triggers(token):
        hit = _trigger_hit("BUY", ltp, holding.get("target_price"), holding.get("stop_loss_price"))
        if hit is None:
            continue
        open_qty = int(holding["qty"])
        configured_qty = holding.get("target_qty") if hit == "TARGET" else holding.get("stop_loss_qty")
        exit_qty = min(int(configured_qty), open_qty) if configured_qty is not None else None
        try:
            events.append(await exit_holding(db, holding["user_id"], holding["token"], ltp, exit_qty))
        except OrderRejected:
            continue
        await db.clear_holding_trigger(holding["user_id"], holding["token"],
                                       "target" if hit == "TARGET" else "stop_loss")

    return events


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
        margin_used = float(position["margin_used"])
        if position["side"] == "BUY":
            pnl = (price - float(position["avg_price"])) * qty
            closing_side = "SELL"
        else:
            pnl = (float(position["avg_price"]) - price) * qty
            closing_side = "BUY"
        await db.update_funds(user_id, margin_used + pnl)

        closed = await db.close_position(position["id"], price, pnl)
        order = await db.create_order(user_id, position["token"], position["symbol"],
                                      closing_side, "MARKET", "MIS", qty, None)
        order = await db.fill_order(order["id"], price, margin_used + pnl)
        events.append({"type": "POSITIONS_UPDATE", "position": closed, "order": order,
                       "user_id": user_id})
    return events


async def close_all_positions(db: DatabaseService, user_id: int,
                              price_lookup: Dict[str, float]) -> list:
    """Manual 'Close All' — squares off every OPEN MIS position for this one user at
    the last known price. Reuses square_off_position per row (same margin-refund/
    audit-order handling as a single manual Exit) rather than a third copy of the
    close logic."""
    events = []
    for position in await db.get_user_positions(user_id, "OPEN"):
        ltp = price_lookup.get(position["token"], 0.0)
        if not ltp or ltp <= 0:
            continue
        try:
            events.append(await square_off_position(db, position["id"], user_id, ltp))
        except OrderRejected:
            continue
    return events
