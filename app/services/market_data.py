from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

Fixed universe: BankNifty index + its 11 stocks, plus Nifty 50 index + its
32 stocks, deduped on the 6 stocks both strategies share (see
_build_filters) = 39 symbol-interval pairs. Still a SINGLE WS connection,
but now close to the server's documented ~40-entries-per-connection output
buffer limit — if that limit is a hard cap (not just a rough historical
observation), adding any further instrument would need a second connection,
mirroring the deleted equity engine's split primary/secondary approach.
"""

import asyncio
import json
import re
from collections import deque as _deque
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import websockets
import websockets.exceptions

import app.config as cfg
from app.models import Candle, TradingPhase
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

_LTP_PAT = re.compile(r"LTP\s*([\d.]+)")
_QTY_PAT = re.compile(r"qty\s+(\d+)", re.IGNORECASE)
_BUY_QTY_PAT = re.compile(r"BuyQty\s+(\d+)")
_SELL_QTY_PAT = re.compile(r"SellQty\s+(\d+)")

_MAX_CANDLES = 300   # per symbol per interval in memory
_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer


class MarketDataService:
    def __init__(self) -> None:
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.state = get_state()
        # stock_symbol (token) -> this app's own internal ALL-CAPS display
        # name. The vendor's per-tick echoed `stockname` field does NOT
        # reliably match the casing we sent in the subscription request (e.g.
        # it echoes "HDFC Bank" even though we subscribed with "HDFC BANK"),
        # so `ltp` must be keyed off this reverse map, never off the raw
        # echoed `stockname` text directly. Merged across BOTH instruments —
        # the 6 stocks BN and NF share resolve to the same name either way.
        self._token_to_name = {
            **{token: name for name, token in cfg.BN_ALL_STOCKS.items()},
            **{token: name for name, token in cfg.NF_ALL_STOCKS.items()},
        }

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
                print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS connecting…")
                await self._run_ws(self._build_filters())
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.state.ws_status = f"WS Error: {e}"
                print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS error: {e}")
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
            print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS connected")

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
        print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS disconnected "
              f"(loop ended — either the server closed the connection or shutdown was requested)")

    # ── Subscription filter builder ────────────────────────────────────────────

    def _build_filters(self) -> list:
        """
        BankNifty index + its 11 stocks, and Nifty 50 index + its 32 stocks,
        all at 5m. Stock filters are deduped by stock_symbol (6 stocks are
        shared between the two universes — HDFCBANK, ICICIBANK, AXISBANK,
        SBIN, KOTAKBANK, INDUSINDBK — each must be subscribed exactly once).
        """
        filters = [
            {"stock_symbol": cfg.BN_INDEX_TOKEN, "stockname": cfg.BN_INDEX_NAME, "interval": "5m"},
            {"stock_symbol": cfg.NF_INDEX_TOKEN, "stockname": cfg.NF_INDEX_NAME, "interval": "5m"},
        ]
        stock_by_symbol = {}
        for sym, token in cfg.BN_ALL_STOCKS.items():
            stock_by_symbol[token] = sym
        for sym, token in cfg.NF_ALL_STOCKS.items():
            stock_by_symbol.setdefault(token, sym)
        filters += [
            {"stock_symbol": token, "stockname": sym, "interval": "5m"}
            for token, sym in stock_by_symbol.items()
        ]
        return filters

    # ── Tick processing ───────────────────────────────────────────────────────

    def _process_tick(self, n: dict) -> None:
        symbol    = n.get("stock_symbol", "")
        interval  = n.get("interval",     "")
        if not symbol or interval != "5m":
            return

        # Real per-trade quantity, embedded as "...qty N..." inside the
        # feed's `quote` text field (confirmed against the live server,
        # 2026-07-23) — historical REST bars never carry this, only live
        # WS ticks do, so it's 0 unless present on this specific tick.
        last_qty = 0.0
        quote_raw = n.get("quote")
        if quote_raw:
            m = _QTY_PAT.search(str(quote_raw))
            if m:
                try:
                    last_qty = float(m.group(1))
                except ValueError:
                    pass

        # Cumulative pending buy/sell order quantity, embedded in the feed's
        # `snap` text field (e.g. "...BuyQty 1111915 SellQty 1944411...") —
        # confirmed present on the live server, same WS-only availability as
        # last_qty above (historical REST bars never carry it).
        buy_qty = sell_qty = 0.0
        snap_raw = n.get("snap")
        if snap_raw:
            snap_str = str(snap_raw)
            mb = _BUY_QTY_PAT.search(snap_str)
            ms = _SELL_QTY_PAT.search(snap_str)
            if mb:
                try:
                    buy_qty = float(mb.group(1))
                except ValueError:
                    pass
            if ms:
                try:
                    sell_qty = float(ms.group(1))
                except ValueError:
                    pass

        candle = Candle(
            start_time=n.get("start_time", ""),
            open=float(n.get("open",   0)),
            close=float(n.get("close", 0)),
            high=float(n.get("high",   0)),
            low=float(n.get("low",     0)),
            volume=float(n.get("volume", 0)),
            last_qty=last_qty,
            buy_qty=buy_qty,
            sell_qty=sell_qty,
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
            # A genuine index tick arrived — the vendor may have resumed
            # streaming it (currently doesn't). One-way latch: once real
            # data is seen, never fall back to synthesizing again this run.
            self.state.bn_index_synthetic = False
            with self.state._bn_index_lock:
                self._upsert_list(self.state.bn_index_candles_5m, candle)
        elif symbol == cfg.NF_INDEX_TOKEN:
            self.state.nf_index_synthetic = False
            with self.state._nf_index_lock:
                self._upsert_list(self.state.nf_index_candles_5m, candle)
        else:
            with self.state.candle_lock(symbol):
                self._upsert(self.state.candles_5m, symbol, candle)
                # Bumped under the SAME lock, right after the mutation, so any
                # reader observing the new version also sees the updated candle list.
                self.state.tick_version[symbol] = self.state.tick_version.get(symbol, 0) + 1
            if self.state.bn_index_synthetic:
                self._update_synthetic_index()
            if self.state.nf_index_synthetic:
                self._update_synthetic_nf_index()

        if ltp > 0:
            if symbol == cfg.BN_INDEX_TOKEN:
                self.state.bn_index_ltp = ltp
            elif symbol == cfg.NF_INDEX_TOKEN:
                self.state.nf_index_ltp = ltp
            else:
                name = self._token_to_name.get(symbol)
                if name:
                    self.state.ltp[name] = ltp

        # Live-price ticker push — every 5m tick (index or stock) refreshes the
        # dashboard delta; the BN engine's entry/exit evaluation runs on its own
        # tick-wise loop timer, not off a per-tick dirty flag.
        if self.state.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF):
            self.state.dirty_ticks_push.add(symbol)

    # ── Synthetic BankNifty index (vendor stopped streaming the real index
    # under either the old or new protocol — confirmed empirically: both the
    # live WS and historical REST return nothing for it) ───────────────────

    def _update_synthetic_index(self) -> None:
        """
        Port of c1.html's updateSyntheticIndexCandle: approximate the
        BankNifty index candle from the 11 constituent stocks' current
        forming-bar % change, weighted by cfg.BN_INDEX_WEIGHTS. Recomputed
        after every constituent stock tick so it stays as fresh as the real
        index tick path would have been.
        """
        weighted_pct = 0.0
        total_weight = 0.0
        latest_time: Optional[str] = None

        for symbol, weight in cfg.BN_INDEX_WEIGHTS.items():
            with self.state.candle_lock(symbol):
                candles = self.state.candles_5m.get(symbol)
                candle = candles[-1] if candles else None
            if not candle or not candle.open or not candle.close:
                continue
            pct = (candle.close - candle.open) / candle.open * 100.0
            weighted_pct += pct * (weight / 100.0)
            total_weight += weight
            if candle.start_time and (latest_time is None or candle.start_time > latest_time):
                latest_time = candle.start_time

        if total_weight == 0 or latest_time is None:
            return   # no constituent candles yet either

        with self.state._bn_index_lock:
            idx_candles = self.state.bn_index_candles_5m
            prev = idx_candles[-1] if idx_candles else None
            is_new_bar = prev is None or prev.start_time != latest_time
            if is_new_bar and prev is not None:
                self._warn_if_gap("BankNifty", prev.start_time, latest_time)

            # Anchor the new bar's open to the PREVIOUS bar's close — only at
            # the moment it actually rolls over, so weighted_pct (a full-bar %
            # change) applies once per tick against a fixed base instead of
            # compounding every tick within the same bar.
            if is_new_bar and prev is not None:
                self.state.bn_synthetic_anchor = prev.close
            anchor = self.state.bn_synthetic_anchor
            if anchor <= 0:
                return   # no seed yet (startup seeding hasn't run / archive empty)

            open_ = anchor if is_new_bar else (prev.open if prev else anchor)
            close_ = open_ * (1 + weighted_pct / 100.0)
            synthetic = Candle(start_time=latest_time, open=open_, close=close_,
                               high=max(open_, close_), low=min(open_, close_))
            self._upsert_list(idx_candles, synthetic)

        self.state.bn_index_ltp = close_

    def _update_synthetic_nf_index(self) -> None:
        """NF mirror of _update_synthetic_index — same anchor-and-weighted-% logic, cfg.NF_*."""
        weighted_pct = 0.0
        total_weight = 0.0
        latest_time: Optional[str] = None

        for symbol, weight in cfg.NF_INDEX_WEIGHTS.items():
            with self.state.candle_lock(symbol):
                candles = self.state.candles_5m.get(symbol)
                candle = candles[-1] if candles else None
            if not candle or not candle.open or not candle.close:
                continue
            pct = (candle.close - candle.open) / candle.open * 100.0
            weighted_pct += pct * (weight / 100.0)
            total_weight += weight
            if candle.start_time and (latest_time is None or candle.start_time > latest_time):
                latest_time = candle.start_time

        if total_weight == 0 or latest_time is None:
            return

        with self.state._nf_index_lock:
            idx_candles = self.state.nf_index_candles_5m
            prev = idx_candles[-1] if idx_candles else None
            is_new_bar = prev is None or prev.start_time != latest_time
            if is_new_bar and prev is not None:
                self._warn_if_gap("Nifty 50", prev.start_time, latest_time)

            if is_new_bar and prev is not None:
                self.state.nf_synthetic_anchor = prev.close
            anchor = self.state.nf_synthetic_anchor
            if anchor <= 0:
                return

            open_ = anchor if is_new_bar else (prev.open if prev else anchor)
            close_ = open_ * (1 + weighted_pct / 100.0)
            synthetic = Candle(start_time=latest_time, open=open_, close=close_,
                               high=max(open_, close_), low=min(open_, close_))
            self._upsert_list(idx_candles, synthetic)

        self.state.nf_index_ltp = close_

    @staticmethod
    def _warn_if_gap(label: str, prev_start: str, new_start: str) -> None:
        """
        Diagnostic only — the synthetic index (BN or NF) advances reactively
        off whichever constituent tick has the latest timestamp; it has no
        backfill, so any WS interruption longer than one 5m bar leaves a
        silent gap (the next real tick just picks up wherever "now" is).
        Logs it so a gap is diagnosable from server logs rather than only
        noticeable as an odd-looking jump in the Stock Candles table.
        """
        try:
            prev_dt = datetime.fromisoformat(prev_start)
            new_dt = datetime.fromisoformat(new_start)
        except ValueError:
            return
        gap_minutes = (new_dt - prev_dt).total_seconds() / 60.0
        if gap_minutes > 10:   # more than one missed 5m bar
            print(f"Synthetic {label} index gap: {prev_start} -> {new_start} "
                  f"({gap_minutes:.0f} min) — likely a WS interruption in between")

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
    def _upsert_list(lst: "_deque[Candle]", candle: Candle) -> None:
        """lst is a deque(maxlen=...) (state.bn_index_candles_5m/nf_index_candles_5m)
        — append relies on maxlen for O(1) eviction, matching _upsert above."""
        if not lst:
            lst.append(candle)
            return
        last = lst[-1].start_time
        if last == candle.start_time:
            lst[-1] = candle
        elif candle.start_time > last:
            lst.append(candle)   # deque(maxlen) auto-evicts from left — O(1)
        # else: stale out-of-order bar (e.g. reconnect replay) — drop it.
