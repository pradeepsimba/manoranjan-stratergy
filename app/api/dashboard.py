from __future__ import annotations

import asyncio
import csv
import io
import time
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

import app.config as cfg
import app.services.settings as settings
from app.backtest.data import warmup_calendar_days
from app.backtest.engine import run_backtest
from app.engine.watchlist import fetch_active_watchlist
from app.models import Candle
from app.services.historical_data import IST, fetch_indicator_history
from app.services.snapshot import apply_depth, stub_entry
from app.state import get_state, spawn
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
async def watchlist_full() -> List[Dict[str, Any]]:
    """Today's whole high-volume universe, flagged with tradeable/open state."""
    st = get_state()
    # Premarket normally populates full_watchlist at 09:00 IST. Before that (or
    # off-session), fetch the client-status universe on demand and seed state so
    # the add-symbol box can BOTH suggest and add stocks any time — otherwise
    # watchlist_add would refuse them ("universe not loaded yet"). Safe: the WS
    # isn't running yet, and premarket overwrites these later.
    if not st.full_watchlist:
        try:
            uni = await fetch_active_watchlist()
        except Exception as e:
            print(f"watchlist_full on-demand fetch failed: {e}")
            uni = {}
        if uni:
            st.full_watchlist = uni
            st.token_to_name  = {tok: name for name, tok in uni.items()}
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
    # Holding style: "intraday" (EOD square-off) or "delivery" (positional,
    # overnight holds); None → the BACKTEST_MODE setting. 1d bars are always
    # positional regardless.
    mode:         Optional[str]            = None
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
        settings.validate_indicator_periods(attr_overrides)
    except ValueError as e:
        raise HTTPException(400, f"overrides: {e}")

    slippage = req.slippage_bps if req.slippage_bps is not None else \
        attr_overrides.get("SLIPPAGE_BPS", cfg.SLIPPAGE_BPS)
    capital = req.capital if req.capital is not None else \
        attr_overrides.get("ACCOUNT_BALANCE", cfg.ACCOUNT_BALANCE)
    timeframe = req.timeframe or attr_overrides.get("BACKTEST_TIMEFRAME") or cfg.BACKTEST_TIMEFRAME
    mode = req.mode or attr_overrides.get("BACKTEST_MODE") or cfg.BACKTEST_MODE
    if capital <= 0:
        raise HTTPException(400, "capital must be greater than 0")
    if slippage < 0:
        raise HTTPException(400, "slippage_bps must be ≥ 0")
    if timeframe not in cfg.BACKTEST_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {cfg.BACKTEST_TIMEFRAMES}")
    if mode not in cfg.BACKTEST_MODES:
        raise HTTPException(400, f"mode must be one of {cfg.BACKTEST_MODES}")
    if timeframe == "1d":
        mode = "delivery"   # 1d bars are days — positional by construction

    run_id = uuid.uuid4().hex[:12]
    await _db.create_backtest_run(
        run_id, req.from_date, req.to_date,
        {"slippage_bps": slippage, "capital": capital,
         "timeframe": timeframe, "mode": mode, "overrides": attr_overrides},
    )
    spawn(
        run_backtest(_db, run_id, req.from_date, req.to_date,
                     slippage, capital, overrides=attr_overrides,
                     timeframe=timeframe, mode=mode)
    )
    return {"run_id": run_id, "status": "running", "timeframe": timeframe, "mode": mode}


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


# ── Live indicators on ANY timeframe (live viewer) ────────────────────────────
# History at `timeframe` is fetched from the REST server at most once per
# _TF_CACHE_TTL seconds (per TF, whole watchlist, cached) — every request then
# patches TODAY's bars from live in-process data before the TA-Lib pass, so the
# view is tick-fresh without hammering the external server:
#   • TF ≥ 5m (incl. 1d) — today's bars are resampled from the live 5m candle
#     stream (covers the FULL watchlist via the secondary WS connection), so
#     the forming bar moves with every tick.
#   • TF < 5m (1m/3m)   — bars finer than the 5m stream can't be derived; the
#     last bar is patched with the live LTP, new bars appear on refetch.
# Live LTP + order-book depth are merged into every row. The page polls this
# every few seconds; only the TTL refetch touches the external server.

