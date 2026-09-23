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
        # module's own top-of-file note) drops that client.
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

    def count(self) -> int:
        return len(self._clients)


# Shared singleton — imported by the router and the scheduler
ws_manager = DashboardWSManager()
