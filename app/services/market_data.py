from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

Two concurrent WS connections share the same tick processor:

  Primary   — active_watchlist (Gemini subset) at 5m + 1h, plus NIFTY 5m.
  Secondary — non-Gemini full_watchlist stocks at 5m only.

Splitting the subscription avoids the server's per-connection output-buffer
limit (which rejects subscriptions for too many stocks at once with 1009).
Both connections feed into the same dirty_ticks set so the tick-wise engine
computes indicators for every stock on every bar update.
"""

import asyncio
import json
import re
from typing import Callable, Dict, List, Optional

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
        self._running  = False
        self._task:    Optional[asyncio.Task] = None
        self._task2:   Optional[asyncio.Task] = None
        self.state     = get_state()

    def start(self) -> None:
        self._running = True
        # Primary: active_watchlist + NIFTY (5m + 1h for trading)
        self._task  = asyncio.create_task(
            self._connect_loop(self._build_filters_primary, "primary")
        )
        # Secondary: non-Gemini stocks (5m only for indicator display)
        self._task2 = asyncio.create_task(
            self._connect_loop(self._build_filters_secondary, "secondary")
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._task2):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ── WebSocket connection loops ─────────────────────────────────────────────

    async def _connect_loop(
        self,
        filter_fn: Callable[[], List[dict]],
        label:     str,
    ) -> None:
        while self._running:
            try:
                filters = filter_fn()
                if not filters:
                    # Nothing to subscribe to yet (e.g. secondary before premarket)
                    await asyncio.sleep(10)
                    continue
                await self._run_ws(filters, label)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.state.ws_status = f"WS Error ({label}): {e}"
                print(f"WS error ({label}): {e}")
            if self._running:
                await asyncio.sleep(5)

    async def _run_ws(self, filters: List[dict], label: str) -> None:
        async with websockets.connect(
            cfg.WS_URL,
            ping_interval=20,
            ping_timeout=30,
            open_timeout=15,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            if label == "primary":
                self.state.ws_status = "WS Connected"

            await ws.send(json.dumps({
                "type":       "LIVE_FEED_INIT",
                "filters":    filters,
                "latestOnly": True,
            }))
            print(f"WS [{label}] subscribed: {len(filters)} symbol-interval pairs")

            async for message in ws:
                if not self._running:
                    break
                try:
                    data  = json.loads(message)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        self._process_tick(item)
                except Exception as e:
                    print(f"Tick parse error ({label}): {e}")

        if label == "primary":
            self.state.ws_status = "WS Disconnected"

    # ── Subscription filter builders ──────────────────────────────────────────

    def _build_filters_primary(self) -> List[dict]:
        """
        Active_watchlist stocks at 5m + 1h (needed for hourly trend gate),
        plus NIFTY 50 at 5m for the index trend filter.
        """
        st        = get_state()
        watchlist = st.active_watchlist
        filters   = [
            {"stock_symbol": token, "stockname": sym, "interval": iv}
            for sym, token in watchlist.items()
            for iv in ("5m", "1h")
        ]
        filters.append({
            "stock_symbol": cfg.NIFTY50_TOKEN,
            "stockname":    cfg.NIFTY50_NAME,
            "interval":     "5m",
        })
        return filters

    def _build_filters_secondary(self) -> List[dict]:
        """
        Non-Gemini stocks at 5m only — gives the indicators page per-tick
        updates for every high-volume stock without needing 1h data.
        Returns [] when full_watchlist is not yet populated (before premarket).
        """
        st     = get_state()
        active = set(st.active_watchlist.values())   # set of tokens
        extra  = [
            {"stock_symbol": token, "stockname": sym, "interval": "5m"}
            for sym, token in st.full_watchlist.items()
            if token not in active
        ]
        return extra

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

        if interval == "5m":
            self.state.last_5m_bar_time = candle.start_time[11:16]

        if ltp > 0:
            if symbol == cfg.NIFTY50_TOKEN:
                self.state.nifty_ltp = ltp
            elif stockname:
                self.state.ltp[stockname] = ltp

        # Tick-wise engine: flag this stock for re-evaluation on the next loop
        # cycle. Only while ACTIVE so the set stays small and bounded.
        if symbol != cfg.NIFTY50_TOKEN and self.state.phase == TradingPhase.ACTIVE:
            self.state.dirty_ticks.add(symbol)

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