_TF_CACHE_TTL  = 60.0    # s between external refetches per TF
_TF_NEG_TTL    = 15.0    # retry backoff after an empty fetch (server down/no data)
_TF_IDLE_EVICT = 600.0   # drop a TF's history after this long with no viewer

_tf_hist_cache: Dict[str, Dict[str, Any]] = {}   # tf → {ts, sig, hist, access}
_tf_hist_locks: Dict[str, asyncio.Lock]   = {}
_tf_refreshing: set                        = set()   # TFs with an in-flight bg refetch


async def _refetch_tf_history(wl: Dict[str, str], timeframe: str,
                              sig: frozenset) -> tuple:
    """Fetch + cache one TF's history (single-flight via the per-TF lock).
    Returns (hist, fetch_ts) — fetch_ts is a monotonic stamp unique to this
    fetch, used downstream to key the per-symbol patched-candle cache."""
    lock = _tf_hist_locks.setdefault(timeframe, asyncio.Lock())
    async with lock:
        # Re-check after the wait — another request may have refreshed already.
        ent = _tf_hist_cache.get(timeframe)
        if ent and ent["sig"] == sig:
            ttl = _TF_CACHE_TTL if ent["hist"] else _TF_NEG_TTL
            if (time.monotonic() - ent["ts"]) < ttl:
                return ent["hist"], ent["ts"]
        days_back = warmup_calendar_days(timeframe, 5)
        hist = await fetch_indicator_history(wl, timeframe, days_back=days_back)
        now  = time.monotonic()
        # An empty result is cached too (with the short negative TTL) so a dead
        # data server is retried at _TF_NEG_TTL cadence, not on every poll.
        _tf_hist_cache[timeframe] = {"ts": now, "sig": sig, "hist": hist, "access": now}
        return hist, now


async def _cached_tf_history(wl: Dict[str, str], timeframe: str) -> tuple:
    """
    Watchlist history at `timeframe`, refetched at most every _TF_CACHE_TTL s.
    Stale-while-revalidate: an expired-but-present entry is served immediately
    and refreshed by a background task, so pollers never stall behind the
    multi-batch external fetch — only the very first (cold) request waits.
    Returns (hist, fetch_ts).
    """
    now = time.monotonic()
    # Idle sweep: a TF nobody has viewed for a while frees its candle lists
    # (and the per-row/per-symbol caches derived from them).
    for tf, ent in list(_tf_hist_cache.items()):
        if now - ent["access"] > _TF_IDLE_EVICT:
            del _tf_hist_cache[tf]
            _evict_tf_memos(tf)

    sig = frozenset(wl.values())         # watchlist edits invalidate the cache
    ent = _tf_hist_cache.get(timeframe)
    if ent and ent["sig"] == sig:
        ent["access"] = now
        ttl = _TF_CACHE_TTL if ent["hist"] else _TF_NEG_TTL
        if now - ent["ts"] < ttl:
            return ent["hist"], ent["ts"]
        if timeframe not in _tf_refreshing:          # single-flight bg refresh
            _tf_refreshing.add(timeframe)

            async def _bg() -> None:
                try:
                    await _refetch_tf_history(wl, timeframe, sig)
                except Exception as e:               # keep the poller alive
                    print(f"TF history refresh error ({timeframe}): {e}")
                finally:
                    _tf_refreshing.discard(timeframe)

            spawn(_bg())
        return ent["hist"], ent["ts"]                # stale but instant
    # Cold cache (or watchlist changed): block on the fetch once.
    return await _refetch_tf_history(wl, timeframe, sig)


