from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any, Dict, List

import csv
import io

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
from app.backtest.engine import run_backtest
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
    run    = await _db.get_backtest_run(run_id)
    trades = await _db.get_backtest_trades(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run_id")

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
    Compute RSI / MACD / ADX / support / VWAP for every stock in the active
    watchlist.  Runs sequentially in a background thread — TA-Lib's C layer
    releases the GIL so the event loop stays unblocked.
    """
    from app.engine.indicator_engine import compute_indicators   # avoid circular at module level

    st = get_state()
    if not st.active_watchlist:
        return []

    def _compute_all() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for sym, tok in list(st.active_watchlist.items()):
            with st.candle_lock(tok):
                c5 = list(st.candles_5m.get(tok, []))

            ltp   = st.ltp.get(sym, c5[-1].close if c5 else 0.0)
            bar_t = c5[-1].start_time[11:16] if c5 else "—"

            empty = dict(
                symbol=sym, ltp=round(ltp, 2), bar_time=bar_t,
                rsi=None, adx=None, plus_di=None, minus_di=None,
                macd=None, macd_signal=None, macd_hist=None,
                support=None, vwap=None, above_vwap=None, pattern=None,
            )
            if len(c5) < 30:
                out.append(empty)
                continue

            today = c5[-1].start_time[:10]   # today's bars are a contiguous suffix
            j = len(c5)
            while j > 0 and c5[j - 1].start_time[:10] == today:
                j -= 1
            ind = compute_indicators(c5, session_candles_5m=c5[j:])

            hist = (round(ind.macd_line - ind.macd_signal_line, 4)
                    if ind.macd_line is not None and ind.macd_signal_line is not None
                    else None)
            out.append(dict(
                symbol      = sym,
                ltp         = round(ltp, 2),
                bar_time    = bar_t,
                rsi         = round(ind.rsi, 1)          if ind.rsi         is not None else None,
                adx         = round(ind.adx, 1)          if ind.adx                     else None,
                plus_di     = round(ind.plus_di, 1)      if ind.plus_di                 else None,
                minus_di    = round(ind.minus_di, 1)     if ind.minus_di                else None,
                macd        = round(ind.macd_line, 4)    if ind.macd_line   is not None else None,
                macd_signal = round(ind.macd_signal_line, 4) if ind.macd_signal_line is not None else None,
                macd_hist   = hist,
                support     = round(ind.support_level, 2) if ind.support_level          else None,
                vwap        = round(ind.vwap, 2)          if ind.vwap                   else None,
                above_vwap  = ind.price_above_vwap,
                pattern     = ind.candle_pattern,
            ))

        return sorted(out, key=lambda x: x["symbol"])

    return await asyncio.to_thread(_compute_all)


# ── Dashboard WebSocket ───────────────────────────────────────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
