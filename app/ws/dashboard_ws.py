from __future__ import annotations

from typing import Set

from fastapi import WebSocket, WebSocketDisconnect


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
        dead: Set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(json_str)
            except (WebSocketDisconnect, ConnectionResetError, RuntimeError, OSError):
                dead.add(ws)
        self._clients -= dead

    def count(self) -> int:
        return len(self._clients)


# Shared singleton — imported by the router and the scheduler
ws_manager = DashboardWSManager()
