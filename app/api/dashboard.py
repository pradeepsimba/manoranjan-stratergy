from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
import app.services.settings as settings
from app.auth import require_login
from app.backtest.engine import run_backtest
from app.backtest.signal_study import run_bn_leader_consensus_study
from app.services import bn_trade, nf_trade
from app.services.historical_data import fetch_candles_for_date
from app.services.settings import BN_FUNDS_KEY
from app.state import get_state
from app.ws.dashboard_ws import ws_manager

IST = ZoneInfo("Asia/Kolkata")

# Whole-app login (see app/auth.py) applied at the router level - every route
# below requires it. The one deliberate exception, /ws/dashboard, lives on
# ws_router at the bottom of this file instead, which carries no such
# dependency.
router = APIRouter(dependencies=[Depends(require_login)])
ws_router = APIRouter()

_db    = None
_sched = None


def set_services(db, sched) -> None:
    global _db, _sched
    _db    = db
    _sched = sched


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/api/status")
def status() -> Dict[str, Any]:
    st = get_state()
    return {
        "phase":       st.phase.value,
        "wsStatus":    st.ws_status,
        "apiStatus":   st.api_status,
        "hasActiveTrade": st.active_trade is not None,
        "closedToday": len(st.closed_trades),
        "hasActiveTradeNf": st.active_trade_nf is not None,
        "closedTodayNf":    len(st.closed_trades_nf),
        "dailyPnl":    round(st.daily_pnl, 2),   # shared account — BN + NF combined
        "funds":       round(st.funds, 2),        # shared account — BN + NF combined
    }


# ── Settings (runtime tunables) ───────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    changes: Dict[str, Any]


class SettingsReset(BaseModel):
    keys: Optional[List[str]] = None   # None = reset everything


@router.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    return settings.describe()


