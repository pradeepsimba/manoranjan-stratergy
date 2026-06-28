from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, time as dtime
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import app.config as cfg
from app.models import ActiveTrade, Trade
from app.state import AppState, EntryDiagnostics, MomResult, PendingSignal, StockStat, get_state

if TYPE_CHECKING:
    from app.services.database import DatabaseService

IST = ZoneInfo("Asia/Kolkata")

_entry_lock: Optional[asyncio.Lock] = None


def _get_entry_lock() -> asyncio.Lock:
    global _entry_lock
    if _entry_lock is None:
        _entry_lock = asyncio.Lock()
    return _entry_lock


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    t     = now_ist().time()
    open_ = dtime(cfg.MARKET_OPEN_HOUR,  cfg.MARKET_OPEN_MIN)
    close = dtime(cfg.MARKET_CLOSE_HOUR, cfg.MARKET_CLOSE_MIN)
    return open_ <= t <= close


def is_in_time_window() -> bool:
    t     = now_ist().time()
    start = dtime(cfg.ENTRY_START_HOUR, cfg.ENTRY_START_MIN)
    end   = dtime(cfg.ENTRY_END_HOUR,   cfg.ENTRY_END_MIN)
    return start <= t < end


def effective_threshold(stock: str, interval: str) -> int:
    base = cfg.STOCK_QTY_THRESHOLD.get(stock)
    if base is None:
        return 9999
    mult = {"3m": 1.5, "5m": 2.0, "15m": 6.0}.get(interval, 1.0)
    return int(base * mult)


def _is_candle_closed(start_time: str, interval: str) -> bool:
    if not start_time:
        return False
    try:
        s     = start_time[:19].replace(" ", "T")
        start = datetime.fromisoformat(s).replace(tzinfo=IST)
        mins  = {"3m": 3, "5m": 5, "15m": 15}.get(interval, 1)
        return now_ist() > start + timedelta(minutes=mins)
    except Exception:
        return False


def _calc_num_lots(state: AppState) -> int:
    risk_budget = state.available_funds * 0.01
    risk_per_lot = cfg.STOPLOSS * cfg.LOT_SIZE   # 18 × 30 = 540
    lots = int(risk_budget / risk_per_lot)
    return max(1, min(lots, 10))


# ── Leader momentum ───────────────────────────────────────────────────────────

def leaders_momentum():
    from app.engine.trade_engine import _LeaderResult
    st = get_state()
    for stock_name in cfg.LEADER_STOCKS:
        stock   = next((s for s in cfg.STOCKS if s.name == stock_name), None)
        candles = st.last_n_candles.get(stock.symbol, []) if stock else []
        if not candles:
            return _LeaderResult("Nobuysell", f"Leader candle missing: {stock_name}")
    buy_leaders  = []
    sell_leaders = []
    for stock_name in cfg.LEADER_STOCKS:
        stock   = next((s for s in cfg.STOCKS if s.name == stock_name), None)
        candles = st.last_n_candles.get(stock.symbol, []) if stock else []
        if not candles:
            continue
        c = candles[-1]
        if   c.close > c.open: buy_leaders.append(stock_name)
        elif c.close < c.open: sell_leaders.append(stock_name)
    n = cfg.SAME_DIRECTION_REQUIRED
    if len(buy_leaders)  >= n:
        return _LeaderResult("BUY",  "+".join(buy_leaders[:n])  + " aligned")
    if len(sell_leaders) >= n:
        return _LeaderResult("SELL", "+".join(sell_leaders[:n]) + " aligned")
    return _LeaderResult("Nobuysell", "No leader match")


class _LeaderResult:
    def __init__(self, signal: str, reason: str):
        self.signal = signal
        self.reason = reason


# ── Entry check ───────────────────────────────────────────────────────────────

