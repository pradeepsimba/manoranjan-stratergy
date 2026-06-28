from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Optional

import websockets
import websockets.exceptions

import app.config as cfg
from app.models import Candle
from app.state import get_state

if TYPE_CHECKING:
    from app.services.database import DatabaseService

_LTP_PAT  = re.compile(r"LTP\s*([\d.]+)")
_BUY_PAT  = re.compile(r"BuyQty (\d+)")
_SELL_PAT = re.compile(r"SellQty (\d+)")


class TickFeedService:
    def __init__(self, db: "DatabaseService") -> None:
        self._db      = db
        self._running = False
        self._task:   Optional[asyncio.Task] = None
        self.state    = get_state()

    def start(self) -> None:
        self._running = True
        self._task    = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── WebSocket connection ──────────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._run_ws()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.state.ws_status = f"WS Error: {e}"
            if self._running:
                await asyncio.sleep(5)

    async def _run_ws(self) -> None:
        async with websockets.connect(
            cfg.WS_URL,
            ping_interval=20,
            ping_timeout=30,
            open_timeout=15,
        ) as ws:
            self.state.ws_status = "WS Connected"

            # Send init subscription message
            filters = [
                {"stock_symbol": s.symbol, "stockname": s.name, "interval": iv}
                for s in cfg.STOCKS
                for iv in ("1m", "5m", "15m")
            ]
            await ws.send(json.dumps({
                "type":       "LIVE_FEED_INIT",
                "filters":    filters,
                "latestOnly": True,
            }))

            async for message in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(message)
                    if isinstance(data, list):
                        for item in data:
                            await self._process_tick(item)
                    else:
                        await self._process_tick(data)
                except Exception as e:
                    print(f"Tick parse error: {e}")

        self.state.ws_status = "WS Disconnected"

    # ── Tick processing ───────────────────────────────────────────────────────

    async def _process_tick(self, n: dict) -> None:
        symbol     = n.get("stock_symbol", "")
        stockname  = n.get("stockname",    "")
        interval   = n.get("interval",     "")
        start_time = n.get("start_time",   "")
        open_      = float(n.get("open",   0))
        close      = float(n.get("close",  0))
        high       = float(n.get("high",   0))
        low        = float(n.get("low",    0))
        volume     = float(n.get("volume", 0))

        ltp = 0.0
        if "ltp" in n:
            ltp_str = str(n["ltp"])
            m = _LTP_PAT.search(ltp_str)
            try:
                ltp = float(m.group(1)) if m else float(ltp_str)
            except (ValueError, AttributeError):
                pass

        buy_qty = sell_qty = 0
        if "snap" in n:
            snap = str(n["snap"])
            bm = _BUY_PAT.search(snap)
            sm = _SELL_PAT.search(snap)
            if bm: buy_qty  = int(bm.group(1))
            if sm: sell_qty = int(sm.group(1))

        candle = Candle(start_time=start_time, open=open_, close=close,
                        high=high, low=low, volume=volume)

        if interval and symbol:
            self._update_all_interval_candles(symbol, interval, candle)

        if interval != self.state.selected_interval:
            return

        self._update_last_n_candles(symbol, candle)

        qty = float(buy_qty + sell_qty)
        if stockname:
            self.state.latest_minute_qty[stockname] = qty
            self.state.latest_buy_qty[stockname]    = buy_qty
            self.state.latest_sell_qty[stockname]   = sell_qty

        if symbol == cfg.INDEX_SYMBOL and ltp > 0:
            self.state.bn_ltp = ltp

        if ltp > 0 and stockname:
            self._db.add_stock_record(stockname, start_time, ltp, qty)

        if symbol == cfg.INDEX_SYMBOL:
            await self._on_bn_tick()

    async def _on_bn_tick(self) -> None:
        from app.engine.trade_engine import check_exit, check_trade_entry
        await check_exit(self.state, self._db)
        asyncio.create_task(check_trade_entry(self._db))

    # ── Candle store updates ──────────────────────────────────────────────────

    def _update_all_interval_candles(self, symbol: str, interval: str, candle: Candle) -> None:
        iv_map = self.state.all_interval_candles.setdefault(interval, {})
        lst    = iv_map.setdefault(symbol, [])
        if lst and lst[-1].start_time == candle.start_time:
            lst[-1] = candle
        else:
            lst.append(candle)
            if len(lst) > 5:
                lst.pop(0)

    def _update_last_n_candles(self, symbol: str, candle: Candle) -> None:
        lst = self.state.last_n_candles.setdefault(symbol, [])
        if lst and lst[-1].start_time == candle.start_time:
            lst[-1] = candle
        else:
            lst.append(candle)
            if len(lst) > 200:
                lst.pop(0)

        if symbol == cfg.INDEX_SYMBOL:
            with self.state._bn_ind_lock:
                bn = self.state.bn_indicator_candles
                if bn and bn[-1].start_time == candle.start_time:
                    bn[-1] = candle
                else:
                    bn.append(candle)
                    if len(bn) > 300:
                        bn.pop(0)
