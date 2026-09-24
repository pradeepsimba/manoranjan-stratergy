from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

Fixed universe: BankNifty index + its 14 stocks (the real NIFTY BANK
index's full membership — see cfg.BN_ALL_STOCKS), plus Nifty 50 index + its
50 stocks, deduped on the 11 stocks both strategies share (see
_build_filters). That's ~55 symbol-interval pairs (2026-09-16/17: the Nifty
50 universe grew toward all 50 official constituents in stages — 3 early
additions, LTIMindtree/Nestle India/ONGC, were removed after a direct vendor
query confirmed zero data; 3 more, InterGlobe Aviation/Jio Financial
Services/Max Healthcare, were added after a user-supplied official
constituent list revealed they were missing and a vendor query confirmed
real data. BankNifty's universe grew from 11 to its full real membership of
14, and 11 non-index "extras" it briefly also carried were removed once
that 14-member figure was confirmed, per an explicit user decision to track
index members only) — past the server's documented ~40-per-connection
output buffer limit, so filters are still split across multiple WS
connections
(cfg.WS_MAX_FILTERS_PER_CONN per connection), each with its own independent
retry loop, mirroring the deleted equity engine's split primary/secondary
approach. _process_tick is fully synchronous (no `await`), so concurrent
connections calling it from different asyncio tasks on the
same event loop can never interleave mid-update — the existing threading.Lock
guards remain correct as-is.
"""

import asyncio
import json
import re
from collections import deque as _deque
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import websockets

import app.config as cfg
from app.models import Candle, TradingPhase
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

_LTP_PAT = re.compile(r"LTP\s*([\d.]+)")
_QTY_PAT = re.compile(r"qty\s+(\d+)", re.IGNORECASE)
_BUY_QTY_PAT = re.compile(r"BuyQty\s+(\d+)")
_SELL_QTY_PAT = re.compile(r"SellQty\s+(\d+)")


def _parse_ltp(n: dict) -> float:
    """Extract the LTP from a tick's `n["ltp"]` field — usually a bare
    number, sometimes text with an embedded "LTP <value>" (see _LTP_PAT).
    Returns 0.0 (never raises) if the field is missing or unparseable."""
    if "ltp" not in n:
        return 0.0
    ltp_raw = str(n["ltp"])
    m = _LTP_PAT.search(ltp_raw)
    try:
        return float(m.group(1)) if m else float(ltp_raw)
    except ValueError:
        return 0.0


_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer


_OPTION_CONN_ID = "options"   # reserved key in _conn_status, distinct from the fixed-universe int chunk ids


class MarketDataService:
    def __init__(self) -> None:
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._conn_status: dict = {}
        self.state = get_state()

        # ── Dedicated option WS connection — a THIRD, separate connection
        # driven ONLY by the live ATM CE/PE watchlist below (2026-09-18):
        # set_bn_atm_watch/set_nf_atm_watch, called every tick from
        # SchedulerService._tick_atm_watch whenever the live ATM strike
        # changes, regardless of whether a trade is open. There used to
        # also be a per-TRADE half of this connection's filter set (the
        # "real-option-LTP paper trading" feature, 2026-09-17,
        # set_bn_option_symbol/set_nf_option_symbol) — REMOVED 2026-09-22
        # (methods, backing fields, and the state.py self-registration that
        # only existed to reach them, all deleted outright) after it let a
        # real tick snap a trade's settlement premium past the scalp
        # strategy's tight ~₹2-3 target/stop bracket (see bn_trade.
        # check_tick_exit's docstring for the full incident writeup).
        self._bn_atm_watch: Optional[tuple] = None
        self._nf_atm_watch: Optional[tuple] = None
        self._option_task: Optional[asyncio.Task] = None
        self._option_filters: list = []
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
        all_filters = self._build_filters()
        chunk_size  = cfg.WS_MAX_FILTERS_PER_CONN
        chunks = [all_filters[i:i + chunk_size] for i in range(0, len(all_filters), chunk_size)] or [[]]
        self._conn_status = {i: "Disconnected" for i in range(len(chunks))}
        self._tasks = [asyncio.create_task(self._connect_loop(chunk, i)) for i, chunk in enumerate(chunks)]
        # Resume the option connection too, if a trade was already active
        # across a stop()/start() cycle (e.g. restart() recovering from a WS
        # error mid-trade) — _option_filters survives stop() deliberately.
        if self._option_filters:
            self._conn_status[_OPTION_CONN_ID] = "Disconnected"
            self._option_task = asyncio.create_task(self._connect_loop(self._option_filters, _OPTION_CONN_ID))

    async def stop(self) -> None:
        self._running = False
        all_tasks = list(self._tasks)
        if self._option_task:
            all_tasks.append(self._option_task)
        for t in all_tasks:
            t.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        self._option_task = None
        # Cancellation skips _run_ws's post-loop status update — set it here so
        # the dashboard doesn't show "WS Connected" after the EOD shutdown.
        self.state.ws_status = "WS Stopped"

    # ── Live ATM CE/PE watchlist ─────────────────────────────────────────────
    # Called by SchedulerService._tick_atm_watch whenever the live ATM strike
    # changes (not every tick — only on an actual change). Always resets the
    # cached LTPs to None — a strike change means the PREVIOUS strike's
    # price is no longer relevant, and holding onto it would show a
    # stale/wrong-strike price until the new strike's first real tick
    # arrives. (There used to be a set_bn_option_symbol/set_nf_option_symbol
    # pair here too, for the now-removed real-option-LTP feature — see
    # __init__'s comment above.)

    def set_bn_atm_watch(self, ce_symbol: Optional[str], pe_symbol: Optional[str],
                          strike: Optional[int] = None) -> None:
        self._bn_atm_watch = (ce_symbol, pe_symbol) if (ce_symbol and pe_symbol) else None
        with self.state._atm_watch_lock:
            self.state.bn_atm_ce_symbol = ce_symbol
            self.state.bn_atm_pe_symbol = pe_symbol
            self.state.bn_atm_ce_ltp = None
            self.state.bn_atm_pe_ltp = None
            # Written in the SAME locked group as the symbols above (2026-09-23
            # fix, found in review) — see state.py's bn_atm_watch_strike comment
            # for why a separate unlocked copy let _build_payload tear this.
            self.state.bn_atm_watch_strike = strike
        self._resync_option_connection()

    def set_nf_atm_watch(self, ce_symbol: Optional[str], pe_symbol: Optional[str],
                          strike: Optional[int] = None) -> None:
        self._nf_atm_watch = (ce_symbol, pe_symbol) if (ce_symbol and pe_symbol) else None
        with self.state._atm_watch_lock:
            self.state.nf_atm_ce_symbol = ce_symbol
            self.state.nf_atm_pe_symbol = pe_symbol
            self.state.nf_atm_ce_ltp = None
            self.state.nf_atm_pe_ltp = None
            self.state.nf_atm_watch_strike = strike
        self._resync_option_connection()

    def _resync_option_connection(self) -> None:
        """
        Recomputes the option connection's desired filter set from
        _bn_atm_watch/_nf_atm_watch and, if it actually changed, tears down
        and reconnects that one dedicated connection with the new set. The
        vendor's LIVE_FEED_INIT protocol only has an
        observed "subscribe at connect time" shape (no separate incremental-
        subscribe message), so a reconnect is the only way to change what
        this connection streams — acceptable since trades open/close far
        less often than ticks arrive.

        CONFIRMED 2026-09-18 (after two earlier rounds of testing wrongly
        concluded this vendor carries no option data at all): options here
        are 1-MINUTE only, not 5m like everything else in this app — a
        `"interval": "5m"` request for an option symbol silently returns
        nothing, at both the REST and WS layer, which is exactly what
        produced that wrong conclusion. `stockname` for an option is the
        underlying's plain name ("NIFTY"/"BANKNIFTY" — cfg.BN_OPTION_
        UNDERLYING/NF_OPTION_UNDERLYING), confirmed from the vendor's own
        echoed tick data — NOT the option symbol itself repeated.
        """
        # dict, not a set — dedupes for free if BN/NF's ATM strikes ever
        # happen to coincide on the identical symbol.
        merged = {}
        if self._bn_atm_watch:
            ce, pe = self._bn_atm_watch
            merged[ce] = cfg.BN_OPTION_UNDERLYING
            merged[pe] = cfg.BN_OPTION_UNDERLYING
        if self._nf_atm_watch:
            ce, pe = self._nf_atm_watch
            merged[ce] = cfg.NF_OPTION_UNDERLYING
            merged[pe] = cfg.NF_OPTION_UNDERLYING
        new_filters = [{"stock_symbol": sym, "stockname": name, "interval": "1m"}
                       for sym, name in merged.items()]
        if new_filters == self._option_filters:
            return
        self._option_filters = new_filters
        if self._option_task:
            # Not awaited — this is a sync method, called from
            # SchedulerService._tick_atm_watch's set_bn_atm_watch/
            # set_nf_atm_watch calls (the only remaining callers now that
            # bn_trade.py/nf_trade.py's per-trade set_bn_option_symbol/
            # set_nf_option_symbol were removed 2026-09-22). cancel() just
            # schedules the cancellation; the old task's own cleanup
            # (_run_ws's post-`async with` line) still runs in the
            # background and could briefly
            # overwrite _conn_status[_OPTION_CONN_ID] back to "Disconnected"
            # right after the new task below sets "Connected" — a cosmetic
            # status-string race only, self-corrects on the new connection's
            # next status change; ticks are routed by symbol match, not
            # connection identity, so this never affects data correctness.
            self._option_task.cancel()
            self._option_task = None
        self._conn_status.pop(_OPTION_CONN_ID, None)
        self._refresh_ws_status()
        if new_filters and self._running:
            self._conn_status[_OPTION_CONN_ID] = "Disconnected"
            self._option_task = asyncio.create_task(self._connect_loop(new_filters, _OPTION_CONN_ID))

    # ── WebSocket connection loop ───────────────────────────────────────────────

    async def _connect_loop(self, filters: list, conn_idx: int) -> None:
        while self._running:
            try:
                print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS connecting (conn {conn_idx}, {len(filters)} filters)…")
                await self._run_ws(filters, conn_idx)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._conn_status[conn_idx] = f"Error: {e}"
                self._refresh_ws_status()
                print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS error (conn {conn_idx}): {e}")
            if self._running:
                await asyncio.sleep(5)

    async def _run_ws(self, filters: list, conn_idx: int) -> None:
        async with websockets.connect(
            cfg.WS_URL,
            ping_interval=20,
            ping_timeout=30,
            open_timeout=15,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            self._conn_status[conn_idx] = "Connected"
            self._refresh_ws_status()
            print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS connected (conn {conn_idx})")

            await ws.send(json.dumps({
                "type":       "LIVE_FEED_INIT",
                "filters":    filters,
                "latestOnly": True,
            }))
            print(f"WS subscribed (conn {conn_idx}): {len(filters)} symbol-interval pairs")

            async for message in ws:
                if not self._running:
                    break
                try:
                    data  = json.loads(message)
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        self._process_tick(item)
                except Exception as e:
                    print(f"Tick parse error (conn {conn_idx}): {e}")

        self._conn_status[conn_idx] = "Disconnected"
        self._refresh_ws_status()
        print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] WS disconnected (conn {conn_idx}) "
              f"(loop ended — either the server closed the connection or shutdown was requested)")

    def _refresh_ws_status(self) -> None:
        """
        Combine all connections' individual status into the single string the
        dashboard displays (st.ws_status) — see the module docstring for why
        there can be more than one connection now.
        """
        statuses  = list(self._conn_status.values())
        connected = sum(1 for s in statuses if s == "Connected")
        if connected == len(statuses):
            self.state.ws_status = "WS Connected"
        elif connected > 0:
            self.state.ws_status = f"WS Partial ({connected}/{len(statuses)} connected)"
        else:
            errors = [s for s in statuses if s.startswith("Error")]
            self.state.ws_status = f"WS Error: {errors[0]}" if errors else "WS Disconnected"

    # ── Subscription filter builder ────────────────────────────────────────────

    def _build_filters(self) -> list:
        """
        BankNifty index + its 14 stocks, and Nifty 50 index + its 50 stocks,
        all at 5m. Stock filters are deduped by stock_symbol — 11 tokens are
        shared between the two universes (BN's 6 leaders plus AU Small
        Finance Bank/Federal Bank/IDFC First Bank/PNB/Canara Bank, which
        NF_ALL_STOCKS also carries as its own BN-parity "extras"), so the
        combined unique-stock count is 53, not BN's 14 + NF's 50 — each
        token must be subscribed exactly once.
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
        if not symbol:
            return

        # Live ATM CE/PE watchlist (2026-09-18) — never touches candles_5m/ltp
        # (those are documented as "the fixed BN/NF stock universe only");
        # just updates whichever watch symbol it matches (see
        # set_bn_atm_watch/set_nf_atm_watch above), a plain live cache with
        # no per-trade latch. Checked before the 5m-only gate below and
        # before the qty/candle-construction work, which would be wasted
        # effort for a tick outside the fixed universe.
        #
        # The real-option-LTP-per-TRADE match (2026-09-17) that used to live
        # here — matching a tick against active_trade.option_symbol and
        # flipping a one-way trade.premium_synthetic latch — was REMOVED
        # 2026-09-22 (explicit user decision), field and all, alongside
        # bn_trade.py/nf_trade.py no longer calling set_bn_option_symbol/
        # set_nf_option_symbol at all: that override let a real tick snap a
        # trade's settlement premium straight past this strategy's whole
        # ~₹2-3 target/stop bracket (confirmed root cause of a real
        # production bug — see bn_trade.check_tick_exit's docstring).
        # Removing the MATCH here too (not just the callers that used to
        # trigger a subscription for it) closes this off completely: even if
        # a tick ever arrived for a symbol that happened to equal some
        # trade's option_symbol, there is no longer anything left for it to
        # flip — settlement is unconditionally synthetic now.
        #
        # CONFIRMED 2026-09-18: this vendor streams options at 1-MINUTE
        # granularity ONLY, not 5m like everything else here — the original
        # version of this method required interval == "5m" unconditionally
        # BEFORE this symbol-match check even ran, which silently discarded
        # every real option tick and produced two rounds of (wrong)
        # "this vendor has no option data at all" testing before the actual
        # cause was found. Options ticks are always "1m" — checked explicitly
        # so a future vendor protocol quirk can't silently disable this path
        # the same way again.
        st = self.state
        with st._atm_watch_lock:
            is_bn_atm_ce = st.bn_atm_ce_symbol and symbol == st.bn_atm_ce_symbol
            is_bn_atm_pe = st.bn_atm_pe_symbol and symbol == st.bn_atm_pe_symbol
            is_nf_atm_ce = st.nf_atm_ce_symbol and symbol == st.nf_atm_ce_symbol
            is_nf_atm_pe = st.nf_atm_pe_symbol and symbol == st.nf_atm_pe_symbol
            if is_bn_atm_ce or is_bn_atm_pe or is_nf_atm_ce or is_nf_atm_pe:
                if interval != "1m":
                    return
                ltp = _parse_ltp(n)
                if ltp > 0:
                    if is_bn_atm_ce: st.bn_atm_ce_ltp = ltp
                    if is_bn_atm_pe: st.bn_atm_pe_ltp = ltp
                    if is_nf_atm_ce: st.nf_atm_ce_ltp = ltp
                    if is_nf_atm_pe: st.nf_atm_pe_ltp = ltp
                return

        if interval != "5m":
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

        ltp = _parse_ltp(n)

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
            if self.state.bn_index_synthetic:
                self._update_synthetic_index()
            if self.state.nf_index_synthetic:
                self._update_synthetic_nf_index()

        if ltp > 0:
            if symbol == cfg.BN_INDEX_TOKEN:
                self.state.bn_index_ltp = ltp
                # Execution-simulation fill delay (2026-09-24) — see state.py's
                # bn_index_tick_seq comment: this is what lets a pending
                # entry/exit tell "a genuinely new tick arrived" apart from
                # "the 100ms tick loop just re-polled the same stale price".
                self.state.bn_index_tick_seq += 1
            elif symbol == cfg.NF_INDEX_TOKEN:
                self.state.nf_index_ltp = ltp
                self.state.nf_index_tick_seq += 1
            else:
                name = self._token_to_name.get(symbol)
                if name:
                    # Locked (2026-09-24, found in review) — see state.py's
                    # _ltp_lock comment: GET /api/prices reads this dict from
                    # a real executor thread via dict.update(), which can
                    # race a brand-new key being inserted here on the event
                    # loop. This single-key write is cheap enough that
                    # locking it on every tick has no meaningful cost.
                    with self.state._ltp_lock:
                        self.state.ltp[name] = ltp

        # Live-price ticker push — every 5m tick (index or stock) refreshes the
        # dashboard delta; the BN engine's entry/exit evaluation runs on its own
        # tick-wise loop timer, not off a per-tick dirty flag.
        # Deliberately unlocked (found in review, confirmed safe — not an
        # oversight): _process_tick is fully synchronous (no `await`, see this
        # class's own docstring), so this add() and _push_tick_updates_loop's
        # swap (`dirty, st.dirty_ticks_push = st.dirty_ticks_push, set()`)
        # can never interleave on the single event loop both run on — same
        # "only ever touched from the event loop" reasoning price_alerts.py's
        # module-level _was_consensus state documents. Would need a lock only
        # if tick ingest ever moved to a real OS thread.
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

        # weighted_pct so far is Σ pct_i·(weight_i/100) — only a correct
        # weighted AVERAGE if total_weight sums to exactly 100. It never
        # does: cfg.BN_INDEX_WEIGHTS' 11 stocks sum to 98.70 even when every
        # stock has ticked, and total_weight drops further any time a
        # constituent's latest candle is missing (a stock hasn't ticked yet
        # this bar, a feed gap, etc — `continue` above excludes it entirely).
        # Using weighted_pct as-is silently understates the index's move
        # proportionally to whatever weight is missing — e.g. a single
        # missing HDFC BANK tick (31.86% weight) would mute the computed
        # move by roughly a third versus the real index. Rescale by the
        # weight actually present so this stays a true weighted average of
        # whichever constituents have data, not a diluted-toward-zero one.
        weighted_pct = weighted_pct * 100.0 / total_weight

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
        self.state.bn_index_tick_seq += 1   # see state.py's bn_index_tick_seq comment

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

        # Same rescale as _update_synthetic_index above — NF_INDEX_WEIGHTS is
        # an equal-weight approximation (see config.py) but total_weight
        # still drops below 100 whenever a constituent's latest candle is
        # missing, which would otherwise mute the computed move.
        weighted_pct = weighted_pct * 100.0 / total_weight

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
        self.state.nf_index_tick_seq += 1   # see state.py's bn_index_tick_seq comment (NF mirror)

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
            # cfg.MAX_CANDLE_BUFFER directly (found in review, 2026-09-23) —
            # this used to be a separately-hardcoded _MAX_CANDLES=300 that
            # only happened to match cfg.MAX_CANDLE_BUFFER by coincidence;
            # retuning one without the other would have silently desynced
            # per-symbol buffer sizes from the documented "300 bars, ~4
            # sessions" cap.
            store[symbol] = _deque([candle], maxlen=cfg.MAX_CANDLE_BUFFER)
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
