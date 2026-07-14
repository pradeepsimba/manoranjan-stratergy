from __future__ import annotations

import asyncio
from typing import Set

from fastapi import WebSocket


class MarketWSManager:
    """Public broadcast channel — shared market data only (prices/candles/status).
    Never carries per-user account data; see AccountWSManager for that."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        print(f"Market WS: browser connected (total={len(self._clients)})")

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        print(f"Market WS: browser disconnected (total={len(self._clients)})")

    async def broadcast(self, json_str: str) -> None:
        # Send to all clients concurrently — one slow/broken browser must not
        # stall the tick-delta push for everyone else. Any send error
        # (disconnect, reset, closed socket) drops that client.
        if not self._clients:
            return
        clients = list(self._clients)
        results = await asyncio.gather(
            *(ws.send_text(json_str) for ws in clients),
            return_exceptions=True,
        )
        for ws, res in zip(clients, results):
            if isinstance(res, BaseException):
                self._clients.discard(ws)

    def count(self) -> int:
        return len(self._clients)


# Shared singleton — imported by the market router and the scheduler
market_ws_manager = MarketWSManager()
