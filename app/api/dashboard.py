from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
import app.services.settings as settings
from app.backtest.engine import run_backtest
from app.backtest.signal_study import run_bn_leader_consensus_study
from app.state import get_state
from app.ws.dashboard_ws import ws_manager

router = APIRouter()

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


# ── Live prices ───────────────────────────────────────────────────────────────

@router.get("/api/prices")
def get_prices() -> Dict[str, float]:
    st = get_state()
    prices = {cfg.BN_INDEX_NAME: st.bn_index_ltp, cfg.NF_INDEX_NAME: st.nf_index_ltp}
    prices.update(st.ltp)
    return prices


# ── Leader-consensus signal study (NOT the options P&L backtest below) ────────
# Synchronous — cheap enough (a handful of days x ~75 bars x 6 stocks, no
# option pricing) to run inline rather than via the run_id/poll pattern the
# real backtest needs.

@router.post("/api/signal-study/bn")
async def bn_signal_study() -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    return await run_bn_leader_consensus_study(_db)


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
        attr_overrides = settings.expand_changes(req.overrides or {}, bt_only=True)
        # Only the scan window matters to a replay — validating against the
        # live-only times (market open/session-end) would falsely reject.
        settings.validate_time_order(attr_overrides, points=("SCAN_START", "CUTOFF"))
        settings.validate_bn_indicator_periods(attr_overrides)
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

@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)