async def check_trade_entry(db: "DatabaseService | None" = None) -> EntryDiagnostics:
    from app.engine.indicator_engine import (
        check_bn_indicators, sideways_range, strong_momentum
    )
    st = get_state()
    async with _get_entry_lock():
        interval   = st.selected_interval
        bn_candles = st.last_n_candles.get(cfg.INDEX_SYMBOL, [])
        bn         = bn_candles[-1] if bn_candles else None

        market_open  = is_market_open()
        time_ok      = is_in_time_window()
        no_active    = st.active_trade is None
        elapsed      = time.monotonic() - st.last_exit_time if st.last_exit_time else float("inf")
        cooldown_ms  = elapsed * 1000

        side_range = sideways_range(bn_candles)
        range_ok   = side_range is not None and side_range >= 12
        mom        = strong_momentum(bn_candles, interval)
        leader_sig = leaders_momentum()
        sig_ok     = leader_sig.signal in ("BUY", "SELL")

        green = red = strong_qty = 0
        stock_stats = []
        for stock_name in cfg.LEADER_STOCKS:
            stock = next((s for s in cfg.STOCKS if s.name == stock_name), None)
            if not stock:
                continue
            sc        = st.last_n_candles.get(stock.symbol, [])
            c         = sc[-1] if sc else None
            if c:
                if   c.close > c.open: green += 1
                elif c.close < c.open: red   += 1
            qty       = st.latest_minute_qty.get(stock_name, 0.0)
            threshold = effective_threshold(stock_name, interval)
            if qty >= threshold:
                strong_qty += 1
            stock_stats.append(StockStat(stock=stock_name, candle=c, qty=qty, threshold=threshold))

        dir_ok          = max(green, red) >= cfg.SAME_DIRECTION_REQUIRED
        sq_ok           = strong_qty >= cfg.SAME_DIRECTION_REQUIRED
        candle_close_ok = bn is not None and _is_candle_closed(bn.start_time, interval)
        already_traded  = bn is not None and bn.start_time[:16] == st.last_trade_candle

        bn_ind = check_bn_indicators()
        st.bn_indicators = bn_ind

        macd_met = bn_ind.macd_dir not in (None, "—", "NEUTRAL")
        ema_met  = bn_ind.ema_stack is not None and (bn_ind.ema_stack.bullish or bn_ind.ema_stack.bearish)
        gate_ok  = bn_ind.bullish or bn_ind.bearish

        candle_close_time: Optional[str] = None
        if bn and bn.start_time:
            try:
                s     = bn.start_time[:19].replace(" ", "T")
                start = datetime.fromisoformat(s).replace(tzinfo=IST)
                mins  = {"3m": 3, "5m": 5, "15m": 15}.get(interval, 1)
                candle_close_time = (start + timedelta(minutes=mins)).strftime("%H:%M")
            except Exception:
                pass

        diag = EntryDiagnostics(
            market_open=market_open, time_window_ok=time_ok, no_active_trade=no_active,
            cooldown_ms=cooldown_ms, sideways_range=side_range, candle_close_ok=candle_close_ok,
            leader_signal_type=leader_sig.signal, leader_signal_reason=leader_sig.reason,
            green=green, red=red, strong_qty=strong_qty,
            already_traded_candle=already_traded, bn_ind=bn_ind, bn_candle=bn,
            stocks=stock_stats, momentum=MomResult(ok=mom.ok, reason=mom.reason),
            candle_close_time=candle_close_time,
        )
        st.entry_diagnostics = diag

        all_but_close = (
            market_open and time_ok and no_active
            and cooldown_ms >= 60_000 and range_ok and mom.ok
            and sig_ok and dir_ok and sq_ok and not already_traded
            and macd_met and ema_met and gate_ok
        )
        all_ok = all_but_close and candle_close_ok

        if all_but_close and not candle_close_ok:
            st.pending_signal = PendingSignal(type=leader_sig.signal, reason=leader_sig.reason)
        else:
            st.pending_signal = None

        if all_ok and st.active_trade is None:
            entry_price = st.bn_ltp if st.bn_ltp > 0 else (bn.close if bn else 0.0)
            candle_time = bn.start_time[:16] if bn else ""
            if bn_ind.bullish and leader_sig.signal == "BUY":
                await enter_trade(st, "BUY",  entry_price, candle_time, "AUTO", db)
            elif bn_ind.bearish and leader_sig.signal == "SELL":
                await enter_trade(st, "SELL", entry_price, candle_time, "AUTO", db)

        return diag


