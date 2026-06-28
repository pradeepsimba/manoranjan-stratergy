from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.
Subscribes to every watchlist symbol at 5m and 1h, plus NIFTY 50 at 5m,
and updates AppState candle stores in real time via per-token locks.
"""

import asyncio
import json
import re
from typing import TYPE_CHECKING, Dict, Optional

import websockets
import websockets.exceptions

import app.config as cfg
from app.models import Candle
from app.state import get_state

if TYPE_CHECKING:
    pass

_LTP_PAT = re.compile(r"LTP\s*([\d.]+)")

_MAX_CANDLES = 300   # per symbol per interval in memory


class MarketDataService:
    def __init__(self) -> None:
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

    # ── WebSocket connection loop ─────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._run_ws()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.state.ws_status = f"WS Error: {e}"
                print(f"WS error: {e}")
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

            filters = self._build_filters()
            await ws.send(json.dumps({
                "type":       "LIVE_FEED_INIT",
                "filters":    filters,
                "latestOnly": True,
            }))
            print(f"WS subscribed: {len(filters)} symbol-interval pairs")

            async for message in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(message)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        self._process_tick(item)
                except Exception as e:
                    print(f"Tick parse error: {e}")

        self.state.ws_status = "WS Disconnected"

    # ── Subscription filter builder ───────────────────────────────────────────

    def _build_filters(self):
        """
        Subscribe to every symbol in the active watchlist at 5m, 1h.
        Always include NIFTY 50 at 5m and 1d for the trend gate.
        """
        st       = get_state()
        watchlist = st.active_watchlist    # {symbol: token}
        intervals = ["5m", "1h"]

        filters = [
            {"stock_symbol": token, "stockname": sym, "interval": iv}
            for sym, token in watchlist.items()
            for iv in intervals
        ]

        # NIFTY 50 for index correlation filter
        filters.append({
            "stock_symbol": cfg.NIFTY50_TOKEN,
            "stockname":    cfg.NIFTY50_NAME,
            "interval":     "5m",
        })
        return filters

    # ── Tick processing ───────────────────────────────────────────────────────

    def _process_tick(self, n: dict) -> None:
        symbol    = n.get("stock_symbol", "")
        stockname = n.get("stockname",    "")
        interval  = n.get("interval",     "")
        if not symbol or not interval:
            return

        candle = Candle(
            start_time=n.get("start_time", ""),
            open=float(n.get("open",   0)),
            close=float(n.get("close", 0)),
            high=float(n.get("high",   0)),
            low=float(n.get("low",     0)),
            volume=float(n.get("volume", 0)),
        )

        # Parse LTP
        ltp = 0.0
        if "ltp" in n:
            ltp_raw = str(n["ltp"])
            m = _LTP_PAT.search(ltp_raw)
            try:
                ltp = float(m.group(1)) if m else float(ltp_raw)
            except (ValueError, AttributeError):
                pass

        # Per-token lock for regular stocks; separate nifty lock for the shared
        # NIFTY candle lists so scan workers never contend across unrelated tokens.
        if symbol == cfg.NIFTY50_TOKEN:
            with self.state._nifty_lock:
                if interval == "5m":
                    self._upsert_list(self.state.nifty_candles_5m, candle)
                elif interval == "1d":
                    self._upsert_list(self.state.nifty_candles_1d, candle)
        else:
            with self.state.candle_lock(symbol):
                if interval == "5m":
                    self._upsert(self.state.candles_5m, symbol, candle)
                elif interval == "1h":
                    self._upsert(self.state.candles_1h, symbol, candle)
                elif interval == "1d":
                    self._upsert(self.state.candles_1d, symbol, candle)

        if ltp > 0:
            if symbol == cfg.NIFTY50_TOKEN:
                self.state.nifty_ltp = ltp
            elif stockname:
                self.state.ltp[stockname] = ltp

    # ── Candle upsert helpers ─────────────────────────────────────────────────

    @staticmethod
    def _upsert(store: Dict[str, list], symbol: str, candle: Candle) -> None:
        lst = store.setdefault(symbol, [])
        if lst and lst[-1].start_time == candle.start_time:
            lst[-1] = candle          # update in-progress bar
        else:
            lst.append(candle)
            if len(lst) > _MAX_CANDLES:
                lst.pop(0)

    @staticmethod
    def _upsert_list(lst: list, candle: Candle) -> None:
        if lst and lst[-1].start_time == candle.start_time:
            lst[-1] = candle
        else:
            lst.append(candle)
            if len(lst) > _MAX_CANDLES:
                lst.pop(0)
