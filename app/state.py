from __future__ import annotations

import threading
from typing import Dict, List, Optional

from app.models import BNDiagnostic, BNTrade, Candle, NFDiagnostic, NFTrade, TradingPhase


class AppState:
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
        self.phase:      TradingPhase = TradingPhase.PRE_MARKET
        self.ws_status:  str          = "—"
        self.api_status: str          = "—"

        # ── Candle stores — BankNifty index + the 12 BN stocks, all keyed by
        # TOKEN. Capped at 300 bars (deque maxlen set on assignment). ─────────
        self.candles_5m: Dict[str, List[Candle]] = {}
        self.tick_version: Dict[str, int] = {}
        self.bn_index_candles_5m: List[Candle] = []

        # ── Live prices (keyed by SYMBOL NAME; BankNifty index kept separately) ─
        self.ltp:          Dict[str, float] = {}
        self.bn_index_ltp: float            = 0.0

        # ── Synthetic BankNifty index (vendor stopped streaming the real index
        # under either the old or new protocol — see market_data.py's
        # _update_synthetic_index) — a one-way latch: flips to False forever
        # the moment a genuine index tick is ever seen again. bn_synthetic_anchor
        # is the running open-anchor for the CURRENT synthetic bar, seeded at
        # startup from the last real close in the self-recorded bn_index_bars
        # archive (see scheduler.py's startup sequence). ──────────────────────
        self.bn_index_synthetic: bool = True
        self.bn_synthetic_anchor: float = 0.0

        # ── The single active Bank Nifty options trade ────────────────────────
        self.active_trade:   Optional[BNTrade] = None
        self.closed_trades:  List[BNTrade]     = []   # today's closed trades
        self.last_trade_candle: Optional[str]  = None  # dedupe same-5m-bar re-entry
        self.last_exit_time: Optional[str]     = None  # ISO timestamp, 60s cooldown
        self.daily_pnl:      float             = 0.0
        # Running paper-account balance — persists ACROSS days (see database's
        # _BN_FUNDS key), unlike daily_pnl which resets every EOD.
        self.funds: float = 0.0

        # ── Latest entry-loop diagnostic ("why didn't it fire") for the dashboard ─
        self.bn_diagnostic: Optional[BNDiagnostic] = None
        self.last_evaluated_bar: Optional[str]     = None   # dedupe: one eval per closed bar

        # ── Live-price ticker push (100ms delta broadcast) ────────────────────
        self.dirty_ticks_push: set = set()

        # ── 15m support/resistance levels (Stock Candles panel only, unrelated
        # to the BN trading strategy) — refreshed every 5 min via a periodic
        # REST fetch (this app otherwise never streams 15m candles), keyed by
        # TOKEN. 5m S/R for the same panel is computed on the fly from
        # candles_5m/bn_index_candles_5m, no separate storage needed. ─────────
        self.sr_15m_levels: Dict[str, Dict[str, List[float]]] = {}

        # Per-token locks: each instrument's candle list gets its own lock so
        # WS tick writes and the tick loop don't contend across unrelated tokens.
        self._token_locks:      Dict[str, threading.Lock] = {}
        self._token_locks_meta: threading.Lock            = threading.Lock()
        self._bn_index_lock: threading.Lock = threading.Lock()

        # ── Nifty 50 — a second, independent instrument running in parallel
        # to BankNifty above. candles_5m/ltp (keyed by symbol string) are
        # SHARED across both instruments — no separate store needed there.
        # funds/daily_pnl are also SHARED (one paper account, two strategies)
        # — only per-instrument STRATEGY EXECUTION state is separate. ───────
        self.nf_index_candles_5m: List[Candle] = []
        self.nf_index_ltp:        float        = 0.0
        self.nf_index_synthetic:  bool         = True
        self.nf_synthetic_anchor: float        = 0.0
        self._nf_index_lock: threading.Lock = threading.Lock()

        self.active_trade_nf:      Optional[NFTrade] = None
        self.closed_trades_nf:     List[NFTrade]      = []
        self.last_trade_candle_nf: Optional[str]      = None
        self.last_exit_time_nf:    Optional[str]      = None
        self.nf_diagnostic:        Optional[NFDiagnostic] = None
        self.last_evaluated_bar_nf: Optional[str]     = None

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
