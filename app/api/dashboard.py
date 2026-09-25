from __future__ import annotations

import asyncio
import csv
import io
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
import app.services.settings as settings
from app.auth import require_login
from app.backtest.engine import run_backtest
from app.backtest.signal_study import run_bn_leader_consensus_study
from app.models import PositionStatus, closed_tail_closes, iv_lookback_closes
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

# Strong references for fire-and-forget background tasks started from a
# request handler (found in review, 2026-09-25) — asyncio only holds a WEAK
# reference to a task created via create_task, so an unreferenced task can
# be garbage-collected mid-run. Every other create_task call in this repo
# (scheduler.py's self._tasks, market_data.py's self._tasks/_option_task)
# already keeps one; this set is the equivalent for the one-off backtest run
# task below, which is a real, potentially tens-of-seconds computation, not
# a fire-and-forget the app can afford to silently lose.
_background_tasks: set = set()

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
    # Retried (bounded, matching the algo entry path's own persistence — see
    # SchedulerService._persist_new_position) instead of one bare attempt,
    # AND rolled back on total failure (found in review, 2026-09-24): a
    # single-shot save that failed used to leave st.active_trade set with
    # NO positions row ever written — the trade still ran/exited correctly
    # in memory, but the update_position_exit at exit time would later
    # match zero rows and the trade would permanently never appear in
    # "Today's Trades"/DB history, an invisible accounting gap. Now: retry
    # first (covers the common transient case silently), and if persistence
    # is still down after retries, actually UNDO the in-memory order (clear
    # active_trade, decrement the trades-today counter place_manual_order
    # bumped) so the human sees a genuine 500 for a genuinely-not-placed
    # order, instead of a false "failed" while a live untracked trade keeps
    # running underneath them.
    if not await _sched._persist_new_position(trade, "BANKNIFTY", "BN manual"):
        st = get_state()
        # trade.status == CLOSED check (found in review, 2026-09-25): the
        # SAME await-yields-the-event-loop window the `is trade` guard below
        # already accounts for also lets the concurrent tick loop
        # (_tick_exits) fully OPEN *and* CLOSE this exact trade — via
        # bn_trade._settle, which already applied its real P&L to
        # st.funds/st.daily_pnl — before this retry loop gives up. That
        # genuinely happened; it isn't "not placed." Treating it like the
        # ordinary rollback below would decrement bn_trades_today for a
        # trade that legitimately executed (letting the daily cap be
        # exceeded) and tell the caller "rolled back" while real money had
        # already moved. _persist_closed_exit (called from _tick_exits once
        # it settles) is independently retrying the matching exit-side
        # write, so surface this distinctly rather than silently falling
        # through to the entry-rollback branch.
        if trade.status == PositionStatus.CLOSED:
            print(f"CRITICAL: BN manual order {trade.order_id} executed to "
                  f"completion (entry+exit) before its entry could be "
                  f"persisted — pnl already applied to funds, no positions "
                  f"row exists for the entry.")
            raise HTTPException(
                500,
                "Order executed and closed before it could be saved to the "
                "database — funds were updated but no trade record exists. "
                "Check server logs (CRITICAL) and reconcile manually."
            )
        # `is trade`, not unconditional (2026-09-24, found in review):
        # _persist_new_position's retry loop awaits for up to ~0.8s (longer
        # if the DB hangs rather than errors quickly), yielding the event
        # loop — the 100ms tick loop keeps running underneath this request,
        # so THIS trade could legitimately close on its own (target/stop
        # hit) and, once the cooldown allows, a DIFFERENT new trade could
        # open into st.active_trade before this retry loop finishes.
        # Blindly clearing st.active_trade here would then orphan that
        # unrelated, perfectly legitimate new trade — un-exitable via the
        # UI, silently reintroducing the exact "invisible live trade"
        # failure mode this whole fix exists to close, just for a
        # different trade. The trades-today counter decrement below is
        # unconditionally correct regardless (it only ever undoes THIS
        # call's own increment), so it stays outside the identity check.
        if st.active_trade is trade:
            st.active_trade = None
        st.bn_trades_today = max(0, st.bn_trades_today - 1)
        raise HTTPException(500, "Order could not be saved after retries — rolled back, not placed.")
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
    # closed_tail_closes() excludes the still-forming last bar — the same
    # forming-bar IV leak already fixed for every other force_close/
    # check_tick_exit caller (found in review 2026-09-22) applies here too:
    # a human clicking Exit must get the same IV basis the automated tick
    # loop would have used.
    lookback = iv_lookback_closes(bn_candles, cfg.BN_IV_LOOKBACK_BARS)

    closed = bn_trade.force_close(datetime.now(IST), st.bn_index_ltp, lookback, label="MANUAL EXIT")
    if closed is None:
        raise HTTPException(400, "No active trade to exit")
    # Routed through the same retry-hardened path the algo/EOD exits use
    # (2026-09-24, found in review) — this used to be a single unretried
    # update_position_exit call, unlike every other exit path in this app,
    # so a transient DB blip here left the positions row permanently OPEN
    # with no reconciler (the exact gap _persist_closed_exit was built to
    # close everywhere else). It logs its own CRITICAL on exhausted retries.
    await _sched._persist_closed_exit(closed, "BN manual")
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
    # Retried + rolled-back-on-failure, with the `is trade` identity guard
    # and the trade.status==CLOSED branch — see manual_order's identical
    # fix above (2026-09-24/2026-09-25, found in review) for the full
    # rationale.
    if not await _sched._persist_new_position(trade, "NIFTY50", "NF manual"):
        st = get_state()
        if trade.status == PositionStatus.CLOSED:
            print(f"CRITICAL: NF manual order {trade.order_id} executed to "
                  f"completion (entry+exit) before its entry could be "
                  f"persisted — pnl already applied to funds, no positions "
                  f"row exists for the entry.")
            raise HTTPException(
                500,
                "Order executed and closed before it could be saved to the "
                "database — funds were updated but no trade record exists. "
                "Check server logs (CRITICAL) and reconcile manually."
            )
        if st.active_trade_nf is trade:
            st.active_trade_nf = None
        st.nf_trades_today = max(0, st.nf_trades_today - 1)
        raise HTTPException(500, "Order could not be saved after retries — rolled back, not placed.")
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
    # See manual_exit's identical comment above — closed_tail_closes()
    # excludes the still-forming last bar.
    lookback = iv_lookback_closes(nf_candles, cfg.NF_IV_LOOKBACK_BARS)

    closed = nf_trade.force_close(datetime.now(IST), st.nf_index_ltp, lookback, label="MANUAL EXIT")
    if closed is None:
        raise HTTPException(400, "No active trade to exit")
    # See manual_exit's identical fix above (2026-09-24, found in review).
    await _sched._persist_closed_exit(closed, "NF manual")
    return {"orderId": closed.order_id, "exitPremium": closed.exit_premium, "pnl": closed.pnl}