@router.put("/api/settings")
async def update_settings(req: SettingsUpdate) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    if not req.changes:
        raise HTTPException(400, "No changes supplied")
    try:
        return await settings.apply_and_persist(_db, req.changes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/settings/reset")
async def reset_settings(req: SettingsReset) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    try:
        return await settings.reset(_db, req.keys)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Positions (the Bank Nifty options trade log) ──────────────────────────────

@router.get("/api/positions")
async def get_positions() -> List[Dict[str, Any]]:
    return await _db.get_today_positions() if _db else []


@router.get("/api/positions/all")
async def get_all_positions() -> List[Dict[str, Any]]:
    return await _db.get_all_positions() if _db else []


# ── Manual order (dashboard's Kite-style BankNifty order form) ────────────────
# Places/closes the SAME single st.active_trade the automated engine uses —
# a manual trading-desk override, not a second/parallel trade concept. See
# app/services/bn_trade.py's place_manual_order/force_close for the shared
# mechanics (open_trade_from_signal/finalize_exit — this is not a fork of
# the live+backtest strategy core, just a different way of constructing the
# BNSignal that feeds it).

class ManualOrderRequest(BaseModel):
    direction: str   # "BUY" (-> long ATM CE) | "SELL" (-> long ATM PE)


@router.post("/api/manual-order")
async def manual_order(req: ManualOrderRequest) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    try:
        trade = bn_trade.place_manual_order(req.direction.upper(), datetime.now(IST))
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        await _db.save_position(trade, instrument="BANKNIFTY")
    except Exception as e:
        raise HTTPException(500, f"Order placed but DB save failed: {e}")
    return {
        "orderId": trade.order_id, "direction": trade.direction,
        "strike": trade.strike, "optionType": trade.option_type,
        "entryIndexPrice": trade.entry_index_price, "entryPremium": trade.entry_premium,
    }


@router.post("/api/manual-exit")
async def manual_exit() -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    if st.active_trade is None:
        raise HTTPException(400, "No active trade to exit")
    if st.bn_index_ltp <= 0:
        raise HTTPException(400, "No live BankNifty price yet")

    with st._bn_index_lock:
        bn_candles = list(st.bn_index_candles_5m)
    closes = (np.fromiter((c.close for c in bn_candles), np.float64, len(bn_candles))
              if bn_candles else np.zeros(0, dtype=np.float64))
    lookback = closes[-cfg.BN_IV_LOOKBACK_BARS:] if closes.size > cfg.BN_IV_LOOKBACK_BARS else closes

    closed = bn_trade.force_close(datetime.now(IST), st.bn_index_ltp, lookback, label="MANUAL EXIT")
    if closed is None:
        raise HTTPException(400, "No active trade to exit")
    try:
        await _db.update_position_exit(
            order_id=closed.order_id, exit_price=closed.exit_index_price,
            exit_time=closed.exit_time, pnl=closed.pnl, exit_premium=closed.exit_premium,
        )
        await _db.set_app_settings({BN_FUNDS_KEY: st.funds})
    except Exception as e:
        raise HTTPException(500, f"Exit applied but DB persist failed: {e}")
    return {"orderId": closed.order_id, "exitPremium": closed.exit_premium, "pnl": closed.pnl}


# ── Manual order (Nifty 50) — mirror of the BankNifty manual order above,
# same single-active-trade-slot semantics but on st.active_trade_nf. Shares
# the BN_FUNDS_KEY paper account (funds/daily_pnl are one shared account
# across both instruments — see app/state.py). ────────────────────────────

@router.post("/api/manual-order-nf")
async def manual_order_nf(req: ManualOrderRequest) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    try:
        trade = nf_trade.place_manual_order(req.direction.upper(), datetime.now(IST))
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        await _db.save_position(trade, instrument="NIFTY50")
    except Exception as e:
        raise HTTPException(500, f"Order placed but DB save failed: {e}")
    return {
        "orderId": trade.order_id, "direction": trade.direction,
        "strike": trade.strike, "optionType": trade.option_type,
        "entryIndexPrice": trade.entry_index_price, "entryPremium": trade.entry_premium,
    }


@router.post("/api/manual-exit-nf")
async def manual_exit_nf() -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    if st.active_trade_nf is None:
        raise HTTPException(400, "No active trade to exit")
    if st.nf_index_ltp <= 0:
        raise HTTPException(400, "No live Nifty 50 price yet")

    with st._nf_index_lock:
        nf_candles = list(st.nf_index_candles_5m)
    closes = (np.fromiter((c.close for c in nf_candles), np.float64, len(nf_candles))
              if nf_candles else np.zeros(0, dtype=np.float64))
    lookback = closes[-cfg.NF_IV_LOOKBACK_BARS:] if closes.size > cfg.NF_IV_LOOKBACK_BARS else closes

    closed = nf_trade.force_close(datetime.now(IST), st.nf_index_ltp, lookback, label="MANUAL EXIT")
    if closed is None:
        raise HTTPException(400, "No active trade to exit")
    try:
        await _db.update_position_exit(
            order_id=closed.order_id, exit_price=closed.exit_index_price,
            exit_time=closed.exit_time, pnl=closed.pnl, exit_premium=closed.exit_premium,
        )
        await _db.set_app_settings({BN_FUNDS_KEY: st.funds})
    except Exception as e:
        raise HTTPException(500, f"Exit applied but DB persist failed: {e}")
    return {"orderId": closed.order_id, "exitPremium": closed.exit_premium, "pnl": closed.pnl}


# ── Live prices ───────────────────────────────────────────────────────────────

@router.get("/api/prices")
def get_prices() -> Dict[str, float]:
    st = get_state()
    prices = {cfg.BN_INDEX_NAME: st.bn_index_ltp, cfg.NF_INDEX_NAME: st.nf_index_ltp}
    prices.update(st.ltp)
    return prices


# ── Stock Candles panel — date-picker historical fetch ────────────────────────
# On-demand, single-calendar-day snapshot (2026-09-16) — separate from
# STATE_UPDATE's live stockCandles/stockCandlesNf, which is always the
# rolling recent buffer (today, or a few days back at most, per
# scheduler._STOCK_TABLE_BARS). Shape-compatible with that same field
# (startTime/open/close/high/low/volume/lastQty/buyQty/sellQty/surged) so the
# frontend can render it through the exact same renderStockCandles it
# already has, just fed a different payload.

def _historical_candle_json(c) -> Dict[str, Any]:
    return {
        "startTime": c.start_time, "open": c.open, "close": c.close,
        "high": c.high, "low": c.low, "volume": c.volume,
        "lastQty": c.last_qty, "buyQty": c.buy_qty, "sellQty": c.sell_qty,
        "surged": False,   # historical/vendor-REST bars carry no live surge signal
    }


@router.get("/api/stock-candles/{instrument}")
async def stock_candles_for_date(instrument: str, for_date: date) -> Dict[str, Any]:
    """
    instrument: "bn" | "nf". for_date: the ONE calendar day to fetch (query
    param, e.g. ?for_date=2026-09-10). Individual stocks come straight from
    the vendor's REST history (it fully archives those); the index itself
    never has vendor history at all (see CLAUDE.md) — its bars come from
    this app's OWN self-recorded bn_index_bars/nf_index_bars table instead,
    so a date before this app was ever running that day returns no index row.
    """
    if instrument not in ("bn", "nf"):
        raise HTTPException(400, "instrument must be 'bn' or 'nf'")
    if _db is None:
        raise HTTPException(503, "Database not ready")

    from_iso = f"{for_date.isoformat()}T00:00:00"
    to_iso   = f"{for_date.isoformat()}T23:59:59"

    if instrument == "bn":
        all_stocks = cfg.BN_ALL_STOCKS
        index_name = cfg.BN_INDEX_NAME
        index_bars = await _db.get_bn_index_bars(from_iso, to_iso)
    else:
        all_stocks = cfg.NF_ALL_STOCKS
        index_name = cfg.NF_INDEX_NAME
        index_bars = await _db.get_nf_index_bars(from_iso, to_iso)

    stock_hist    = await fetch_candles_for_date(all_stocks, for_date)
    token_to_name = {tok: name for name, tok in all_stocks.items()}

    stock_candles: Dict[str, Any] = {}
    if index_bars:
        stock_candles[index_name] = [_historical_candle_json(c) for c in index_bars]
    for tok, candles in stock_hist.items():
        stock_candles[token_to_name.get(tok, tok)] = [_historical_candle_json(c) for c in candles]

    return {"date": for_date.isoformat(), "stockCandles": stock_candles}


# ── Leader-consensus signal study (NOT the options P&L backtest below) ────────
# Synchronous — cheap enough (a handful of days x ~75 bars x 6 stocks, no
# option pricing) to run inline rather than via the run_id/poll pattern the
# real backtest needs.

@router.post("/api/signal-study/bn")
async def bn_signal_study(
    mode: str = "threshold", days: Optional[int] = None, required: Optional[int] = None,
) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    if mode not in ("threshold", "direction"):
        raise HTTPException(400, "mode must be 'threshold' or 'direction'")
    if days is not None and days <= 0:
        raise HTTPException(400, "days must be > 0")
    if required is not None and not (1 <= required <= 6):
        raise HTTPException(400, "required must be between 1 and 6")
    return await run_bn_leader_consensus_study(_db, mode=mode, days_back=days, required=required)


# ── Backtest ──────────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    from_date:    date
    to_date:      date
    # None = use the CURRENT dynamic settings (resolved at request time — a
    # pydantic default would freeze the import-time value).
    slippage_bps: Optional[float]          = None
    # Per-run strategy overrides, {spec_key: value} — validated against the
    # settings registry and scoped to this run's worker threads only.
    overrides:    Optional[Dict[str, Any]] = None


@router.post("/api/backtest")
async def start_backtest(req: BacktestRequest) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    if req.from_date > req.to_date:
        raise HTTPException(400, "from_date must be on or before to_date")

    try:
        # No SPEC key is bt=True any more (2026-09-09 settings cleanup —
        # every strategy/session tunable is now a static cfg attribute), so
        # this only ever succeeds with an empty overrides dict; any key at
        # all raises "unknown setting" here, which is the correct behavior.
        attr_overrides = settings.expand_changes(req.overrides or {}, bt_only=True)
    except ValueError as e:
        raise HTTPException(400, f"overrides: {e}")

    slippage = req.slippage_bps if req.slippage_bps is not None else \
        attr_overrides.get("SLIPPAGE_BPS", cfg.SLIPPAGE_BPS)
    if slippage < 0:
        raise HTTPException(400, "slippage_bps must be ≥ 0")

    run_id = uuid.uuid4().hex[:12]
    await _db.create_backtest_run(
        run_id, req.from_date, req.to_date,
        {"slippage_bps": slippage, "overrides": attr_overrides},
    )
    asyncio.create_task(
        run_backtest(_db, run_id, req.from_date, req.to_date,
                     slippage, overrides=attr_overrides)
    )
    return {"run_id": run_id, "status": "running"}


@router.get("/api/backtest/{run_id}")
async def get_backtest(run_id: str) -> Dict[str, Any]:
    run = await _db.get_backtest_run(run_id) if _db else None
    if run is None:
        raise HTTPException(404, "Unknown run_id")
    return run


@router.delete("/api/backtest/{run_id}")
async def delete_backtest(run_id: str) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    await _db.delete_backtest_run(run_id)
    return {"deleted": run_id}


@router.get("/api/backtest/{run_id}/trades")
async def get_backtest_trades(run_id: str) -> List[Dict[str, Any]]:
    return await _db.get_backtest_trades(run_id) if _db else []


@router.get("/api/backtest/{run_id}/export.csv")
async def export_backtest_csv(run_id: str) -> Response:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    run = await _db.get_backtest_run(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run_id")
    trades = await _db.get_backtest_trades(run_id)

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow([
        "Direction", "Option", "Strike", "Expiry",
        "Entry Index Price", "Entry Time", "Exit Index Price", "Exit Time",
        "Lot Size", "Outcome", "Stop Loss", "Target",
        "Entry Premium", "Exit Premium",
        "Gross P&L", "Costs", "Net P&L", "R Multiple",
    ])
    for t in trades:
        w.writerow([
            t.get("direction"),   t.get("option_type"), t.get("strike"), t.get("expiry"),
            t.get("entry_price"), t.get("entry_time"),  t.get("exit_price"), t.get("exit_time"),
            t.get("quantity"),    t.get("outcome"),     t.get("stop_loss"), t.get("target"),
            t.get("entry_premium"), t.get("exit_premium"),
            t.get("gross_pnl"),   t.get("costs"),       t.get("net_pnl"), t.get("r_multiple"),
        ])

    from_d = str(run.get("from_date", "")).replace("-", "")
    to_d   = str(run.get("to_date",   "")).replace("-", "")
    fname  = f"backtest_{from_d}_{to_d}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/api/backtests")
async def list_backtests() -> List[Dict[str, Any]]:
    return await _db.list_backtest_runs() if _db else []


# ── Dashboard WebSocket ───────────────────────────────────────────────────────
# On ws_router, not router - deliberately NOT behind login (explicit user
# decision: the alert feature's live price feed stays open).

@ws_router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)
