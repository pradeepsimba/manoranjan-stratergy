from __future__ import annotations

"""
Live WebSocket feed from the custom market data server.

The tradable universe is now dynamic (discovered into the `instruments`
table, not a fixed dict) and can exceed the server's ~40-entries-per-
connection output buffer limit, so the instrument list is sharded into
batches of cfg.WS_FILTER_BATCH_SIZE and each batch gets its own WS connection
(own reconnect loop, own `LIVE_FEED_INIT` subscribe message) — the small
fixed 12-symbol BN universe only ever needed one connection; a broader equity
universe needs several running concurrently.

Every instrument — including candles and live price — is now keyed
UNIFORMLY by TOKEN (no more "candles by token, ltp by name" split the old BN
engine had; token is the one stable identifier, since it's also the
`instruments` table's primary key).
"""

import asyncio
import json
import re
from collections import deque as _deque
from typing import Any, Dict, List, Optional

import websockets
import websockets.exceptions

import app.config as cfg
from app.models import Candle, MarketPhase
from app.state import get_state

_LTP_PAT = re.compile(r"LTP\s*([\d.]+)")

# The feed's "snap" field is a free-text quote snapshot, e.g.:
#   "360 One WAM (13061) | LTP 1109.90 Qty 286 | O:1108.30 H:1127.80 L:1085.70
#    C:1081.30 | Vol 2682207 | BuyQty 112267 SellQty 149711 | OI 12037000
#    (0.00%) | UC 1189.40 LC 973.20\nBids (price x qty):\n   1) 1109.40 x 1\n
#    ...\nAsks (price x qty):\n   1) 1109.50 x 20\n..."
# — real Level-1 depth the app previously discarded entirely (only OHLCV + LTP
# were parsed out of each tick). \b keeps "Qty" from also matching inside
# "BuyQty"/"SellQty".
_SNAP_QTY_PAT  = re.compile(r"\bQty\s+(\d+)")
_SNAP_BUY_PAT  = re.compile(r"BuyQty\s+(\d+)")
_SNAP_SELL_PAT = re.compile(r"SellQty\s+(\d+)")
_SNAP_OI_PAT   = re.compile(r"\bOI\s+(\d+)")
_SNAP_UC_PAT   = re.compile(r"\bUC\s+([\d.]+)")
_SNAP_LC_PAT   = re.compile(r"\bLC\s+([\d.]+)")
_SNAP_ROW_PAT  = re.compile(r"\d+\)\s+([\d.]+)\s+x\s+(\d+)")


def _parse_snap(snap: str) -> Optional[Dict[str, Any]]:
    """Parses the feed's free-text depth snapshot into structured Level-1 data.
    Returns None if `snap` is empty/unrecognized (caller then just skips the
    depth update for this tick, leaving the previous snapshot in place)."""
    if not snap:
        return None
    bids_idx = snap.find("Bids")
    asks_idx = snap.find("Asks")
    header = snap[:bids_idx] if bids_idx != -1 else snap
    bids_section = snap[bids_idx:asks_idx] if bids_idx != -1 and asks_idx != -1 else ""
    asks_section = snap[asks_idx:] if asks_idx != -1 else ""

    def _search(pat: re.Pattern, cast):
        m = pat.search(header)
        return cast(m.group(1)) if m else None

    try:
        return {
            "ltpQty":       _search(_SNAP_QTY_PAT, int),
            "buyQty":       _search(_SNAP_BUY_PAT, int),
            "sellQty":      _search(_SNAP_SELL_PAT, int),
            "oi":           _search(_SNAP_OI_PAT, int),
            "upperCircuit": _search(_SNAP_UC_PAT, float),
            "lowerCircuit": _search(_SNAP_LC_PAT, float),
            "bids": [{"price": float(p), "qty": int(q)} for p, q in _SNAP_ROW_PAT.findall(bids_section)],
            "asks": [{"price": float(p), "qty": int(q)} for p, q in _SNAP_ROW_PAT.findall(asks_section)],
        }
    except (ValueError, AttributeError):
        return None

_MAX_CANDLES = cfg.MAX_CANDLE_BUFFER
_WS_MAX_SIZE = 16 * 1024 * 1024   # 16 MiB receive buffer