# ── Reset the shared paper account (2026-09-19, explicit user decision) ────
# Resets funds back to cfg.BN_STARTING_FUNDS and today's shared daily_pnl to
# 0 — a manual "fresh start" for the paper account, distinct from the
# automatic EOD reset (scheduler._run_eod also zeroes daily_pnl every day
# regardless). Does NOT touch closed_trades/closed_trades_nf (today's trade
# history) or the DB's positions table — only the funds/pnl COUNTERS, so
# past trades remain visible for review. Refuses while a trade is open on
# either instrument, since resetting funds under an open position would
# make its eventual settlement land against the wrong baseline.
@router.post("/api/reset-funds")
async def reset_funds() -> Dict[str, Any]:
    if _db is None:
        raise HTTPException(503, "Database not ready")
    st = get_state()
    if st.active_trade is not None or st.active_trade_nf is not None:
        raise HTTPException(400, "Exit the active trade(s) before resetting funds.")
    st.funds = cfg.BN_STARTING_FUNDS
    st.daily_pnl = 0.0
    try:
        await _db.set_app_settings({BN_FUNDS_KEY: st.funds})
    except Exception as e:
        raise HTTPException(500, f"Funds reset in memory but DB persist failed: {e}")
    return {"funds": st.funds, "dailyPnl": st.daily_pnl}


