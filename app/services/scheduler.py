from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import app.config as cfg
from app.models import BNIndicators
from app.state import EntryDiagnostics, get_state

if TYPE_CHECKING:
    from app.services.database import DatabaseService
    from app.ws.dashboard_ws import DashboardWSManager

IST              = ZoneInfo("Asia/Kolkata")
DISPLAY_INTERVALS = ["1m", "5m", "15m"]


class SchedulerService:
    def __init__(self, ws_manager: "DashboardWSManager", db: "DatabaseService") -> None:
        self._ws    = ws_manager
        self._db    = db
        self._tasks: List[asyncio.Task] = []

    def start(self) -> None:
        asyncio.create_task(self._load_initial_data())
        self._tasks = [
            asyncio.create_task(self._push_dashboard_loop()),
            asyncio.create_task(self._refresh_historical_loop()),
            asyncio.create_task(self._refresh_big_trades_loop()),
            asyncio.create_task(self._prune_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── Data loading ──────────────────────────────────────────────────────────

    async def _load_initial_data(self) -> None:
        print("Loading initial historical data in parallel…")
        await self._load_historical()

    async def _load_historical(self) -> None:
        from app.engine.sr_engine import detect
        from app.services.historical_data import fetch_bn_indicator_candles, fetch_historical
        st = get_state()
        try:
            interval = st.selected_interval
            (bn_candles, main_data, sr5, sr15, min1) = await asyncio.gather(
                fetch_bn_indicator_candles(interval),
                fetch_historical(interval, st.num_candles, st.candle_offset),
                fetch_historical("5m",  30, 0),
                fetch_historical("15m", 30, 0),
                fetch_historical("1m",   5, 0),
            )

            if bn_candles:
                with st._bn_ind_lock:
                    st.bn_indicator_candles.clear()
                    st.bn_indicator_candles.extend(bn_candles)
                print(f"Loaded {len(bn_candles)} BN indicator candles")

            for sym, candles in main_data.items():
                st.last_n_candles[sym] = list(candles)
            print(f"Loaded historical candles for {len(main_data)} stocks")

            self._process_sr_data(sr5,  st.sr5m,  "5m",  detect)
            self._process_sr_data(sr15, st.sr15m, "15m", detect)

            iv1m = st.all_interval_candles.setdefault("1m", {})
            for sym, candles in min1.items():
                iv1m[sym] = list(candles)
            print("Loaded 1m display candles")

            if interval not in ("5m", "15m", "1m"):
                iv = st.all_interval_candles.setdefault(interval, {})
                for sym, candles in main_data.items():
                    iv[sym] = list(candles[-5:])

            st.api_status = "API OK"
        except Exception as e:
            print(f"Historical load error: {e}")
            st.api_status = "API Error"

    def _process_sr_data(self, sr_data, dest, interval, detect_fn) -> None:
        st     = get_state()
        iv_map = st.all_interval_candles.setdefault(interval, {})
        for sym, candles in sr_data.items():
            stock = next((s for s in cfg.STOCKS if s.symbol == sym), None)
            if stock:
                dest[stock.name] = detect_fn(candles)
            iv_map[sym] = list(candles[-5:])
        print(f"SR+display loaded for {interval} ({len(dest)} stocks)")

    # ── Periodic tasks ────────────────────────────────────────────────────────

    async def _push_dashboard_loop(self) -> None:
        while True:
            try:
                payload = self._build_dashboard_payload()
                await self._ws.broadcast(json.dumps(payload, default=str))
            except Exception as e:
                print(f"Push error: {e}")
            await asyncio.sleep(1)

    async def _refresh_historical_loop(self) -> None:
        await asyncio.sleep(300)
        while True:
            try:
                await self._load_historical()
            except Exception as e:
                print(f"Historical refresh error: {e}")
            await asyncio.sleep(300)

    async def _refresh_big_trades_loop(self) -> None:
        while True:
            try:
                bt = await self._db.get_big_trades_data(get_state().selected_interval)
                get_state().big_trades_snapshot = {"data": bt}
            except Exception as e:
                print(f"BigTrades refresh error: {e}")
            await asyncio.sleep(5)

    async def _prune_loop(self) -> None:
        """Prune old stock rows once per trading day at 20:00 IST (Mon-Fri)."""
        while True:
            now    = datetime.now(IST)
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if now >= target or now.weekday() >= 5:
                await asyncio.sleep(3600)
                continue
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self._db.prune_old_stock_data()
            except Exception as e:
                print(f"Prune error: {e}")

    # ── Dashboard payload builder ─────────────────────────────────────────────

    def _build_dashboard_payload(self) -> Dict[str, Any]:
        st    = get_state()
        clock = datetime.now(IST).strftime("%H:%M:%S")

        payload: Dict[str, Any] = {
            "type":      "STATE_UPDATE",
            "clock":     clock,
            "wsStatus":  st.ws_status,
            "apiStatus": st.api_status,
            "interval":  st.selected_interval,
            "funds":     st.available_funds,
            "signal":    st.global_signal,
        }

        # Active trade
        if st.active_trade:
            at  = st.active_trade
            bn  = st.last_n_candles.get(cfg.INDEX_SYMBOL, [])
            ltp = st.bn_ltp if st.bn_ltp > 0 else (bn[-1].close if bn else at.entry)
            pnl_pts = (ltp - at.entry) if at.type == "BUY" else (at.entry - ltp)
            pnl_rs  = round(pnl_pts * at.num_lots * cfg.LOT_SIZE, 2)
            payload["activeTrade"] = {
                "type":       at.type,
                "entry":      at.entry,
                "entryTime":  at.entry_time,
                "confidence": at.confidence,
                "currentSL":  at.current_sl,
                "numLots":    at.num_lots,
                "ltp":        ltp,
                "pnl":        round(pnl_pts, 2),
                "pnlRs":      pnl_rs,
            }
        else:
            payload["activeTrade"] = None

        payload["pendingSignal"] = (
            {"type": st.pending_signal.type, "reason": st.pending_signal.reason}
            if st.pending_signal else None
        )

        if st.bn_indicators:
            payload["bnIndicators"] = self._indicator_map(st.bn_indicators)

        # Entry diagnostics
        diag = st.entry_diagnostics
        if diag:
            gate_ok = diag.bn_ind is not None and (diag.bn_ind.bullish or diag.bn_ind.bearish)
            dm: Dict[str, Any] = {
                "marketOpen":          diag.market_open,
                "timeWindowOk":        diag.time_window_ok,
                "noActiveTrade":       diag.no_active_trade,
                "cooldownMs":          diag.cooldown_ms,
                "sidewaysRange":       diag.sideways_range,
                "candleCloseOk":       diag.candle_close_ok,
                "candleCloseTime":     diag.candle_close_time,
                "leaderSignal":        diag.leader_signal_type,
                "leaderReason":        diag.leader_signal_reason,
                "green":               diag.green,
                "red":                 diag.red,
                "strongQty":           diag.strong_qty,
                "alreadyTradedCandle": diag.already_traded_candle,
                "gateOk":              gate_ok,
                "time":                clock,
            }
            if diag.momentum:
                dm["momentum"] = {"ok": diag.momentum.ok, "reason": diag.momentum.reason}
            if diag.bn_ind:
                dm["bnInd"] = self._indicator_map(diag.bn_ind)
            if diag.bn_candle:
                bn = diag.bn_candle
                dm["bn"] = {"open": bn.open, "close": bn.close, "startTime": bn.start_time}
            if diag.stocks:
                dm["stocks"] = [
                    {
                        "stock":     ss.stock,
                        "qty":       ss.qty,
                        "threshold": ss.threshold,
                        **({"candle": {"open": ss.candle.open, "close": ss.candle.close}}
                           if ss.candle else {}),
                    }
                    for ss in diag.stocks
                ]
            payload["entryDiag"] = dm

        payload["stocksMultiFrame"] = self._multi_frame_stocks()
        payload["multiFrameCounts"] = self._multi_frame_counts()

        # Legacy stock table
        stocks_out: List[Dict[str, Any]] = []
        for s in cfg.STOCKS:
            candles = st.last_n_candles.get(s.symbol, [])
            if not candles:
                continue
            c3 = []
            for i in range(len(candles) - 1, max(len(candles) - 4, -1), -1):
                cc = candles[i]
                t  = cc.start_time[11:16] if cc.start_time and len(cc.start_time) >= 16 else ""
                c3.append({"time": t, "diff": round(cc.close - cc.open, 2)})
            stocks_out.append({
                "name":    s.name,
                "symbol":  s.symbol,
                "c3":      c3,
                "buyQty":  st.latest_buy_qty.get(s.name,  0),
                "sellQty": st.latest_sell_qty.get(s.name, 0),
            })

        g = [0, 0, 0]; r = [0, 0, 0]; ne = [0, 0, 0]; col_times = ["", "", ""]
        for row in stocks_out:
            for i, cell in enumerate(row["c3"][:3]):
                d = cell["diff"]
                if   d > 0: g[i]  += 1
                elif d < 0: r[i]  += 1
                else:       ne[i] += 1
                if not col_times[i]:
                    col_times[i] = cell["time"]
        payload["stocks"] = stocks_out
        payload["candleCounts"] = [
            {"label": lbl, "time": col_times[i], "green": g[i], "red": r[i], "neutral": ne[i]}
            for i, lbl in enumerate(["Latest", "Previous", "PrevPrev"])
        ]

        # S/R levels
        sr_list = []
        for s in cfg.STOCKS:
            lvl5  = st.sr5m.get(s.name)
            lvl15 = st.sr15m.get(s.name)
            if not lvl5 and not lvl15:
                continue
            sr_list.append({
                "name":   s.name,
                "s5sup":  lvl5.supports    if lvl5  else [],
                "s5res":  lvl5.resistances if lvl5  else [],
                "s15sup": lvl15.supports   if lvl15 else [],
                "s15res": lvl15.resistances if lvl15 else [],
            })
        payload["srLevels"] = sr_list

        payload["trades"] = [_trade_dict(t) for t in self._db.get_today_trades()]

        if st.big_trades_snapshot:
            payload["bigTrades"] = st.big_trades_snapshot

        return payload

    def _multi_frame_stocks(self) -> List[Dict[str, Any]]:
        st   = get_state()
        rows = []
        for s in cfg.STOCKS:
            frames: Dict[str, Any] = {}
            for iv in DISPLAY_INTERVALS:
                iv_map  = st.all_interval_candles.get(iv, {})
                candles = iv_map.get(s.symbol)
                cells   = []
                for pos in range(2):
                    idx = len(candles) - 1 - pos if candles else -1
                    if idx >= 0:
                        c = candles[idx]
                        cell: Dict[str, Any] = {
                            "time": c.start_time[11:16]
                                    if c.start_time and len(c.start_time) >= 16 else "",
                            "diff": round(c.close - c.open, 2),
                        }
                    else:
                        cell = {"time": "", "diff": 0.0, "missing": True}
                    cells.append(cell)
                frames[iv] = cells
            rows.append({
                "name":    s.name,
                "symbol":  s.symbol,
                "frames":  frames,
                "buyQty":  st.latest_buy_qty.get(s.name,  0),
                "sellQty": st.latest_sell_qty.get(s.name, 0),
            })
        return rows

    def _multi_frame_counts(self) -> Dict[str, List[Dict[str, Any]]]:
        st     = get_state()
        result: Dict[str, List[Dict[str, Any]]] = {}
        for iv in DISPLAY_INTERVALS:
            iv_map = st.all_interval_candles.get(iv, {})
            g = [0, 0]; r = [0, 0]; ne = [0, 0]; times = ["", ""]
            for s in cfg.STOCKS:
                candles = iv_map.get(s.symbol)
                for pos in range(2):
                    idx = len(candles) - 1 - pos if candles else -1
                    if idx >= 0:
                        c = candles[idx]
                        if   c.close > c.open: g[pos]  += 1
                        elif c.close < c.open: r[pos]  += 1
                        else:                  ne[pos] += 1
                        if not times[pos] and s.symbol == cfg.INDEX_SYMBOL:
                            times[pos] = (c.start_time[11:16]
                                          if c.start_time and len(c.start_time) >= 16 else "")
                    else:
                        ne[pos] += 1
            result[iv] = [
                {"label": lbl, "time": times[i], "green": g[i], "red": r[i], "neutral": ne[i]}
                for i, lbl in enumerate(["Latest", "Previous"])
            ]
        return result

    @staticmethod
    def _indicator_map(ind: BNIndicators) -> Dict[str, Any]:
        m: Dict[str, Any] = {
            "rsi":     ind.rsi,
            "macdDir": ind.macd_dir,
            "macdVal": ind.macd_val,
            "bull":    ind.bull,
            "bear":    ind.bear,
            "bullish": ind.bullish,
            "bearish": ind.bearish,
        }
        if ind.ema_stack:
            ema_d = {
                "ema20":   ind.ema_stack.ema20,
                "ema50":   ind.ema_stack.ema50,
                "bullish": ind.ema_stack.bullish,
                "bearish": ind.ema_stack.bearish,
            }
            m["emaStack"] = ema_d
            m["ema"]      = ema_d
        if ind.leader_pat:
            m["leaderPat"] = {
                "bullCount": ind.leader_pat.bull_count,
                "bearCount": ind.leader_pat.bear_count,
                "matches":   [{"stock": pm.stock, "pattern": pm.pattern}
                               for pm in ind.leader_pat.matches],
            }
        return m


def _trade_dict(t) -> Dict[str, Any]:
    return {
        "id":            t.id,
        "type":          t.type,
        "price":         t.price,
        "time":          t.time,
        "confidence":    t.confidence,
        "pnl":           t.pnl,
        "optionPremium": t.option_premium,
    }
