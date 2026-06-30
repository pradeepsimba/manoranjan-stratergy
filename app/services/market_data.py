from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

Three concurrent WS connections share the same tick processor so every stock
gets per-tick indicator updates without exceeding the server's ~32 KB per-
connection output buffer (which closes with 1009 when too many symbols are
subscribed at once):

  primary-5m  — active_watchlist at 5m + NIFTY 5m      (≤~40 entries)
  primary-1h  — active_watchlist at 1h only             (≤~40 entries)
  secondary   — non-Gemini full_watchlist stocks at 5m  (≤~40 entries)
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

_LTP_PAT     = re.compile(r"LTP\s*([\d.]+)")
_BUYQTY_PAT  = re.compile(r"BuyQty\s+(\d+)\s+SellQty\s+(\d+)")
_BID1_PAT    = re.compile(r"Bids[^:]*:.*?1\)\s+([\d.]+)\s+x\s+(\d+)", re.DOTALL)
_ASK1_PAT    = re.compile(r"Asks[^:]*:.*?1\)\s+([\d.]+)\s+x\s+(\d+)", re.DOTALL)

_MAX_CANDLES = 300   # per symbol per interval in memory
_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer


def _parse_depth(snap: str) -> dict:
    """Extract order-book data from the snap string in a WS tick."""
    out: dict = {}
    m = _BUYQTY_PAT.search(snap)
    if m:
        buy_qty  = int(m.group(1))
        sell_qty = int(m.group(2))
        total    = buy_qty + sell_qty
        out["buy_qty"]  = buy_qty
        out["sell_qty"] = sell_qty
        out["ratio"]    = round(buy_qty / total, 3) if total > 0 else 0.5
    m = _BID1_PAT.search(snap)
    if m:
        bid_p = float(m.group(1))
        if bid_p > 0:
            out["bid"] = bid_p
    m = _ASK1_PAT.search(snap)
    if m:
        ask_p = float(m.group(1))
        if ask_p > 0:
            out["ask"] = ask_p
    if "bid" in out and "ask" in out:
        out["spread"] = round(out["ask"] - out["bid"], 2)
    return out


class MarketDataService:
    def __init__(self) -> None:
        self._running   = False
        self._task:     Optional[asyncio.Task] = None   # primary-5m
        self._task_1h:  Optional[asyncio.Task] = None   # primary-1h
        self._task2:    Optional[asyncio.Task] = None   # secondary
        self.state      = get_state()

    def start(self) -> None:
        self._running  = True
        # primary-5m: active_watchlist (5m) + NIFTY (5m)
        self._task    = asyncio.create_task(
            self._connect_loop(self._build_filters_5m, "primary-5m")
        )
        # primary-1h: active_watchlist (1h) — separate connection to stay
        # under the server's per-connection output-buffer limit
        self._task_1h = asyncio.create_task(
            self._connect_loop(self._build_filters_1h, "primary-1h")
        )
        # secondary: non-Gemini stocks (5m only for indicator display)
        self._task2   = asyncio.create_task(
            self._connect_loop(self._build_filters_secondary, "secondary")
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._task_1h, self._task2):
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
            if label == "primary-5m":
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

        if label == "primary-5m":
            self.state.ws_status = "WS Disconnected"

    # ── Subscription filter builders ──────────────────────────────────────────

    def _build_filters_5m(self) -> List[dict]:
        """
        Active_watchlist at 5m + NIFTY 5m.
        Kept as a separate connection from 1h so each stays under the server's
        per-connection output-buffer limit (~32 KB / ~40 ticks).
        """
        st      = get_state()
        filters = [
            {"stock_symbol": token, "stockname": sym, "interval": "5m"}
            for sym, token in st.active_watchlist.items()
        ]
        filters.append({
            "stock_symbol": cfg.NIFTY50_TOKEN,
            "stockname":    cfg.NIFTY50_NAME,
            "interval":     "5m",
        })
        return filters

    def _build_filters_1h(self) -> List[dict]:
        """
        Active_watchlist at 1h only — needed for the hourly trend gate.
        Separate connection so the combined 5m+1h subscription doesn't overflow
        the server buffer.
        """
        st = get_state()
        return [
            {"stock_symbol": token, "stockname": sym, "interval": "1h"}
            for sym, token in st.active_watchlist.items()
        ]

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

        # Parse order-book depth from the snap field (stocks only; NIFTY has no
        # real order book — its snap shows -0.01 sentinels which _parse_depth
        # discards via the bid_p > 0 guard).
        snap = n.get("snap", "")
        if snap and stockname and symbol != cfg.NIFTY50_TOKEN:
            depth = _parse_depth(snap)
            if depth:
                self.state.depth[stockname] = depth

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