# ── Live prices ───────────────────────────────────────────────────────────────

@router.get("/api/prices")
def get_prices() -> Dict[str, float]:
    st = get_state()
    prices = {cfg.BN_INDEX_NAME: st.bn_index_ltp, cfg.NF_INDEX_NAME: st.nf_index_ltp}
    # Locked (2026-09-24, found in review) — this is a sync `def` route,
    # which FastAPI/Starlette runs in a real executor thread, not on the
    # event loop. market_data.py's _process_tick writes st.ltp[name] = ltp
    # from the event loop, unlocked reasoning that's only valid against
    # OTHER event-loop-only code — it doesn't cover this handler. Without
    # the lock, a brand-new key being inserted mid-update() here (routine
    # early in a session, any tracked symbol's first tick of the run) races
    # this thread's iteration over the same dict.
    with st._ltp_lock:
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
        # As of 2026-09-23 the "Scalp Timing" group's trading-window keys
        # (SCALP_WINDOW1/2_*) are dynamic bt=True SPEC entries (the
        # 2026-09-09 cleanup's "no SPEC key is bt=True" claim this comment
        # used to make is stale — found in review), so a real override dict
        # can reach here now. The group's two time-stop keys
        # (BN_SCALP_TIME_STOP_S/NF_SCALP_TIME_STOP_S) are NOT among them —
        # they were reverted to bt=False on 2026-09-24 (found in review:
        # app/backtest/engine.py's _try_exit never reads BTPosition's frozen
        # time_stop_s at all, so offering it as a per-run override silently
        # did nothing; see CLAUDE.md's backtest fidelity limitation note).
        # expand_changes only validates each key in isolation
        # (format/bounds); a window-pair cross-check (start < end) is a
        # separate step below, same as apply_and_persist/reset in
        # settings.py — otherwise an inverted window silently makes
        # _in_trading_window unsatisfiable for the whole backtest run with
        # no error surfaced (found in review). Reuses settings.effective_state
        # (2026-09-23 fix, found in review: this used to hand-roll the same
        # current-cfg-plus-overrides merge inline instead of calling the
        # shared builder settings.py's own save/reset/startup paths use).
        attr_overrides = settings.expand_changes(req.overrides or {}, bt_only=True)
        settings.validate_scalp_windows(settings.effective_state(attr_overrides))
    except ValueError as e:
        raise HTTPException(400, f"overrides: {e}")

    # attr_overrides can never contain "SLIPPAGE_BPS" (found in review,
    # 2026-09-23) — it was pulled out of the dynamic SPEC registry on
    # 2026-09-09, so expand_changes above would reject it with a 400 before
    # this line could ever see it; the only real override path is the
    # top-level slippage_bps request field. Reads cfg.SLIPPAGE_BPS directly
    # instead of the dead attr_overrides.get(...) lookup that used to imply
    # otherwise.
    slippage = req.slippage_bps if req.slippage_bps is not None else cfg.SLIPPAGE_BPS
    if slippage < 0:
        raise HTTPException(400, "slippage_bps must be ≥ 0")

    run_id = uuid.uuid4().hex[:12]
    await _db.create_backtest_run(
        run_id, req.from_date, req.to_date,
        {"slippage_bps": slippage, "overrides": attr_overrides},
    )
    task = asyncio.create_task(
        run_backtest(_db, run_id, req.from_date, req.to_date,
                     slippage, overrides=attr_overrides)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
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
    except WebSocketDisconnect:
        pass   # normal client-side close — nothing to log
    except Exception as e:
        # Found in review (2026-09-23): this used to be a bare
        # `except Exception: ws_manager.disconnect(websocket)` with no
        # logging at all — an unexpected error here (protocol violation,
        # a real bug in the receive loop) would vanish with zero trace,
        # unlike market_data.py's equivalent WS error handlers, which all
        # print. WebSocketDisconnect (the normal close path) is still
        # silent, same as before.
        print(f"Dashboard WS error: {e}")
    finally:
        ws_manager.disconnect(websocket)
