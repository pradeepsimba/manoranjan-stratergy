from __future__ import annotations

import asyncio
import csv
import io
import math
import uuid
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
import app.services.settings as settings
from app.backtest.engine import run_backtest
from app.services.historical_data import fetch_indicator_history
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


# ── Watchlist ─────────────────────────────────────────────────────────────────

class WatchlistOp(BaseModel):
    symbol: str


@router.get("/api/watchlist")
def watchlist() -> List[Dict[str, str]]:
    st = get_state()
    return [{"symbol": sym, "token": tok}
            for sym, tok in st.active_watchlist.items()]


@router.get("/api/watchlist/full")
def watchlist_full() -> List[Dict[str, Any]]:
    """Today's whole high-volume universe, flagged with tradeable/open state."""
    st = get_state()
    universe = dict(st.full_watchlist)
    # Restored open positions can be active without being in today's universe.
    for sym, tok in st.active_watchlist.items():
        universe.setdefault(sym, tok)
    return [{
        "symbol": sym,
        "token":  tok,
        "active": sym in st.active_watchlist,
        "open":   sym in st.positions,
        "ai":     sym in st.gemini_shortlist,
    } for sym, tok in sorted(universe.items())]


@router.post("/api/watchlist/add")
async def watchlist_add(req: WatchlistOp) -> Dict[str, Any]:
    if _sched is None:
        raise HTTPException(503, "Scheduler not ready")
    try:
        return await _sched.watchlist_add(req.symbol)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.post("/api/watchlist/remove")
async def watchlist_remove(req: WatchlistOp) -> Dict[str, Any]:
    if _sched is None:
        raise HTTPException(503, "Scheduler not ready")
    try:
        return await _sched.watchlist_remove(req.symbol)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


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
    # None = use the CURRENT dynamic settings (resolved at request time —
    # a pydantic default would freeze the import-time value).
    slippage_bps: Optional[float]          = None
    capital:      Optional[float]          = None
    # Bar interval to replay; None → the BACKTEST_TIMEFRAME setting.
    timeframe:    Optional[str]            = None
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
        # live-only times (premarket/open/session-end) would falsely reject.
        settings.validate_time_order(attr_overrides, points=("SCAN_START", "CUTOFF"))
    except ValueError as e:
        raise HTTPException(400, f"overrides: {e}")

    slippage = req.slippage_bps if req.slippage_bps is not None else \
        attr_overrides.get("SLIPPAGE_BPS", cfg.SLIPPAGE_BPS)
    capital = req.capital if req.capital is not None else \
        attr_overrides.get("ACCOUNT_BALANCE", cfg.ACCOUNT_BALANCE)
    timeframe = req.timeframe or attr_overrides.get("BACKTEST_TIMEFRAME") or cfg.BACKTEST_TIMEFRAME
    if capital <= 0:
        raise HTTPException(400, "capital must be greater than 0")
    if slippage < 0:
        raise HTTPException(400, "slippage_bps must be ≥ 0")
    if not cfg.is_timeframe(timeframe):
        raise HTTPException(400, f"timeframe must be one of {cfg.TIMEFRAMES}")

    run_id = uuid.uuid4().hex[:12]
    await _db.create_backtest_run(
        run_id, req.from_date, req.to_date,
        {"slippage_bps": slippage, "capital": capital,
         "timeframe": timeframe, "overrides": attr_overrides},
    )
    asyncio.create_task(
        run_backtest(_db, run_id, req.from_date, req.to_date,
                     slippage, capital, overrides=attr_overrides, timeframe=timeframe)
    )
    return {"run_id": run_id, "status": "running", "timeframe": timeframe}


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


# ── Live indicators on ANY timeframe (on-demand viewer) ───────────────────────
# Fetches recent candles at `timeframe` for the whole watchlist and computes the
# SAME indicators as the live path — but from freshly-fetched history, never
# touching live state or the WS engine. Heavier than the live snapshot (one REST
# batch + a TA-Lib pass), so it is polled on demand by the indicators page, not
# streamed.

def _tf_indicator_rows(watchlist: Dict[str, str],
                       hist: Dict[str, list]) -> List[Dict[str, Any]]:
    from app.engine.indicator_engine import compute_indicators   # numpy/TA-Lib
    out: List[Dict[str, Any]] = []
    for sym, tok in watchlist.items():
        candles = hist.get(tok) or []
        entry = stub_entry()
        apply_depth(entry, {})          # add bid/ask/… keys as None (no live book)
        entry["symbol"] = sym
        if len(candles) < 3:
            entry["ltp"]      = round(candles[-1].close, 2) if candles else 0.0
            entry["bar_time"] = candles[-1].start_time[11:16] if candles else "—"
            out.append(entry)
            continue
        # today's session suffix (chronological) → correct session VWAP per TF
        today = candles[-1].start_time[:10]
        i = len(candles)
        while i > 0 and candles[i - 1].start_time[:10] == today:
            i -= 1
        ind = compute_indicators(candles, session_candles_5m=candles[i:])
        macd_hist = (round(ind.macd_line - ind.macd_signal_line, 4)
                     if ind.macd_line is not None and ind.macd_signal_line is not None else None)
        entry.update({
            "ltp":         round(candles[-1].close, 2),
            "bar_time":    candles[-1].start_time[11:16],
            "rsi":         round(ind.rsi, 1)              if ind.rsi is not None else None,
            "adx":         round(ind.adx, 1)             if ind.adx is not None else None,
            "plus_di":     round(ind.plus_di, 1)         if ind.plus_di is not None else None,
            "minus_di":    round(ind.minus_di, 1)        if ind.minus_di is not None else None,
            "macd":        round(ind.macd_line, 4)       if ind.macd_line is not None else None,
            "macd_signal": round(ind.macd_signal_line, 4) if ind.macd_signal_line is not None else None,
            "macd_hist":   macd_hist,
            "support":     round(ind.support_level, 2)   if ind.support_level else None,
            "vwap":        round(ind.vwap, 2)            if ind.vwap else None,
            "above_vwap":  ind.price_above_vwap,
            "pattern":     ind.candle_pattern,
        })
        out.append(entry)
    return sorted(out, key=lambda x: x["symbol"])


@router.get("/api/indicators/tf/{timeframe}")
async def indicators_by_timeframe(timeframe: str) -> List[Dict[str, Any]]:
    if not cfg.is_timeframe(timeframe):
        raise HTTPException(400, f"timeframe must be one of {cfg.TIMEFRAMES}")
    st = get_state()
    wl = dict(st.full_watchlist if st.full_watchlist else st.active_watchlist)
    if not wl:
        return []
    # Enough calendar days for TALIB_LOOKBACK bars to converge at this TF.
    mins      = cfg.TIMEFRAME_MINUTES.get(timeframe, 5)
    days_back = max(5, math.ceil(cfg.TALIB_LOOKBACK * mins / 375.0 * 7.0 / 5.0) + 2)
    hist = await fetch_indicator_history(wl, timeframe, days_back=days_back)
    # TA-Lib pass off the event loop.
    return await asyncio.to_thread(_tf_indicator_rows, wl, hist)


# ── Timeframes (choice set for the UI) ────────────────────────────────────────

@router.get("/api/timeframes")
def timeframes() -> Dict[str, Any]:
    return {"timeframes": cfg.TIMEFRAMES,
            "backtest_default": cfg.BACKTEST_TIMEFRAME}


# ── Dashboard WebSocket ───────────────────────────────────────────────────────

@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(websocket)
