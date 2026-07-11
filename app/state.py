from __future__ import annotations

import asyncio
import threading
from typing import Dict, List, Optional, Set

from app.models import Candle, Position, TradingPhase

# Strong references to fire-and-forget tasks. The event loop keeps only WEAK
# refs to tasks, so an unreferenced create_task() result can be garbage-
# collected mid-flight (per the asyncio docs) — e.g. a long backtest silently
# stopping with its DB row stuck on 'running'. spawn() pins the task until done.
_bg_tasks: Set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """create_task + keep a strong reference until the task completes."""
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


class AppState:
    _instance: Optional["AppState"] = None
    _creation_lock = threading.Lock()

    def __new__(cls) -> "AppState":
        # Double-checked locking: get_state() runs in every scan worker on every
        # tick, so the steady-state path must not serialize threads on a lock.
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
        self.phase:      TradingPhase = TradingPhase.PRE_MARKET
        self.ws_status:  str          = "—"
        self.api_status: str          = "—"

        # ── Universe & Watchlist ──────────────────────────────────────────────
        # Gemini AI shortlist: list of trading symbols e.g. ["RELIANCE", "TCS"]
        self.gemini_shortlist: List[str]      = []
        # Active watchlist (Gemini AI-selected): {symbol: token} — trading subset
        self.active_watchlist: Dict[str, str] = {}
        # Full pre-Gemini watchlist: {symbol: token} — all high-volume stocks
        self.full_watchlist:   Dict[str, str] = {}
        # Reverse map {token: symbol} for the FULL watchlist — lets the tick loop
        # iterate the dirty-token set directly without scanning the whole dict.
        self.token_to_name:    Dict[str, str] = {}

        # ── Candle stores (symbol → list[Candle], capped at 300 bars) ─────────
        self.candles_5m: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}

        # Monotonic per-token counter, bumped once per accepted 5m candle
        # upsert (same lock as the mutation — see market_data._process_tick).
        # Lets a reader cheaply detect "this token's 5m data hasn't changed
        # since I last looked" without re-walking/re-resampling candles_5m.
        self.tick_version: Dict[str, int] = {}

        # NIFTY 50 session 5m bars — index trend gate + session VWAP
        self.nifty_candles_5m: List[Candle] = []

        # ── Live prices ───────────────────────────────────────────────────────
        self.ltp:       Dict[str, float] = {}   # symbol → latest LTP
        self.nifty_ltp: float            = 0.0
        # Order book depth — written by WS thread, read by scan workers.
        # GIL-protected dict ops (same pattern as ltp) make this safe in CPython.
        self.depth: Dict[str, dict] = {}        # symbol → {bid,ask,spread,buy_qty,sell_qty,ratio}

        # ── Positions ─────────────────────────────────────────────────────────
        # `positions` holds ONLY currently-open trades, so len() is a true
        # concurrent count. Closed trades move to `closed_positions` for the
        # day's log / dashboard.
        self.positions:        Dict[str, Position] = {}   # symbol → OPEN Position
        self.closed_positions: List[Position]      = []   # today's CLOSED trades
        self.traded_today:     Set[str]            = set()
        self.daily_pnl:        float               = 0.0

        # ── Scan diagnostics ──────────────────────────────────────────────────
        # Written by scan worker threads, read by the event loop (dashboard /
        # API), so all access goes through the lock below to avoid a
        # "dict changed size during iteration" race.
        self.last_scan_results: Dict[str, dict] = {}
        self._scan_results_lock: threading.Lock = threading.Lock()
        self.last_5m_bar_time:  Optional[str]   = None   # "HH:MM" of last scanned bar

        # Per-symbol indicator snapshot — written by scan workers on every tick,
        # read by the event loop for the WebSocket broadcast. GIL-protected dict
        # ops make this safe in CPython without an extra lock (same as ltp).
        self.indicator_snapshot: Dict[str, dict] = {}

        # ── Tick-wise engine ──────────────────────────────────────────────────
        # Tokens that received a tick since the last evaluation cycle. The WS
        # thread adds; the tick loop swaps it out and evaluates those stocks.
        self.dirty_ticks: Set[str] = set()
        self.dirty_ticks_push: Set[str] = set()

        # Per-token locks: each symbol's candle list gets its own lock so WS
        # tick writes and ThreadPoolExecutor scan reads don't contend across
        # unrelated stocks.
        self._token_locks:      Dict[str, threading.Lock] = {}
        self._token_locks_meta: threading.Lock            = threading.Lock()

        # Separate lock for the shared NIFTY candle lists.
        self._nifty_lock: threading.Lock = threading.Lock()

    def candle_lock(self, token: str) -> threading.Lock:
        """Return (and lazily create) the per-token candle lock."""
        try:
            return self._token_locks[token]
        except KeyError:
            with self._token_locks_meta:
                if token not in self._token_locks:
                    self._token_locks[token] = threading.Lock()
                return self._token_locks[token]

    # ── Scan-results access (thread-safe) ──────────────────────────────────────
    def record_scan(self, symbol: str, result: dict) -> None:
        """Worker-thread write of a per-stock scan diagnostic."""
        with self._scan_results_lock:
            # Pop-then-insert so dict order == recency: re-assigning an existing
            # key keeps its old position, which would freeze the dashboard's
            # [-N:] "most recent scans" slice on the first-inserted symbols.
            self.last_scan_results.pop(symbol, None)
            self.last_scan_results[symbol] = result

    def scan_snapshot(self) -> list:
        """Event-loop read: a consistent (symbol, result) list snapshot."""
        with self._scan_results_lock:
            return list(self.last_scan_results.items())

    def clear_scan_results(self) -> None:
        with self._scan_results_lock:
            self.last_scan_results.clear()
            self.indicator_snapshot.clear()
            self.depth.clear()


def get_state() -> AppState:
    return AppState()
