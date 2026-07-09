from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

Fixed, small universe (BankNifty index + 11 stocks = 12 symbol-interval
pairs, well under the server's ~40-entries-per-connection output buffer), so
a SINGLE WS connection covers everything — no need for the equity engine's
split primary-5m/primary-1h/secondary connections (BN never uses 1h data or
a "non-Gemini" secondary universe).
"""

import asyncio
import json
import re
from collections import deque as _deque
from typing import Optional

import websockets
import websockets.exceptions

import app.config as cfg
from app.models import Candle, TradingPhase
from app.state import get_state

_LTP_PAT = re.compile(r"LTP\s*([\d.]+)")

_MAX_CANDLES = 300   # per symbol per interval in memory
_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer


class MarketDataService:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.state = get_state()

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        # Cancellation skips _run_ws's post-loop status update — set it here so
        # the dashboard doesn't show "WS Connected" after the EOD shutdown.
        self.state.ws_status = "WS Stopped"

    async def restart(self) -> None:
        if not self._running:
            return
        await self.stop()
        self.state.ws_status = "WS Resubscribing…"
        self.start()

    # ── WebSocket connection loop ───────────────────────────────────────────────

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                await self._run_ws(self._build_filters())
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.state.ws_status = f"WS Error: {e}"
                print(f"WS error: {e}")
            if self._running:
                await asyncio.sleep(5)

    async def _run_ws(self, filters: list) -> None:
        async with websockets.connect(
            cfg.WS_URL,
            ping_interval=20,
            ping_timeout=30,
            open_timeout=15,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            self.state.ws_status = "WS Connected"

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
                    data  = json.loads(message)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        self._process_tick(item)
                except Exception as e:
                    print(f"Tick parse error: {e}")

        self.state.ws_status = "WS Disconnected"

    # ── Subscription filter builder ────────────────────────────────────────────

    def _build_filters(self) -> list:
        """BankNifty index + the 11 BN stocks, all at 5m — the fixed strategy universe."""
        filters = [{
            "stock_symbol": cfg.BN_INDEX_TOKEN,
            "stockname":    cfg.BN_INDEX_NAME,
            "interval":     "5m",
        }]
        filters += [
            {"stock_symbol": token, "stockname": sym, "interval": "5m"}
            for sym, token in cfg.BN_ALL_STOCKS.items()
        ]
        return filters

    # ── Tick processing ───────────────────────────────────────────────────────

    def _process_tick(self, n: dict) -> None:
        symbol    = n.get("stock_symbol", "")
        stockname = n.get("stockname",    "")
        interval  = n.get("interval",     "")
        if not symbol or interval != "5m":
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

        if symbol == cfg.BN_INDEX_TOKEN:
            with self.state._bn_index_lock:
                self._upsert_list(self.state.bn_index_candles_5m, candle)
        else:
            with self.state.candle_lock(symbol):
                self._upsert(self.state.candles_5m, symbol, candle)
                # Bumped under the SAME lock, right after the mutation, so any
                # reader observing the new version also sees the updated candle list.
                self.state.tick_version[symbol] = self.state.tick_version.get(symbol, 0) + 1

        if ltp > 0:
            if symbol == cfg.BN_INDEX_TOKEN:
                self.state.bn_index_ltp = ltp
            elif stockname:
                self.state.ltp[stockname] = ltp

        # Live-price ticker push — every 5m tick (index or stock) refreshes the
        # dashboard delta; the BN engine's entry/exit evaluation runs on its own
        # tick-wise loop timer, not off a per-tick dirty flag.
        if self.state.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF):
            self.state.dirty_ticks_push.add(symbol)

    # ── Candle upsert helpers ─────────────────────────────────────────────────

    @staticmethod
    def _upsert(store: dict, symbol: str, candle: Candle) -> None:
        lst = store.get(symbol)
        if lst is None:
            store[symbol] = _deque([candle], maxlen=_MAX_CANDLES)
            return
        last = lst[-1].start_time
        if last == candle.start_time:
            lst[-1] = candle          # update in-progress bar
        elif candle.start_time > last:   # ISO strings — lexicographic == chronological
            lst.append(candle)        # deque(maxlen) auto-evicts from left — O(1)
        # else: stale out-of-order bar (e.g. reconnect replay) — appending it
        # would break the chronological order every scan relies on; drop it.

    @staticmethod
    def _upsert_list(lst: list, candle: Candle) -> None:
        if not lst:
            lst.append(candle)
            return
        last = lst[-1].start_time
        if last == candle.start_time:
            lst[-1] = candle
        elif candle.start_time > last:
            lst.append(candle)
            if len(lst) > _MAX_CANDLES:
                lst.pop(0)