def _min_of_day(ts: str) -> int:
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _resample_5m(live5: List[Candle], mins: int, anchor: int) -> List[Candle]:
    """Bucket chronological 5m candles into `mins`-minute bars anchored at `anchor`."""
    bars: List[Candle] = []
    for c in live5:
        bs = _min_of_day(c.start_time)
        bs -= (bs - anchor) % mins
        start = f"{c.start_time[:11]}{bs // 60:02d}:{bs % 60:02d}:00"
        if bars and bars[-1].start_time == start:
            b = bars[-1]                 # fresh Candle built below — safe to mutate
            b.high    = max(b.high, c.high)
            b.low     = min(b.low,  c.low)
            b.close   = c.close
            b.volume += c.volume
        else:
            bars.append(Candle(start_time=start, open=c.open, close=c.close,
                               high=c.high, low=c.low, volume=c.volume))
    return bars


def _today_5m(st, tok: str, today: str) -> List[Candle]:
    """
    Today's live 5m bars for a token — the chronological SUFFIX of the shared
    buffer, collected walking backward so the per-token lock is held for
    O(today's bars), not a scan of the full multi-day buffer.
    """
    out: List[Candle] = []
    with st.candle_lock(tok):
        buf = st.candles_5m.get(tok)
        if buf:
            for c in reversed(buf):
                if c.start_time[:10] != today:
                    break
                out.append(c)
    out.reverse()
    return out


def _live_patched(cached: list, timeframe: str, tok: str, sym: str,
                  st, today: str) -> list:
    """Copy of `cached` with TODAY's bars refreshed from live in-process data;
    returns `cached` itself (no copy) when there is nothing to patch."""
    mins  = cfg.TIMEFRAME_MINUTES.get(timeframe, 5)
    live5 = _today_5m(st, tok, today) if mins >= 5 else []
    ltp   = st.ltp.get(sym, 0.0)
    has_today = bool(cached) and cached[-1].start_time[:10] == today

    if not live5 and not (ltp > 0 and has_today):
        return cached                    # nothing live to fold in

    out = list(cached)                   # never mutate the shared cache entry

    if live5 and timeframe == "1d":
        open_min = cfg.MARKET_OPEN_HOUR * 60 + cfg.MARKET_OPEN_MIN
        bar = Candle(start_time=(out[-1].start_time if has_today
                                 else f"{today}T{open_min // 60:02d}:{open_min % 60:02d}:00"),
                     open=live5[0].open, close=live5[-1].close,
                     high=max(c.high for c in live5),
                     low=min(c.low for c in live5),
                     volume=sum(c.volume for c in live5))
        if has_today:
            out[-1] = bar                # replace today's (stale) daily bar
        else:
            out.append(bar)
    elif live5:
        # Anchor buckets the way the server does. Every bucket of a day shares
        # the same modulo-`mins` residue, so the cached TAIL bar (when it is
        # today's) yields the anchor in O(1); otherwise fall back to the
        # session open (a dynamic setting — resolved here, not at import).
        if has_today:
            anchor = _min_of_day(out[-1].start_time) % mins
        else:
            anchor = (cfg.MARKET_OPEN_HOUR * 60 + cfg.MARKET_OPEN_MIN) % mins
        for b in _resample_5m(live5, mins, anchor):
            if not out or b.start_time > out[-1].start_time:
                out.append(b)
            elif b.start_time == out[-1].start_time:
                # MERGE with the cached forming bar, don't replace: if the live
                # 5m buffer misses the bucket's early bars (symbol subscribed
                # mid-day), the server bar still holds the true open/high/low.
                prev = out[-1]
                out[-1] = Candle(start_time=b.start_time, open=prev.open,
                                 high=max(prev.high, b.high),
                                 low=min(prev.low, b.low),
                                 close=b.close,
                                 volume=max(prev.volume, b.volume))
            # else: bucket already final in the cached fetch — keep it
    else:
        # live5 is empty here, so the entry guard above only let us reach this
        # point because (ltp > 0 and has_today) held — out[-1] is guaranteed
        # to exist and be today's bar. Sub-5m TFs (1m/3m) hit this every poll
        # since they can't be resampled from the 5m stream; resampled TFs only
        # hit it when the live 5m stream itself is down (no ticks to fold in).
        lb = out[-1]
        out[-1] = Candle(start_time=lb.start_time, open=lb.open, close=ltp,
                         high=max(lb.high, ltp), low=min(lb.low, ltp),
                         volume=lb.volume)
    return out


