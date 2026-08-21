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
import time
from collections import deque as _deque
from typing import Callable, Dict, List, Optional

import websockets
import websockets.exceptions

import app.config as cfg
from app.engine.orderbook import append_tape, parse_snap
from app.models import Candle, TapeEvent, TradingPhase
from app.services.historical_data import fetch_today_candles
from app.state import get_state, nifty_token

_LTP_PAT     = re.compile(r"LTP\s*([\d.]+)")
_BUYQTY_PAT  = re.compile(r"BuyQty\s+(\d+)\s+SellQty\s+(\d+)")
_BID1_PAT    = re.compile(r"Bids[^:]*:.*?(?<!\d)1\)\s+([\d.]+)\s+x\s+(\d+)", re.DOTALL)
_ASK1_PAT    = re.compile(r"Asks[^:]*:.*?(?<!\d)1\)\s+([\d.]+)\s+x\s+(\d+)", re.DOTALL)

_MAX_CANDLES = 300   # per symbol per interval in memory
_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer
# MUST stay paired with HistoricalDataWebSocketHandler.MAX_FILTER_OBJECTS on the server - that's
# the actual per-session subscription cap this number exists to respect. Raised from 40 to 300 for
# a ~10,000-stock universe: at 40, covering 10,000 stocks needs ~750 concurrent WebSocket
# connections from this one process (a real problem: fd limits, per-connection overhead); at 300
# that drops to ~125. Still well under the whole universe in one connection, so a single
# connection's reconnect gap only ever affects this many symbols' live feed, not all 10,000.
_MAX_SYMBOLS_PER_WS = 300


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
        # symbol → last raw snap string: the 3 order-book regexes are the most
        # expensive part of _process_tick, and the book often doesn't move
        # between pushes — identical snap ⇒ identical parse ⇒ skip it.
        self._last_snap: Dict[str, str] = {}
        # symbol → last (LTQ, price) pair appended to the scalper's tape. LTQ is a
        # LEVEL, not a delta, so this is what stops a quiet tick from re-counting
        # the previous print (see the tape block in _process_tick).
        self._last_ltq: Dict[str, tuple] = {}

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
        # st.depth is cleared at EOD — the snap dedup cache must go with it,
        # or a byte-identical first snap tomorrow would skip the parse and
        # leave that symbol's depth missing until its book changes.
        self._last_snap.clear()
        self._last_ltq.clear()
        # Same reasoning for the tape's volume baseline: bars keep completing
        # during an outage, so the first tick after a reconnect must re-baseline
        # rather than book the whole outage's volume as one giant print.
        self.state.last_bar_volume.clear()
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
        # Every connection is being torn down and rebuilt (see docstring), so
        # every symbol's ltp is about to go stale for the reconnect+resubscribe
        # gap. Without clearing it, _tick_exits() (scheduler.py) keeps reading
        # the LAST price from before the restart and evaluating SL/target
        # against it as if it were live - st.ltp.get(symbol) never returns
        # None just because the feed is briefly down. Clearing it makes
        # _tick_exits' existing `if not ltp: continue` guard skip these
        # symbols until a fresh tick repopulates them post-reconnect, instead
        # of silently acting on minutes-old prices.
        self.state.ltp.clear()
        self.state.nifty_ltp = 0.0
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
            # Own errors list → this background heal never writes api_status:
            # its "API OK" could otherwise mask a concurrent session load's
            # "API partial" (the status the dashboard needs to keep showing).
            bf_errors: list = []
            data = await fetch_today_candles(wl, [cfg.INTERVAL_5M], errors=bf_errors)
            if bf_errors:
                print(f"WS [{label}] backfill fetch errors: {bf_errors[0]}")
        except Exception as e:
            print(f"WS [{label}] reconnect backfill failed: {e}")
            return
        st = self.state
        filled = 0
        for token, per_iv in data.items():
            # Per-token isolation: one bad symbol's merge must not raise out of
            # _run_ws — that would crash the CONNECTION into a reconnect →
            # backfill → crash loop, killing the feed (and exits) for every
            # symbol on this chunk.
            try:
                candles = per_iv.get(cfg.INTERVAL_5M) or []
                if not candles:
                    continue
                filled += 1
                if token == nifty_token():
                    with st._nifty_lock:
                        for c in candles:
                            self._upsert_list(st.nifty_candles_5m, c)
                else:
                    with st.candle_lock(token):
                        for c in candles:
                            self._upsert(st.candles_5m, token, c)
            except Exception as e:
                print(f"WS [{label}] backfill merge error ({token}): {e}")
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
            "stock_symbol": nifty_token(),
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
        # Resolved once per tick — same value used for every NIFTY comparison
        # below (see state.nifty_token()'s docstring for why this isn't the
        # static cfg.NIFTY50_TOKEN).
        is_nifty = symbol == nifty_token()

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

        # Scalper eligibility, resolved once: the 5-level book parse and the tape
        # are only maintained for TRADEABLE symbols and only while the scalper is
        # switched on. At a ~10,000-symbol universe, running them for every
        # display-only stock would add real per-tick cost for data nothing reads.
        st = self.state
        scalp_on = (interval == "5m" and not is_nifty and bool(stockname)
                    and cfg.SCALP_ENABLED and stockname in st.active_watchlist)
        dvol = 0.0

        # Per-token lock for regular stocks; separate nifty lock for the shared
        # NIFTY candle lists so scan workers never contend across unrelated tokens.
        # ltp is written INSIDE the same lock as the candle mutation, right below -
        # previously it was written afterward, unlocked. A scan-pool worker thread
        # takes its candle+ltp snapshot under this same lock (see entry_engine.py),
        # so writing them separately left a window where a reader could observe
        # THIS tick's already-updated candle paired with the PREVIOUS tick's ltp
        # (or vice versa) - a torn read across two values meant to describe the
        # same instant, feeding inconsistent near_support/VWAP-vs-sizing prices
        # into the same entry decision.
        if is_nifty:
            with self.state._nifty_lock:
                if interval == "5m":
                    self._upsert_list(self.state.nifty_candles_5m, candle)
                if ltp > 0:
                    self.state.nifty_ltp = ltp
        else:
            with self.state.candle_lock(symbol):
                if interval == "5m":
                    # Traded quantity since the previous tick = the forming bar's
                    # volume delta. Computed BEFORE the upsert (which overwrites
                    # the bar) and under the same lock, so it can't race a
                    # concurrent reader. This is the tape's primary volume source
                    # because it is always present, whatever the snap format —
                    # and it aggregates EVERY print since the last tick, where a
                    # single LTQ field would only describe the last one.
                    if scalp_on:
                        prev = st.last_bar_volume.get(stockname)
                        if prev is None:
                            # First sighting: record the baseline only. Emitting
                            # the forming bar's whole accumulated volume as one
                            # print would fake a huge burst of tape activity on
                            # startup / reconnect.
                            dvol = 0.0
                            st.last_bar_volume[stockname] = (candle.start_time,
                                                             candle.volume)
                        elif prev[0] == candle.start_time:
                            dvol = max(0.0, candle.volume - prev[1])
                            st.last_bar_volume[stockname] = (candle.start_time,
                                                             candle.volume)
                        elif candle.start_time > prev[0]:
                            dvol = candle.volume      # bar rolled: all of it is new
                            st.last_bar_volume[stockname] = (candle.start_time,
                                                             candle.volume)
                        else:
                            # STALE out-of-order bar (reconnect replay). _upsert
                            # drops it from the candle series and the tape must
                            # drop it too: treating it as a rolled bar would emit
                            # its whole volume as a phantom print AND rebase the
                            # baseline backwards, so the next legitimate tick
                            # would dump its full bar volume as a second one.
                            dvol = 0.0
                    self._upsert(self.state.candles_5m, symbol, candle)
                    # Bumped under the SAME lock, right after the mutation, so
                    # any reader observing the new version is guaranteed to
                    # also see the updated candle list (see AppState.tick_version).
                    self.state.tick_version[symbol] = self.state.tick_version.get(symbol, 0) + 1
                elif interval == "1h":
                    self._upsert(self.state.candles_1h, symbol, candle)
                if ltp > 0 and stockname:
                    self.state.ltp[stockname] = ltp

        # Dashboard "last bar" clock — only for accepted STOCK 5m bars, and never
        # move it backwards (a reconnect-replayed stale bar is dropped by _upsert
        # but must not rewind this display; NIFTY's own cadence isn't "the" bar).
        if interval == "5m" and not is_nifty:
            bt = candle.start_time[11:16]
            if self.state.last_5m_bar_time is None or bt >= self.state.last_5m_bar_time:
                self.state.last_5m_bar_time = bt

        # Parse order-book depth from the snap field (stocks only; NIFTY has no
        # real order book — its snap shows -0.01 sentinels which _parse_depth
        # discards via the bid_p > 0 guard).
        snap = n.get("snap", "")
        snap_changed = bool(
            snap and stockname and not is_nifty and interval == "5m"
            and self._last_snap.get(stockname) != snap)
        if snap_changed:
            self._last_snap[stockname] = snap
            depth = _parse_depth(snap)
            if depth:
                # MERGE, don't replace: a partial snap (e.g. bid/ask present but
                # no BuyQty/SellQty line) would otherwise drop the last-known
                # `ratio`, silently turning depth_bullish into an auto-pass and
                # letting a sell-skewed book through. New dict = atomic swap for
                # lock-free readers (same GIL-safe pattern as ltp).
                prev = self.state.depth.get(stockname)
                self.state.depth[stockname] = {**prev, **depth} if prev else depth

        # ── Scalper: 5-level book + tape (tradeable symbols only) ─────────────
        # Independent of the legacy depth parse above — that one stays byte-for-
        # byte as it was so `depth_bullish` and the indicators page cannot
        # regress; this one adds the per-level quantities/order counts the W-OBI
        # and anti-spoofing filters need.
        if scalp_on:
            now_m = time.monotonic()
            book  = st.book.get(stockname)
            if snap:
                if snap_changed or book is None:
                    book = parse_snap(snap, ts=now_m)
                    st.book[stockname] = book
                else:
                    # Byte-identical snap = the exchange re-published the SAME
                    # book, so it is confirmed live as of now. Refreshing the
                    # timestamp (a lone atomic float write) keeps the staleness
                    # guard from rejecting a quiet-but-current book; rebuilding
                    # the object would be pure waste.
                    book.ts = now_m

            # One tape print per tick, carrying the book that was live when it
            # traded — that pairing is what lets tape_stats tell an ask-hitting
            # buy from a bid-hitting sell.
            #
            # The volume delta is authoritative. LTQ is only a FALLBACK for a feed
            # that publishes no bar volume, and it must be de-duplicated: LTQ is a
            # level (the last print's size), not a delta, so re-reading it on every
            # quiet tick would append the SAME trade again and again — at ~10
            # ticks/s a single 500-share print would fabricate thousands of shares
            # of "aggressive buying" per second and fire entries on nothing. Only
            # an LTQ/price pair that CHANGED counts as a new print; two identical
            # consecutive prints are undercounted, which is the safe direction.
            qty = dvol
            if qty <= 0 and book is not None and book.ltq:
                ltq_key = (book.ltq, book.ltp or ltp)
                if self._last_ltq.get(stockname) != ltq_key:
                    self._last_ltq[stockname] = ltq_key
                    qty = float(book.ltq)
            price = ltp if ltp > 0 else (book.ltp if book else 0.0)
            if qty > 0 and price > 0:
                st.tape[stockname] = append_tape(
                    st.tape.get(stockname),
                    TapeEvent(
                        ts    = now_m,
                        price = price,
                        qty   = qty,
                        bid   = book.best_bid() if book else 0.0,
                        ask   = book.best_ask() if book else 0.0,
                    ),
                    int(cfg.SCALP_TAPE_MAXLEN),
                )

        # Tick-wise engine: flag this stock for re-evaluation on the next loop
        # cycle. Only 5m ticks update the forming bar; 1h ticks must not enqueue
        # dirty_ticks or they trigger scans on stale 5m bars. Only while ACTIVE.
        if interval == "5m" and not is_nifty and self.state.phase in (TradingPhase.ACTIVE, TradingPhase.WAIT_ZONE, TradingPhase.CUTOFF):
            self.state.dirty_ticks.add(symbol)
            self.state.dirty_ticks_push.add(symbol)
            if scalp_on:
                self.state.dirty_ticks_scalp.add(symbol)
            # See state.last_tick_wallclock's docstring - this is what the tick loop's stale-feed
            # alarm reads.
            self.state.last_tick_wallclock = time.monotonic()

    # ── Candle upsert helpers ─────────────────────────────────────────────────

    @staticmethod
    def _upsert(store: Dict[str, list], symbol: str, candle: Candle) -> None:
        lst = store.get(symbol)
        # `not lst` also covers an EXISTING-but-empty deque — the historical
        # loader stores one for symbols the REST response listed with no bars
        # (halted stock, new listing); lst[-1] on it would raise IndexError.
        if lst is None or not lst:
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
    def _upsert_list(lst: "_deque", candle: Candle) -> None:
        # lst is a deque(maxlen=...) (see AppState.nifty_candles_5m) - append()
        # auto-evicts the oldest entry in O(1) once full, same as _upsert() above.
        if not lst:
            lst.append(candle)
            return
        last = lst[-1].start_time
        if last == candle.start_time:
            lst[-1] = candle
        elif candle.start_time > last:
            lst.append(candle)
