from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Deque, Dict, List, Optional, Set, Tuple

import app.config as cfg
from app.models import Candle, OrderBook, Position, TapeEvent, TradingPhase

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
        # In BOTH screen modes this is the list that ended up TRADEABLE (the
        # dashboard's AI marker), so daily_stats keeps the same meaning.
        self.gemini_shortlist: List[str]      = []
        # Symbols the AI flagged as RISKY and that were therefore removed from
        # the tradeable list — only populated when GEMINI_MODE is exclude_risky.
        # Kept separate from the shortlist so the dashboard can show WHAT was
        # excluded rather than only what survived.
        self.gemini_excluded:  List[str]      = []
        # Active watchlist (Gemini AI-selected): {symbol: token} — trading subset
        self.active_watchlist: Dict[str, str] = {}
        # Full pre-Gemini watchlist: {symbol: token} — all high-volume stocks
        self.full_watchlist:   Dict[str, str] = {}
        # Reverse map {token: symbol} for the FULL watchlist — lets the tick loop
        # iterate the dirty-token set directly without scanning the whole dict.
        self.token_to_name:    Dict[str, str] = {}

        # NIFTY 50's real trading_symbol, resolved from the client-status row by
        # fetch_active_watchlist() (see its docstring). None until the first
        # successful fetch — nifty_token() below falls back to the static
        # cfg.NIFTY50_TOKEN default until then.
        self.nifty_symbol: Optional[str] = None

        # ── Candle stores (symbol → list[Candle], capped at 300 bars) ─────────
        self.candles_5m: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}

        # Monotonic per-token counter, bumped once per accepted 5m candle
        # upsert (same lock as the mutation — see market_data._process_tick).
        # Lets a reader cheaply detect "this token's 5m data hasn't changed
        # since I last looked" without re-walking/re-resampling candles_5m.
        self.tick_version: Dict[str, int] = {}

        # NIFTY 50 session 5m bars — index trend gate + session VWAP.
        # deque(maxlen=...), not a plain list: market_data._upsert_list used to
        # evict the oldest bar with lst.pop(0) once this passed MAX_CANDLE_BUFFER,
        # an O(n) shift-the-whole-list operation on the hot tick path - every
        # other candle store already used deque(maxlen=...) for O(1) eviction,
        # this one was just inconsistent with the rest.
        self.nifty_candles_5m: Deque[Candle] = deque(maxlen=cfg.MAX_CANDLE_BUFFER)

        # ── Live prices ───────────────────────────────────────────────────────
        self.ltp:       Dict[str, float] = {}   # symbol → latest LTP
        self.nifty_ltp: float            = 0.0
        # Order book depth — written by WS thread, read by scan workers.
        # GIL-protected dict ops (same pattern as ltp) make this safe in CPython.
        self.depth: Dict[str, dict] = {}        # symbol → {bid,ask,spread,buy_qty,sell_qty,ratio}

        # ── Scalper: full order book + tape (symbol-keyed) ────────────────────
        # Populated ONLY for tradeable symbols while SCALP_ENABLED is on — the
        # 5-level parse is more regex work than the legacy L1 `depth` parse
        # above, and the universe can be ~10,000 symbols. `depth` is left
        # completely untouched by the scalper so the existing depth_bullish
        # condition and the indicators page can't regress.
        # Written by the WS thread, read by the event loop; both use whole-object
        # atomic swaps (new OrderBook / new tape tuple per update), the same
        # GIL-safe pattern as ltp/depth — see OrderBook and orderbook.append_tape.
        self.book: Dict[str, OrderBook]           = {}   # symbol → parsed 5-level book
        self.tape: Dict[str, Tuple[TapeEvent, ...]] = {}  # symbol → recent prints
        # symbol → (bar start_time, that bar's cumulative volume) as of the
        # previous tick. The PAIR matters: the tape's traded-quantity source is
        # the volume delta within one bar, so the reader must know which bar the
        # baseline belongs to (see market_data._process_tick).
        self.last_bar_volume: Dict[str, Tuple[str, float]] = {}

        # Tokens that ticked since the scalp engine's last cycle. A THIRD dirty
        # set (alongside dirty_ticks / dirty_ticks_push) because each consumer
        # swaps-and-clears its own: sharing one would mean whichever loop ran
        # first stole the other's ticks.
        self.dirty_ticks_scalp: Set[str] = set()

        # Scalp book state. Realized scalp-only P&L (its own loss limit),
        # per-symbol and total trade counts (churn caps), and monotonic exit
        # timestamps (re-entry cooldown). Mutated on the event loop only.
        self.scalp_pnl:            float            = 0.0
        self.scalp_trades_today:   Dict[str, int]   = {}
        self.scalp_last_exit:      Dict[str, float] = {}   # symbol → time.monotonic()
        # Rolling diagnostics for /api/scalp + the dashboard: the most recent
        # decisions (fired, dry-run, and rejected-with-reason). Bounded deque —
        # this is a debugging aid, not a trade log (the DB holds those).
        self.scalp_log:            Deque[dict]      = deque(maxlen=60)
        # Latest resolved ScalpSession (window/execute/required ratio) so the API
        # and dashboard report exactly what the engine is acting on. Typed loosely
        # to keep app.state free of a dependency on the scalper's own module.
        self.scalp_session:        Optional[object] = None
        # Date ("YYYY-MM-DD") whose scalp square-off already ran, so the
        # per-cycle check flattens once instead of every 100ms after 14:45.
        self.scalp_squareoff_date: Optional[str]    = None

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
        # Wall-clock (time.monotonic()) of the last accepted stock 5m WS tick, regardless of its
        # bar content - distinct from last_5m_bar_time (which only advances forward and describes
        # the DATA, not receipt time). Lets the tick loop detect a feed that's gone silent even
        # though the WS socket itself still reports "connected" (see feed_stale_warning below).
        self.last_tick_wallclock: Optional[float] = None
        # Set/cleared by the ACTIVE-phase tick loop; None while the feed is healthy. Surfaced on the
        # dashboard alongside ws_status, which only reflects socket-level health, not whether ticks
        # are actually arriving.
        self.feed_stale_warning: Optional[str] = None

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

    def reset_scalp_state(self) -> None:
        """
        Daily reset of every scalper-owned structure (called from EOD).

        Kept separate from clear_scan_results so it is obvious what belongs to
        the scalper. All of it MUST be cleared: a surviving `book`/`tape` would
        let tomorrow's first cycle evaluate yesterday's liquidity (the staleness
        guard uses monotonic time, which does NOT reset with the trading day),
        and surviving trade counts / cooldowns would silently ration tomorrow's
        entries.
        """
        self.book.clear()
        self.tape.clear()
        self.last_bar_volume.clear()
        self.dirty_ticks_scalp.clear()
        self.scalp_pnl = 0.0
        self.scalp_trades_today.clear()
        self.scalp_last_exit.clear()
        self.scalp_log.clear()
        self.scalp_session        = None
        self.scalp_squareoff_date = None


def get_state() -> AppState:
    return AppState()


def nifty_token() -> str:
    """
    NIFTY 50's real trading_symbol as stored by the live-ingestion process
    (app_historical_data.stock_symbol), resolved from the client-status
    response by fetch_active_watchlist(). cfg.NIFTY50_TOKEN is that index's
    AngelOne *exchange token* ("99926000"), not its trading_symbol — the two
    are different fields and only the latter is what the historical-data
    REST/WS API and the DB actually key rows by (see CLAUDE.md's NIFTY50_TOKEN
    caveat). Falls back to the static default before the first successful
    watchlist fetch, or if the server never reports a NIFTY 50 row.
    """
    return get_state().nifty_symbol or cfg.NIFTY50_TOKEN