# Per-(tf, token) cache of _live_patched's OWN output — distinct from the
# indicator-row memo below. Most watchlist symbols receive no 5m tick between
# polls, so re-walking candles_5m (_today_5m) and re-bucketing it
# (_resample_5m) every poll is wasted work even before TA-Lib enters the
# picture. Keyed on (hist_ts, tick_version): hist_ts changes exactly when
# _cached_tf_history refetches (new multi-day base data); tick_version[tok]
# changes exactly when a NEW 5m candle is durably upserted for that token
# (bumped under the same candle_lock as the mutation — see
# MarketDataService._process_tick and scheduler._load_all_historical, the
# only two writers of candles_5m). Skipped for TF < 5m: their output folds in
# the live LTP every poll (see _live_patched's `else` branch), so it changes
# far more often than tick_version and caching it would mostly just miss.
_patched_cache: Dict[tuple, tuple] = {}   # (tf, tok) → (hist_ts, tick_version, out)


def _get_patched(hist: Dict[str, list], hist_ts: float, timeframe: str,
                 tok: str, sym: str, st, today: str) -> list:
    mins = cfg.TIMEFRAME_MINUTES.get(timeframe, 5)
    if mins < 5:
        return _live_patched(hist.get(tok) or [], timeframe, tok, sym, st, today)
    version = st.tick_version.get(tok, 0)
    key = (timeframe, tok)
    ent = _patched_cache.get(key)
    if ent is not None and ent[0] == hist_ts and ent[1] == version:
        return ent[2]
    out = _live_patched(hist.get(tok) or [], timeframe, tok, sym, st, today)
    _patched_cache[key] = (hist_ts, version, out)
    return out


# Per-(tf, token) memo of the last computed row/signal: most watchlist symbols
# receive no ticks between polls, so their candle inputs — and therefore the
# whole TA-Lib pass — are bit-identical poll after poll. The fingerprint (last
# bar's identity + OHLCV + list length) changes on any tick, resample, LTP
# patch, or refetch; on a hit the cached result is reused and only the cheap
# TF-agnostic fields (live LTP, order-book depth) are refreshed.
_tf_row_memo: Dict[tuple, tuple] = {}   # (tf, tok) → (fingerprint, entry dict)
_mtf_sig_memo: Dict[tuple, tuple] = {}  # (tf, tok) → (fingerprint, signal dict)


def _evict_tf_memos(timeframe: str) -> None:
    for memo in (_tf_row_memo, _mtf_sig_memo, _patched_cache):
        for key in [k for k in memo if k[0] == timeframe]:
            del memo[key]


def _bar_fingerprint(candles: list) -> Optional[tuple]:
    """
    Cache key for a memoized indicator row: candle identity + the dynamic
    settings generation. Without the generation, a Settings-page change to
    RSI_PERIOD/MACD_*/ADX_THRESHOLD/COND_*/GATE_*/TALIB_LOOKBACK etc. would
    never invalidate an already-memoized row (bars unchanged) — the TF viewer
    would keep serving pre-change indicator values until a new bar arrives,
    which can be a full bar duration away (up to a day for the 1d TF).
    """
    if not candles:
        return None
    last = candles[-1]
    return (cfg.settings_generation(), len(candles), last.start_time,
            last.open, last.high, last.low, last.close, last.volume)


