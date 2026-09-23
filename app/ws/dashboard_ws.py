from __future__ import annotations

import asyncio
from typing import Set

from fastapi import WebSocket


# Root cause of "the dashboard sometimes freezes" (found 2026-09-23, live-
# reproduced against a running dev server: a fresh WS client waited ~60s
# for its first STATE_UPDATE instead of the usual ~1s): asyncio.gather(...,
# return_exceptions=True) below does NOT bound how long it waits — it only
# stops one client's raised exception from cancelling the others' sends. A
# genuinely hung send (a browser tab that went to sleep, a network drop
# with no clean close/RST yet) can block ws.send_text() indefinitely
# waiting for TCP buffer space, and gather() waits for EVERY task to
# finish before returning — so one stuck client freezes broadcast() for
# ALL clients, and since _push_dashboard_loop/_push_tick_updates_loop's
# `while True` awaits this same broadcast() every cycle, the whole
# dashboard (every connected browser) stalls until that one connection's
# transport eventually times out on its own. This directly contradicted
# this class's own "one slow/broken browser must not stall... for
# everyone else" comment below — return_exceptions=True alone does not
# deliver that guarantee, only a per-send timeout does.
_SEND_TIMEOUT_S = 2.0


class DashboardWSManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        # Serializes broadcast() end-to-end (found in review, 2026-09-23):
        # this manager has THREE independent producers calling broadcast()
        # on their own cadence — SchedulerService._push_dashboard_loop (1s),
        # _push_tick_updates_loop (~100ms), and _tick_alerts (on fire) — each
        # its own asyncio.Task. Without this lock, two of them can each
        # snapshot `self._clients` and call ws.send_text() on the SAME
        # WebSocket concurrently from different tasks; Starlette's WebSocket
        # wraps one ASGI connection and is not safe for concurrent send()
        # calls from separate coroutines — an interleaved write can corrupt
        # that client's frame stream or raise mid-send. It also meant a
        # single stuck client could get _force_close scheduled once per
        # overlapping broadcast() call before the first one even finished
        # discarding it. Serializing means a stalled client (bounded by
        # _SEND_TIMEOUT_S) can delay the NEXT broadcast() call from
        # starting, but never lets two sends to one client overlap.
        self._broadcast_lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        print(f"Browser connected (total={len(self._clients)})")

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        print(f"Browser disconnected (total={len(self._clients)})")

    async def broadcast(self, json_str: str) -> None:
        # Send to all clients concurrently — one slow/broken browser must not
        # stall the 100ms tick-delta push for everyone else. Any send error
        # OR a send that doesn't complete within _SEND_TIMEOUT_S (a hung
        # connection, not just an immediately-erroring one — see this
        # module's own top-of-file note) drops that client. The whole method
        # runs under _broadcast_lock (see __init__'s comment) so this
        # manager's three independent producer loops never send to the same
        # client concurrently.
        async with self._broadcast_lock:
            if not self._clients:
                return
            clients = list(self._clients)
            results = await asyncio.gather(
                *(asyncio.wait_for(ws.send_text(json_str), timeout=_SEND_TIMEOUT_S) for ws in clients),
                return_exceptions=True,
            )
            for ws, res in zip(clients, results):
                if isinstance(res, BaseException):
                    self._clients.discard(ws)
                    # 2026-09-23 fix, found in review: discarding from _clients
                    # only stops FUTURE broadcasts to this connection — it does
                    # NOT close it. Without this, a hung/half-open connection
                    # (the exact case _SEND_TIMEOUT_S targets) never gets a
                    # close frame, so the browser's WebSocket.onclose/reconnect
                    # logic never fires, and app/api/dashboard.py's dashboard_ws
                    # handler stays parked in `await websocket.receive_text()`
                    # forever — the task and its socket leak for the life of
                    # the process instead of being torn down. Fire-and-forget,
                    # bounded by its own timeout, so one broken client's cleanup
                    # never adds to this (already time-bounded) broadcast call's
                    # latency; best-effort only since the connection may already
                    # be fully dead. No dedup guard is needed here: ws was just
                    # discarded from _clients above while still holding
                    # _broadcast_lock, so no other (necessarily later, since
                    # they're serialized) broadcast() call can ever see this ws
                    # again and schedule a second close for it.
                    asyncio.create_task(self._force_close(ws))

    async def _force_close(self, ws: WebSocket) -> None:
        try:
            await asyncio.wait_for(ws.close(), timeout=_SEND_TIMEOUT_S)
        except Exception:
            pass  # already broken/closing — nothing more we can do

    def count(self) -> int:
        return len(self._clients)


# Shared singleton — imported by the router and the scheduler
ws_manager = DashboardWSManager()