class MarketDataService:
    def __init__(self) -> None:
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._instruments: List[Dict[str, str]] = []   # [{"token": ..., "name": ...}, ...]
        self._conn_status: Dict[int, str] = {}
        self.state = get_state()

    def start(self, instruments: List[Dict[str, str]]) -> None:
        self._instruments = instruments
        self._running = True
        self._conn_status = {}
        batches = [
            instruments[i:i + cfg.WS_FILTER_BATCH_SIZE]
            for i in range(0, len(instruments), cfg.WS_FILTER_BATCH_SIZE)
        ] or [[]]
        self._tasks = [
            asyncio.create_task(self._connect_loop(idx, batch))
            for idx, batch in enumerate(batches)
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # Cancellation skips _run_ws's post-loop status update — set it here so
        # the dashboard doesn't show "Connected" after a deliberate shutdown.
        self.state.ws_status = "WS Stopped"

    async def restart(self, instruments: List[Dict[str, str]]) -> None:
        was_running = self._running
        if was_running:
            await self.stop()
            self.state.ws_status = "WS Resubscribing…"
        self.start(instruments)

    # ── WebSocket connection loop (one per instrument-batch shard) ───────────

    async def _connect_loop(self, idx: int, batch: List[Dict[str, str]]) -> None:
        filters = [
            {"stock_symbol": b["token"], "stockname": b["name"], "interval": cfg.INTERVAL_5M}
            for b in batch
        ]
        while self._running:
            try:
                await self._run_ws(idx, filters)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._conn_status[idx] = f"Error: {e}"
                self._refresh_overall_status()
                print(f"WS shard {idx} error: {e}")
            if self._running:
                await asyncio.sleep(5)

    async def _run_ws(self, idx: int, filters: list) -> None:
        async with websockets.connect(
            cfg.WS_URL,
            ping_interval=20,
            ping_timeout=30,
            open_timeout=15,
            max_size=_WS_MAX_SIZE,
        ) as ws:
            self._conn_status[idx] = "Connected"
            self._refresh_overall_status()

            await ws.send(json.dumps({
                "type":       "LIVE_FEED_INIT",
                "filters":    filters,
                "latestOnly": True,
            }))
            print(f"WS shard {idx} subscribed: {len(filters)} symbol-interval pairs")

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

        self._conn_status[idx] = "Disconnected"
        self._refresh_overall_status()

    def _refresh_overall_status(self) -> None:
        total = max(len(self._tasks), 1)
        connected = sum(1 for v in self._conn_status.values() if v == "Connected")
        if connected == 0:
            self.state.ws_status = "WS Disconnected"
        elif connected == total:
            self.state.ws_status = "WS Connected" if total == 1 else f"WS Connected ({connected}/{total})"
        else:
            self.state.ws_status = f"WS Degraded ({connected}/{total})"

    # ── Tick processing ───────────────────────────────────────────────────────

    def _process_tick(self, n: Dict[str, Any]) -> None:
        token    = n.get("stock_symbol", "")
        interval = n.get("interval", "")
        if not token or interval != cfg.INTERVAL_5M:
            return

        candle = Candle(
            start_time=n.get("start_time", ""),
            open=float(n.get("open",   0)),
            close=float(n.get("close", 0)),
            high=float(n.get("high",   0)),
            low=float(n.get("low",     0)),
            volume=float(n.get("volume", 0)),
        )

        with self.state.candle_lock(token):
            self._upsert(self.state.candles_5m, token, candle)
            # Bumped under the SAME lock, right after the mutation, so any
            # reader observing the new version also sees the updated candle list.
            self.state.tick_version[token] = self.state.tick_version.get(token, 0) + 1

        ltp = 0.0
        if "ltp" in n:
            ltp_raw = str(n["ltp"])
            m = _LTP_PAT.search(ltp_raw)
            try:
                ltp = float(m.group(1)) if m else float(ltp_raw)
            except (ValueError, AttributeError):
                pass
        if ltp > 0:
            self.state.ltp[token] = ltp

        depth = _parse_snap(n.get("snap", ""))
        if depth is not None:
            self.state.depth[token] = depth

        if self.state.phase in (MarketPhase.OPEN, MarketPhase.PRE_MARKET):
            self.state.dirty_ticks_push.add(token)

    # ── Candle upsert helper ──────────────────────────────────────────────────

    @staticmethod
    def _upsert(store: dict, token: str, candle: Candle) -> None:
        lst = store.get(token)
        if lst is None:
            store[token] = _deque([candle], maxlen=_MAX_CANDLES)
            return
        last = lst[-1].start_time
        if last == candle.start_time:
            lst[-1] = candle          # update in-progress bar
        elif candle.start_time > last:   # ISO strings — lexicographic == chronological
            lst.append(candle)        # deque(maxlen) auto-evicts from left — O(1)
        # else: stale out-of-order bar (e.g. reconnect replay) — appending it
        # would break the chronological order readers rely on; drop it.