def _last_day_start(candles: list) -> int:
    """Index of the first bar of the last day — the session suffix start.
    Feeds session_candles_5m, so it must never span more than one day (a
    multi-day slice computes a wrong session VWAP)."""
    i = len(candles)
    day = candles[-1].start_time[:10]
    while i > 0 and candles[i - 1].start_time[:10] == day:
        i -= 1
    return i


def _tf_indicator_rows(watchlist: Dict[str, str],
                       hist: Dict[str, list],
                       timeframe: str,
                       hist_ts: float) -> List[Dict[str, Any]]:
    from app.engine.indicator_engine import compute_indicators   # numpy/TA-Lib
    st        = get_state()
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    for sym, tok in watchlist.items():
        candles = _get_patched(hist, hist_ts, timeframe, tok, sym, st, today_ist)
        live_ltp = round(st.ltp.get(sym, 0.0), 2)

        fp   = _bar_fingerprint(candles)
        memo = _tf_row_memo.get((timeframe, tok))
        if memo is not None and memo[0] == fp:
            entry = dict(memo[1])                       # unchanged bars → no TA-Lib
            if live_ltp > 0:
                entry["ltp"] = live_ltp
            apply_depth(entry, st.depth.get(sym, {}))   # depth is always live
            out.append(entry)
            continue

        entry = stub_entry()
        apply_depth(entry, st.depth.get(sym, {}))   # live order book (TF-agnostic)
        entry["symbol"] = sym
        if len(candles) < 3:
            entry["ltp"]      = live_ltp if live_ltp > 0 else (round(candles[-1].close, 2) if candles else 0.0)
            entry["bar_time"] = candles[-1].start_time[11:16] if candles else "—"
            _tf_row_memo[(timeframe, tok)] = (fp, dict(entry))
            out.append(entry)
            continue
        # today's session suffix (chronological) → correct session VWAP per TF
        i = _last_day_start(candles)
        ind = compute_indicators(candles, session_candles_5m=candles[i:])
        macd_hist = round(ind.macd_histogram, 4) if ind.macd_histogram is not None else None
        entry.update({
            "ltp":         live_ltp if live_ltp > 0 else round(candles[-1].close, 2),
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
        _tf_row_memo[(timeframe, tok)] = (fp, dict(entry))
        out.append(entry)
    # Order doesn't matter: the one consumer (indicators.js loadTF) rebuilds a
    # symbol-keyed map and sorts client-side — an O(n log n) sort here on every
    # poll would be pure waste.
    return out


@router.get("/api/indicators/tf/{timeframe}", response_model=None)
async def indicators_by_timeframe(timeframe: str) -> List[Dict[str, Any]]:
    if not cfg.is_timeframe(timeframe):
        raise HTTPException(400, f"timeframe must be one of {cfg.TIMEFRAMES}")
    st = get_state()
    wl = dict(st.full_watchlist if st.full_watchlist else st.active_watchlist)
    if not wl:
        return []
    hist, hist_ts = await _cached_tf_history(wl, timeframe)
    # Live patch + TA-Lib pass off the event loop.
    return await asyncio.to_thread(_tf_indicator_rows, wl, hist, timeframe, hist_ts)


# ── Multi-timeframe comparison (confluence) ───────────────────────────────────
# For each stock, computes the SAME indicators on several timeframes at once and
# scores each TF's directional bias from three sub-signals (above-VWAP, MACD line
# vs signal, +DI vs −DI): 0–3, ≥2 = bullish-leaning. The per-stock "confluence" =
# how many selected TFs are bullish-leaning; rows sort by it so the strongest
# multi-timeframe alignments surface first. Uses the shared TF history cache +
# the same live today-bar patching as the single-TF viewer.

def _mtf_tf_signals(watchlist: Dict[str, str], hist: Dict[str, list],
                    timeframe: str, hist_ts: float) -> Dict[str, Any]:
    """Per-symbol bias signals for ONE timeframe (runs in a worker thread)."""
    from app.engine.indicator_engine import compute_indicators   # numpy/TA-Lib
    st        = get_state()
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    out: Dict[str, Any] = {}
    for sym, tok in watchlist.items():
        candles = _get_patched(hist, hist_ts, timeframe, tok, sym, st, today_ist)
        if len(candles) < 3:
            out[sym] = None
            continue

        fp   = _bar_fingerprint(candles)
        memo = _mtf_sig_memo.get((timeframe, tok))
        if memo is not None and memo[0] == fp:
            out[sym] = memo[1]                    # unchanged bars → no TA-Lib
            continue

        i = _last_day_start(candles)
        ind = compute_indicators(candles, session_candles_5m=candles[i:])
        above   = bool(ind.price_above_vwap)
        macd_up = (ind.macd_line is not None and ind.macd_signal_line is not None
                   and ind.macd_line > ind.macd_signal_line)
        di_up   = (ind.plus_di is not None and ind.minus_di is not None
                   and ind.plus_di > ind.minus_di)
        sig = {
            "rsi":   round(ind.rsi, 1) if ind.rsi is not None else None,
            "score": int(above) + int(macd_up) + int(di_up),   # 0–3
            "vwap":  above, "macd": macd_up, "di": di_up,
            "close": round(candles[-1].close, 2),
        }
        _mtf_sig_memo[(timeframe, tok)] = (fp, sig)
        out[sym] = sig
    return out


@router.get("/api/indicators/mtf", response_model=None)
async def indicators_mtf(tfs: str = "5m,15m,1h") -> Dict[str, Any]:
    # Validate + dedupe (preserve order) + cap at 4 to bound compute.
    chosen: List[str] = []
    for t in tfs.split(","):
        t = t.strip()
        if cfg.is_timeframe(t) and t not in chosen:
            chosen.append(t)
    chosen = chosen[:4]
    if len(chosen) < 2:
        raise HTTPException(400, "pick at least 2 valid timeframes to compare")

    st = get_state()
    wl = dict(st.full_watchlist if st.full_watchlist else st.active_watchlist)
    if not wl:
        return {"timeframes": chosen, "rows": []}

    # Each TF fetched via the shared cache, concurrently (per-TF locks) — so
    # compare polls reuse the same history the single-TF viewer keeps warm.
    fetched = await asyncio.gather(
        *[_cached_tf_history(wl, tf) for tf in chosen])
    # Each TF's live patch + TA-Lib pass in its own thread (GIL released).
    sigs = await asyncio.gather(
        *[asyncio.to_thread(_mtf_tf_signals, wl, h, tf, ts)
          for tf, (h, ts) in zip(chosen, fetched)])
    sig_by_tf = dict(zip(chosen, sigs))

    live = st.ltp
    smallest = min(chosen, key=lambda t: cfg.TIMEFRAME_MINUTES.get(t, 5))
    rows: List[Dict[str, Any]] = []
    for sym in wl:
        per, bull, have = {}, 0, False
        for tf in chosen:
            s = sig_by_tf[tf].get(sym)
            per[tf] = s
            if s:
                have = True
                if s["score"] >= 2:
                    bull += 1
        if not have:
            continue
        lp = round(live.get(sym, 0.0), 2)
        if lp <= 0:
            small = per.get(smallest)
            lp = small["close"] if small else 0.0
        rows.append({"symbol": sym, "ltp": lp, "tf": per, "bull": bull, "n": len(chosen)})

    rows.sort(key=lambda r: (-r["bull"], r["symbol"]))
    return {"timeframes": chosen, "rows": rows}


# ── Timeframes (choice set for the UI) ────────────────────────────────────────

@router.get("/api/timeframes")
def timeframes() -> Dict[str, Any]:
    return {"timeframes": cfg.TIMEFRAMES,                 # viewer: all intervals
            "backtest_timeframes": cfg.BACKTEST_TIMEFRAMES,  # replay: intraday only
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
