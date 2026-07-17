from __future__ import annotations

import threading
from typing import Dict, List, Optional

from app.models import Candle, MarketPhase


class AppState:
    """
    Process-wide singleton holding SHARED market data only (candles, live
    prices, connection status). Per-user data (funds, holdings, positions,
    orders) is never cached here — it lives in Postgres and is read per
    request, since a process-wide singleton can't hold N users' account state.
    """

    _instance: Optional["AppState"] = None
    _creation_lock = threading.Lock()

    def __new__(cls) -> "AppState":
        # Double-checked locking: get_state() runs on the event loop AND from
        # the WS tick handler, so the steady-state path must not serialize.
        inst = cls._instance
        if inst is None:
            with cls._creation_lock:
                inst = cls._instance
                if inst is None:
                    inst = super().__new__(cls)
                    inst._init()
                    cls._instance = inst
        return inst

    def _init(self) -> None:
        # ── Session ───────────────────────────────────────────────────────────
        self.phase:      MarketPhase = MarketPhase.PRE_MARKET
        self.ws_status:  str         = "—"
        self.api_status: str         = "—"

        # ── Candle stores — every tradable instrument, keyed by TOKEN. Capped
        # at MAX_CANDLE_BUFFER bars (deque maxlen set on assignment). ─────────
        self.candles_5m:   Dict[str, List[Candle]] = {}
        self.tick_version: Dict[str, int]          = {}

        # ── Live prices, keyed by TOKEN ────────────────────────────────────────
        self.ltp: Dict[str, float] = {}

        # ── Level-1 market depth, keyed by TOKEN — parsed from the feed's
        # "snap" field (see MarketDataService._parse_snap): up to 5 bid/ask
        # levels plus last-trade qty, buy/sell qty, OI, and circuit limits.
        # Same "no lock, atomic single-key dict assignment" treatment as
        # `ltp` above — not protected by candle_lock. ─────────────────────────
        self.depth: Dict[str, dict] = {}

        # ── Live-price ticker push (100ms delta broadcast) ────────────────────
        self.dirty_ticks_push: set = set()

        # Per-token locks: each instrument's candle list gets its own lock so
        # WS tick writes and readers don't contend across unrelated tokens.
        self._token_locks:      Dict[str, threading.Lock] = {}
        self._token_locks_meta: threading.Lock            = threading.Lock()

    def candle_lock(self, token: str) -> threading.Lock:
        """Return (and lazily create) the per-token candle lock."""
        try:
            return self._token_locks[token]
        except KeyError:
            with self._token_locks_meta:
                if token not in self._token_locks:
                    self._token_locks[token] = threading.Lock()
                return self._token_locks[token]


def get_state() -> AppState:
    return AppState()
