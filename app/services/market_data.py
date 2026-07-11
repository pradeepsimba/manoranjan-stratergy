from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

Multiple concurrent WS connections share the same tick processor so every
stock gets per-tick indicator updates without exceeding the server's ~32 KB
per-connection output buffer (which closes with 1009 when too many symbols
are subscribed at once). EVERY group is chunked to ≤_MAX_SYMBOLS_PER_WS
entries per connection — active_watchlist is not bounded by that limit
(GEMINI_MAX_STOCKS may exceed it, and manual adds / restored positions grow
it further), so the primaries must chunk exactly like the secondaries:

  primary-5m-N  — active_watchlist at 5m (+ NIFTY 5m in the last chunk)
  primary-1h-N  — active_watchlist at 1h only
  secondary-N   — non-Gemini full_watchlist stocks at 5m (display only)

Reconnect gap-fill: on every REconnect of a 5m connection the day's bars are
re-fetched over REST and merged through the chronological upsert, so an
outage doesn't leave a silent splice in the candle series (TA-Lib would treat
it as contiguous). 1h is NOT backfilled — the REST server's hourly id ("60m")
may not align with the WS "1h" bars and the hourly gate only reads the last
bar anyway.
"""

import asyncio
import json
import re
from collections import deque as _deque
from typing import Callable, Dict, List, Optional

import websockets
import websockets.exceptions

import app.config as cfg
from app.models import Candle, TradingPhase
from app.services.historical_data import fetch_today_candles
from app.state import get_state

_LTP_PAT     = re.compile(r"LTP\s*([\d.]+)")
_BUYQTY_PAT  = re.compile(r"BuyQty\s+(\d+)\s+SellQty\s+(\d+)")
_BID1_PAT    = re.compile(r"Bids[^:]*:.*?(?<!\d)1\)\s+([\d.]+)\s+x\s+(\d+)", re.DOTALL)
_ASK1_PAT    = re.compile(r"Asks[^:]*:.*?(?<!\d)1\)\s+([\d.]+)\s+x\s+(\d+)", re.DOTALL)

_MAX_CANDLES = 300   # per symbol per interval in memory
_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer
_MAX_SYMBOLS_PER_WS = 40   # server's per-connection output buffer supports ~40 subscriptions


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
        out["spread"] = max(0.0, round(out["ask"] - out["bid"], 2))
    return out


class MarketDataService:
    def __init__(self) -> None:
        self._running   = False
        self._tasks:    List[asyncio.Task] = []   # all connection loops
        self.state      = get_state()
        # Per-connection health, label → "connected" | "disconnected" | "error: …".
        # ws_status is DERIVED from this (primary-5m-* first) — a single string
        # written by whichever of N connections last changed would let one
        # flapping display-only secondary mask a dead tradeable feed, and
        # vice versa.
        self._conn_status: Dict[str, str] = {}
        # Labels that have connected at least once THIS PROCESS — a later
        # connect for such a label is a REconnect and triggers the 5m backfill.
        # Deliberately not cleared on stop()/restart(): the watchlist-edit
        # restart is itself an outage that can cross a bar boundary.
        self._seen_labels: set = set()

    def start(self) -> None:
        self._running = True
        st = get_state()
        self._conn_status.clear()

        # primary-5m-N: active_watchlist at 5m + NIFTY (last chunk). Chunked
        # like everything else — GEMINI_MAX_STOCKS / manual adds / restored
        # positions can push the active list past one connection's buffer,
        # and this is the feed SL/target exits depend on.
        n_5m = max(1, -(-(len(st.active_watchlist) + 1) // _MAX_SYMBOLS_PER_WS))
        tasks = [
            asyncio.create_task(
                self._connect_loop(lambda i=i: self._build_filters_5m_chunk(i),
                                   f"primary-5m-{i}")
            )
            for i in range(n_5m)
        ]
        # primary-1h-N: active_watchlist at 1h only (hourly trend gate)
        n_1h = max(1, -(-len(st.active_watchlist) // _MAX_SYMBOLS_PER_WS))
        tasks += [
            asyncio.create_task(
                self._connect_loop(lambda i=i: self._build_filters_1h_chunk(i),
                                   f"primary-1h-{i}")
            )
            for i in range(n_1h)
        ]
        # secondary-N: non-Gemini stocks (5m only for indicator display)
        extra_count = max(0, len(st.full_watchlist) - len(st.active_watchlist))
        n_sec       = max(1, -(-extra_count // _MAX_SYMBOLS_PER_WS))  # ceil div
        tasks += [
            asyncio.create_task(
                self._connect_loop(
                    lambda i=i: self._build_filters_secondary_chunk(i),
                    f"secondary-{i}",
                )
            )
            for i in range(n_sec)
        ]
        self._tasks = tasks

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._conn_status.clear()
        # Cancellation skips _run_ws's post-loop status update — set it here so
        # the dashboard doesn't show "WS Connected" after the EOD shutdown.
        self.state.ws_status = "WS Stopped"

    async def restart(self) -> None:
        """
        Tear down and reopen all connections so the subscription filters
        (and secondary chunk count) are rebuilt from the CURRENT watchlists —
        used after runtime watchlist add/remove. No-op unless already running.
        """
        if not self._running:
            return
        await self.stop()
        # stop() advertises "WS Stopped" — overwrite so the dashboard doesn't
        # flash a scary status during an intentional resubscribe.
        self.state.ws_status = "WS Resubscribing…"
        self.start()

    # ── WebSocket connection loops ─────────────────────────────────────────────

    def _note_conn(self, label: str, status: str) -> None:
        """
        Record one connection's health and re-derive the dashboard ws_status.
        The tradeable feed (primary-5m-*) decides the headline — exits stop
        working without it — with degraded aux connections noted as a suffix.
        """
        self._conn_status[label] = status
        primaries = {l: s for l, s in self._conn_status.items()
                     if l.startswith("primary-5m")}
        bad_p = {l: s for l, s in primaries.items() if s != "connected"}
        if bad_p:
            l, s = next(iter(bad_p.items()))
            derived = f"WS {s} ({l})"
        elif primaries:
            aux_down = sum(1 for l, s in self._conn_status.items()
                           if not l.startswith("primary-5m") and s != "connected")
            derived = f"WS Connected ({aux_down} aux down)" if aux_down else "WS Connected"
        else:
            derived = "WS Connecting…"
        self.state.ws_status = derived

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
                self._note_conn(label, f"error: {e}")
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
            await ws.send(json.dumps({
                "type":       "LIVE_FEED_INIT",
                "filters":    filters,
                "latestOnly": True,
            }))
            print(f"WS [{label}] subscribed: {len(filters)} symbol-interval pairs")

            # Reconnect (or restart) — bars may have completed during the
            # outage, and latestOnly resumes at the newest one. Merge today's
            # REST bars through the chronological upsert BEFORE consuming live
            # ticks (which are buffered on the socket meanwhile), or the series
            # keeps a silent splice that TA-Lib would treat as contiguous.
            if label in self._seen_labels:
                await self._backfill_5m(filters, label)
            self._seen_labels.add(label)
            self._note_conn(label, "connected")

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

        self._note_conn(label, "disconnected")

    async def _backfill_5m(self, filters: List[dict], label: str) -> None:
        """
        Re-fetch today's 5m bars for this connection's symbols and merge them
        through the same chronological upsert the live feed uses (idempotent:
        equal start_time replaces, older is dropped). 1h filters are skipped —
        the REST server's hourly id ("60m") may not align with WS "1h" bars
        and must not mix into the same store.
        """
        wl = {f["stockname"]: f["stock_symbol"]
              for f in filters if f.get("interval") == "5m"}
        if not wl:
            return
        try:
            data = await fetch_today_candles(wl, [cfg.INTERVAL_5M])
        except Exception as e:
            print(f"WS [{label}] reconnect backfill failed: {e}")
            return
        st = self.state
        filled = 0
        for token, per_iv in data.items():
            candles = per_iv.get(cfg.INTERVAL_5M) or []
            if not candles:
                continue
            filled += 1
            if token == cfg.NIFTY50_TOKEN:
                with st._nifty_lock:
                    for c in candles:
                        self._upsert_list(st.nifty_candles_5m, c)
            else:
                with st.candle_lock(token):
                    for c in candles:
                        self._upsert(st.candles_5m, token, c)
        print(f"WS [{label}] reconnect backfill: merged {filled} symbols")

    # ── Subscription filter builders ──────────────────────────────────────────

    def _build_filters_5m_chunk(self, chunk_index: int) -> List[dict]:
        """
        One ≤_MAX_SYMBOLS_PER_WS slice of (active_watchlist at 5m + NIFTY).
        NIFTY rides at the END of the combined list, so it lands in the last
        chunk. The active list is NOT bounded by the per-connection limit
        (GEMINI_MAX_STOCKS may exceed it; manual adds and restored positions
        grow it further) — an unchunked subscription would overflow the
        server's output buffer, 1009-close in a loop, and silently stop every
        SL/target exit.
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
        start = chunk_index * _MAX_SYMBOLS_PER_WS
        return filters[start:start + _MAX_SYMBOLS_PER_WS]

    def _build_filters_1h_chunk(self, chunk_index: int) -> List[dict]:
        """
        One ≤_MAX_SYMBOLS_PER_WS slice of active_watchlist at 1h — needed for
        the hourly trend gate. Separate connections so the combined 5m+1h
        subscription doesn't overflow the server buffer.
        """
        st      = get_state()
        filters = [
            {"stock_symbol": token, "stockname": sym, "interval": "1h"}
            for sym, token in st.active_watchlist.items()
        ]
        start = chunk_index * _MAX_SYMBOLS_PER_WS
        return filters[start:start + _MAX_SYMBOLS_PER_WS]

    def _build_filters_secondary_chunk(self, chunk_index: int) -> List[dict]:
        """
        One slice (≤_MAX_SYMBOLS_PER_WS entries) of the non-Gemini stocks at
        5m only — gives the indicators page per-tick updates for every
        high-volume stock without needing 1h data. Split across multiple
        connections (one per chunk_index) since full_watchlist is uncapped
        and a single connection would overflow the server's output buffer.
        Returns [] when full_watchlist is not yet populated (before premarket)
        or this chunk_index has nothing left to subscribe to.
        """
        st     = get_state()
        active = set(st.active_watchlist.values())   # set of tokens
        extra  = [
            (sym, token)
            for sym, token in st.full_watchlist.items()
            if token not in active
        ]
        start = chunk_index * _MAX_SYMBOLS_PER_WS
        chunk = extra[start:start + _MAX_SYMBOLS_PER_WS]
        return [
            {"stock_symbol": token, "stockname": sym, "interval": "5m"}
            for sym, token in chunk
        ]

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
        else:
            with self.state.candle_lock(symbol):
                if interval == "5m":
                    self._upsert(self.state.candles_5m, symbol, candle)
                    # Bumped under the SAME lock, right after the mutation, so
                    # any reader observing the new version is guaranteed to
                    # also see the updated candle list (see AppState.tick_version).
                    self.state.tick_version[symbol] = self.state.tick_version.get(symbol, 0) + 1
                elif interval == "1h":
                    self._upsert(self.state.candles_1h, symbol, candle)

        # Dashboard "last bar" clock — only for accepted STOCK 5m bars, and never
        # move it backwards (a reconnect-replayed stale bar is dropped by _upsert
        # but must not rewind this display; NIFTY's own cadence isn't "the" bar).
        if interval == "5m" and symbol != cfg.NIFTY50_TOKEN:
            bt = candle.start_time[11:16]
            if self.state.last_5m_bar_time is None or bt >= self.state.last_5m_bar_time:
                self.state.last_5m_bar_time = bt

        if ltp > 0:
            if symbol == cfg.NIFTY50_TOKEN:
                self.state.nifty_ltp = ltp
            elif stockname:
                self.state.ltp[stockname] = ltp

        # Parse order-book depth from the snap field (stocks only; NIFTY has no
        # real order book — its snap shows -0.01 sentinels which _parse_depth
        # discards via the bid_p > 0 guard).
        snap = n.get("snap", "")
        if snap and stockname and symbol != cfg.NIFTY50_TOKEN and interval == "5m":
            depth = _parse_depth(snap)
            if depth:
                # MERGE, don't replace: a partial snap (e.g. bid/ask present but
                # no BuyQty/SellQty line) would otherwise drop the last-known
                # `ratio`, silently turning depth_bullish into an auto-pass and
                # letting a sell-skewed book through. New dict = atomic swap for
                # lock-free readers (same GIL-safe pattern as ltp).
                prev = self.state.depth.get(stockname)
                self.state.depth[stockname] = {**prev, **depth} if prev else depth

        # Tick-wise engine: flag this stock for re-evaluation on the next loop
        # cycle. Only 5m ticks update the forming bar; 1h ticks must not enqueue
        # dirty_ticks or they trigger scans on stale 5m bars. Only while ACTIVE.
        if interval == "5m" and symbol != cfg.NIFTY50_TOKEN and self.state.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF):
            self.state.dirty_ticks.add(symbol)
            self.state.dirty_ticks_push.add(symbol)

    # ── Candle upsert helpers ─────────────────────────────────────────────────

    @staticmethod
    def _upsert(store: Dict[str, list], symbol: str, candle: Candle) -> None:
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