# ── Enter / Exit ──────────────────────────────────────────────────────────────

async def enter_trade(
    state: AppState,
    trade_type: str,
    price: float,
    candle_time: str,
    confidence: str,
    db: "DatabaseService | None" = None,
) -> None:
    if state.active_trade is not None:
        return
    num_lots             = _calc_num_lots(state)
    state.active_trade   = ActiveTrade(
        type=trade_type, entry=price, entry_time=candle_time,
        confidence=confidence, num_lots=num_lots,
    )
    state.last_trade_candle = candle_time
    state.signal_locked     = True

    t            = Trade()
    t.type       = trade_type
    t.price      = price
    t.time       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t.confidence = confidence
    t.pnl        = 0.0
    if db:
        await db.save_trade(t)
    print(f"ENTRY {trade_type} @ {price:.2f} [{candle_time}] conf={confidence} lots={num_lots}")


async def check_exit(state: AppState, db: "DatabaseService | None" = None) -> None:
    if state.active_trade is None:
        return
    bn_candles = state.last_n_candles.get(cfg.INDEX_SYMBOL, [])
    if not bn_candles:
        return
    price = state.bn_ltp if state.bn_ltp > 0 else bn_candles[-1].close
    at    = state.active_trade

    if at.type == "BUY":
        pnl = price - at.entry
        if   pnl >= cfg.TRAIL_TRIGGER:     at.current_sl = max(at.current_sl, price - cfg.TRAIL_DISTANCE)
        elif pnl >= cfg.BREAKEVEN_TRIGGER: at.current_sl = max(at.current_sl, at.entry)
        if price >= at.entry + cfg.TARGET or price <= at.current_sl:
            await _exit_trade(state, price, pnl, db)
    else:
        pnl = at.entry - price
        if   pnl >= cfg.TRAIL_TRIGGER:     at.current_sl = min(at.current_sl, price + cfg.TRAIL_DISTANCE)
        elif pnl >= cfg.BREAKEVEN_TRIGGER: at.current_sl = min(at.current_sl, at.entry)
        if price <= at.entry - cfg.TARGET or price >= at.current_sl:
            await _exit_trade(state, price, pnl, db)


async def _exit_trade(state: AppState, exit_price: float, pnl_pts: float,
                      db: "DatabaseService | None" = None) -> None:
    if state.active_trade is None:
        return
    pnl_pts  = round(pnl_pts, 2)
    num_lots = state.active_trade.num_lots
    pnl_rs   = round(pnl_pts * num_lots * cfg.LOT_SIZE, 2)

    t            = Trade()
    t.type       = state.active_trade.type + "_EXIT"
    t.price      = exit_price
    t.time       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t.confidence = state.active_trade.confidence
    t.pnl        = pnl_rs
    if db:
        await db.save_trade(t)

    state.available_funds += pnl_rs
    state.active_trade    = None
    state.last_exit_time  = time.monotonic()
    state.signal_locked   = False
    state.pending_signal  = None
    print(f"EXIT @ {exit_price:.2f} PnL={pnl_pts:.2f} pts = ₹{pnl_rs:.2f} ({num_lots} lots)")


# ── Manual entry / exit ───────────────────────────────────────────────────────

async def manual_entry(state: AppState, trade_type: str, price: float,
                       db: "DatabaseService | None" = None) -> None:
    if state.active_trade is not None:
        return
    candle_time = now_ist().strftime("%Y-%m-%dT%H:%M")
    await enter_trade(state, trade_type, price, candle_time, "MANUAL", db)


async def manual_exit(state: AppState, db: "DatabaseService | None" = None) -> None:
    if state.active_trade is None:
        return
    bn_candles = state.last_n_candles.get(cfg.INDEX_SYMBOL, [])
    if not bn_candles:
        return
    price = bn_candles[-1].close
    pnl   = (price - state.active_trade.entry
             if state.active_trade.type == "BUY"
             else state.active_trade.entry - price)
    await _exit_trade(state, price, pnl, db)
