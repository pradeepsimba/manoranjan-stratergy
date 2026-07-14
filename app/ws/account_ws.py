from __future__ import annotations

import asyncio
from typing import Dict, Set

from fastapi import WebSocket


class AccountWSManager:
    """
    Authenticated, per-user channel — order fills, holdings/positions/funds
    changes are pushed ONLY to that user's own connected sockets. Never a
    global broadcast (unlike MarketWSManager): account data must not leak
    across users.
    """

    def __init__(self) -> None:
        self._clients: Dict[int, Set[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.setdefault(user_id, set()).add(ws)

    def disconnect(self, user_id: int, ws: WebSocket) -> None:
        sockets = self._clients.get(user_id)
        if sockets:
            sockets.discard(ws)
            if not sockets:
                self._clients.pop(user_id, None)

    async def send_to_user(self, user_id: int, json_str: str) -> None:
        sockets = self._clients.get(user_id)
        if not sockets:
            return
        clients = list(sockets)
        results = await asyncio.gather(
            *(ws.send_text(json_str) for ws in clients),
            return_exceptions=True,
        )
        for ws, res in zip(clients, results):
            if isinstance(res, BaseException):
                self.disconnect(user_id, ws)

    def count(self) -> int:
        return sum(len(s) for s in self._clients.values())


# Shared singleton — imported by the trading router and the scheduler
account_ws_manager = AccountWSManager()
