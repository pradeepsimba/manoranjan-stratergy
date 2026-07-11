from __future__ import annotations

import asyncio
from typing import Set

from fastapi import WebSocket

from app.state import spawn


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
        # stall the 100ms tick-delta push for everyone else. gather() alone
        # still WAITS for the slowest send, and a stalled TCP window (suspended
        # laptop, throttled tab) parks send_text in drain() indefinitely with
        # NO exception — so each send also gets a timeout, and a client that
        # times out is dropped like any other send error.
        if not self._clients:
            return
        clients = list(self._clients)
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(json_str), timeout=2.0)
              for ws in clients),
            return_exceptions=True,
        )
        for ws, res in zip(clients, results):
            if isinstance(res, BaseException):
                self._clients.discard(ws)
                # CLOSE the socket, don't just forget it: an abandoned-but-open
                # connection leaves the /ws/dashboard handler parked in
                # receive_text() and the browser never sees onclose — the tab
                # shows a live-looking but permanently frozen dashboard. (A
                # timed-out send may also have been cancelled mid-frame, which
                # corrupts the stream — closing is the only safe disposal.)
                spawn(self._close_quietly(ws))

    @staticmethod
    async def _close_quietly(ws: WebSocket) -> None:
        try:
            await ws.close()
        except Exception:
            pass

    def count(self) -> int:
        return len(self._clients)


# Shared singleton — imported by the router and the scheduler
ws_manager = DashboardWSManager()
