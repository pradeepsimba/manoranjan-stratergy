from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any, Dict, List

import csv
import io

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
from app.backtest.engine import run_backtest
from app.services.snapshot import apply_depth, stub_entry
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
        "phase":     st.phase.value,
        "wsStatus":  st.ws_status,
        "apiStatus": st.api_status,
        "watchlist": len(st.active_watchlist),
        "dailyPnl":  round(st.daily_pnl, 2),
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
    return await _db.get_today_positions() if _db else []


@router.get("/api/positions/all")
async def get_all_positions() -> List[Dict[str, Any]]:
    return await _db.get_all_positions() if _db else []


# ── Scan results ──────────────────────────────────────────────────────────────

@router.get("/api/scans")
def get_scans() -> Dict[str, Any]:
    st = get_state()
    return {
        "lastBarTime": st.last_5m_bar_time,
        "results": [
            {"symbol": sym, **res}
            for sym, res in st.scan_snapshot()[-40:]
        ],
    }


# ── Live prices ───────────────────────────────────────────────────────────────

@router.get("/api/prices")
def get_prices() -> Dict[str, float]:
    st = get_state()
    return {**st.ltp, "NIFTY50": st.nifty_ltp}


# ── Backtest ──────────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    from_date:    date
    to_date:      date
    slippage_bps: float = cfg.SLIPPAGE_BPS
    capital:      float = cfg.ACCOUNT_BALANCE


@router.post("/api/backtest")
async def start_backtest(req: BacktestRequest) -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    if req.from_date > req.to_date:
        raise HTTPException(400, "from_date must be on or before to_date")
    if req.capital <= 0:
        raise HTTPException(400, "capital must be greater than 0")

    run_id = uuid.uuid4().hex[:12]
    await _db.create_backtest_run(
        run_id, req.from_date, req.to_date,
        {"slippage_bps": req.slippage_bps, "capital": req.capital},
    )
    asyncio.create_task(
        run_backtest(_db, run_id, req.from_date, req.to_date, req.slippage_bps, req.capital)
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
        "Symbol", "Entry Price", "Entry Time", "Exit Price", "Exit Time",
        "Qty", "Outcome", "Stop Loss", "Target",
        "Gross P&L", "Costs", "Net P&L", "R Multiple",
        "RSI", "ADX", "MACD", "Support", "Pattern",
    ])
    for t in trades:
        w.writerow([
            t.get("symbol"),      t.get("entry_price"),    t.get("entry_time"),
            t.get("exit_price"),  t.get("exit_time"),      t.get("quantity"),
            t.get("outcome"),     t.get("stop_loss"),      t.get("target"),
            t.get("gross_pnl"),   t.get("costs"),          t.get("net_pnl"),
            t.get("r_multiple"),  t.get("rsi"),            t.get("adx"),
            t.get("macd"),        t.get("support_level"),  t.get("candle_pattern"),
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


# ── Live indicators ───────────────────────────────────────────────────────────

@router.get("/api/indicators")
async def get_live_indicators() -> List[Dict[str, Any]]:
    """
    Get the pre-computed indicator snapshot for all stocks.
    Avoids expensive sequential recalculation, returning the latest background scan state instantly.
    """
    st = get_state()
    wl = st.full_watchlist if st.full_watchlist else st.active_watchlist
    if not wl:
        return []

    out: List[Dict[str, Any]] = []
    snapshot = dict(st.indicator_snapshot)

    for sym, tok in list(wl.items()):
        live_ltp = round(st.ltp.get(sym, 0.0), 2)
        if sym in snapshot:
            entry = dict(snapshot[sym])
            entry["symbol"] = sym
            entry["ltp"] = live_ltp if live_ltp > 0 else entry.get("ltp", 0.0)
        else:
            # Stub if background scanner hasn't processed this symbol yet —
            # fall back to the last 5m candle for a price / bar time.
            c5 = list(st.candles_5m.get(tok, []))
            entry = stub_entry()
            entry["symbol"]   = sym
            entry["ltp"]      = round(live_ltp if live_ltp > 0 else (c5[-1].close if c5 else 0.0), 2)
            entry["bar_time"] = c5[-1].start_time[11:16] if c5 else "—"
        apply_depth(entry, st.depth.get(sym, {}))
        out.append(entry)

    return sorted(out, key=lambda x: x["symbol"])


# ── Dashboard WebSocket ───────────────────────────────────────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)
