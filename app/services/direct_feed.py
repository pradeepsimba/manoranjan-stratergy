"""
Direct live-tick channel from the feed process (ws_real_time_data's DirectFeedServer).

The normal tick path is broker -> feed -> Postgres (one write per candle per 200 ms) -> NOTIFY ->
websocket server -> this app: 0.3-0.7 s end to end. This client connects straight to the feed
process over plain TCP and receives every candle update the moment the feed processes the tick, at
network latency. It is ADDITIVE: the websocket path keeps running and takes over automatically
whenever this one is quiet (see MarketDataService._process_tick's source check), so a feed restart,
a dropped connection or a stall can never leave the app without data.

Protocol: newline-delimited JSON (see DirectFeedServer's class comment). Candle updates carry the
full current candle state, in the same shape MarketDataService._process_tick already consumes from
the websocket server, so they are fed through exactly the same code.

Disabled unless DIRECT_FEED_ADDR ("host:port") and DIRECT_FEED_TOKEN are both configured.
"""
import asyncio
import json
import time
from typing import Callable, Iterable, Optional

# No message (tick or heartbeat) for this long = treat the channel as dead and let the websocket
# path carry the data. The server sends a heartbeat every second when idle.
LIVE_TIMEOUT_S = 5.0
# readline() waits at most this long for ANY line (tick or heartbeat). The server heartbeats every
# second, so this much silence means the link is dead (e.g. a half-open TCP connection after the feed
# host hung or the network partitioned, which never delivers a FIN/RST) - drop it and reconnect.
_READ_TIMEOUT_S = LIVE_TIMEOUT_S + 3.0
_YIELD_EVERY = 200   # ticks processed before handing the event loop back, bounding stalls under a flood
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 10.0


class DirectFeedClient:
    def __init__(self, host: str, port: int, token: str, on_tick: Callable[[dict], None],
                 intervals: Iterable[str] = ("1m", "5m")):
        self._host = host
        self._port = port
        self._token = token
        self._on_tick = on_tick
        self._intervals = list(intervals)
        self._symbols: frozenset = frozenset()
        self._writer: Optional[asyncio.StreamWriter] = None
        self._last_msg = 0.0           # time.monotonic() of the last line received (tick OR heartbeat)
        self._last_tick = 0.0          # time.monotonic() of the last real LIVE tick (not a heartbeat or snapshot)
        self._last_tick_by_symbol: dict = {}   # symbol -> time.monotonic() of its last live tick
        self.connected = False
        self.ticks_received = 0

    # ── public ────────────────────────────────────────────────────────────────

    def is_live(self) -> bool:
        """True while the channel is delivering (a line arrived within LIVE_TIMEOUT_S)."""
        return self.connected and (time.monotonic() - self._last_msg) < LIVE_TIMEOUT_S

    def symbols_fresh(self, symbols: Iterable[str], max_age_s: float) -> bool:
        """True only if EVERY given symbol had a live tick within max_age_s. Lets the caller insist on the
        instruments that matter (the two indices) rather than 'some tick for something' - option or stock
        ticks must not keep the feed looking healthy while an index is frozen."""
        if not self.connected:
            return False
        now = time.monotonic()
        return all(now - self._last_tick_by_symbol.get(s, -1e9) < max_age_s for s in symbols)

    def ticks_fresh(self, max_age_s: float) -> bool:
        """True if a real live tick arrived within max_age_s. Heartbeats do NOT count: they keep flowing
        while the feed's broker stream is dead, so only ticks prove prices are actually arriving."""
        return self.connected and self._last_tick > 0 and (time.monotonic() - self._last_tick) < max_age_s

    def set_symbols(self, symbols: Iterable[str]) -> None:
        """Replace the subscription. Takes effect on the open connection immediately (no reconnect),
        and is re-sent automatically after any reconnect."""
        new = frozenset(symbols)
        if new == self._symbols:
            return
        self._symbols = new
        if self._writer is not None and self.connected:
            try:
                self._send(self._subscribe_msg())
            except Exception as e:  # noqa: BLE001 - the read loop will notice a dead socket
                print(f"Direct feed: could not send subscription update: {e}")

    async def run(self) -> None:
        """Connect, read, reconnect forever. Cancel the task to stop."""
        backoff = _INITIAL_BACKOFF_S
        while True:
            started = time.monotonic()
            try:
                await self._session()
                # Reset the backoff only if the session actually held for a while: a server that accepts
                # and immediately closes (e.g. a rejected token) must not be retried every second forever.
                if time.monotonic() - started >= 10.0:
                    backoff = _INITIAL_BACKOFF_S
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"Direct feed: connection error: {e!r}")
            finally:
                self.connected = False
                self._writer = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    # ── internals ─────────────────────────────────────────────────────────────

    def _subscribe_msg(self) -> dict:
        return {"subscribe": sorted(self._symbols), "intervals": self._intervals}

    def _send(self, msg: dict) -> None:
        assert self._writer is not None
        self._writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), timeout=5.0)
        self._writer = writer
        try:
            # Mark connected BEFORE sending: a set_symbols() call that lands while the first message is
            # still draining then sends its own update, instead of seeing connected=False and being
            # silently lost until the next reconnect.
            self.connected = True
            self._last_msg = time.monotonic()
            # Auth in a line of its own, subscription in the next: the server caps the pre-auth line at
            # 64 KB, and a large watchlist put into the same line as the token exceeded it, so the very
            # first message was rejected and the client reconnect-looped forever.
            self._send({"auth": self._token})
            self._send(self._subscribe_msg())
            await writer.drain()
            print(f"Direct feed: connected to {self._host}:{self._port} "
                  f"({len(self._symbols)} symbols, intervals {self._intervals})")
            batch = 0
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=_READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    print(f"Direct feed: nothing received for {_READ_TIMEOUT_S:.0f}s - treating the link as dead")
                    return
                if not line:
                    print("Direct feed: server closed the connection")
                    return
                self._last_msg = time.monotonic()
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "hb" in msg:
                    continue
                self.ticks_received += 1
                if not msg.get("snapshot"):
                    self._last_tick = time.monotonic()
                    sym = msg.get("stock_symbol")
                    if sym:
                        self._last_tick_by_symbol[sym] = self._last_tick
                try:
                    self._on_tick(msg)
                except Exception as e:  # noqa: BLE001 - one bad tick must not drop the connection
                    print(f"Direct feed: tick handler error: {e!r}")
                batch += 1
                if batch >= _YIELD_EVERY:
                    # readline() does not suspend while the buffer already holds complete lines, so a
                    # flood was processed in one uninterrupted run (measured stalls up to ~90 ms for
                    # other tasks). Yield periodically.
                    batch = 0
                    await asyncio.sleep(0)
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
