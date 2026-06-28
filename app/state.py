from __future__ import annotations

import threading
from datetime import date
from typing import Dict, List, Optional, Set

from app.models import Candle, EntrySignal, Position, TradingPhase


class AppState:
    _instance: Optional["AppState"] = None
    _creation_lock = threading.Lock()

    def __new__(cls) -> "AppState":
        with cls._creation_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._init()
                cls._instance = obj
        return cls._instance

    def _init(self) -> None:
        # ── Session ───────────────────────────────────────────────────────────
        self.phase:        TradingPhase   = TradingPhase.PRE_MARKET
        self.trading_date: Optional[date] = None
        self.ws_status:    str            = "—"
        self.api_status:   str            = "—"

        # ── Universe & Watchlist ──────────────────────────────────────────────
        # Full NSE universe loaded from instrument master: list of StockInfo dicts
        self.full_universe:    List[dict]     = []
        # Gemini AI shortlist: list of trading symbols e.g. ["RELIANCE", "TCS"]
        self.gemini_shortlist: List[str]      = []
        # Active watchlist subscribed via WebSocket: {symbol: token}
        self.active_watchlist: Dict[str, str] = {}

        # ── Candle stores (symbol → list[Candle], capped at 300 bars) ─────────
        self.candles_5m: Dict[str, List[Candle]] = {}
        self.candles_1h: Dict[str, List[Candle]] = {}
        self.candles_1d: Dict[str, List[Candle]] = {}

        # NIFTY 50 candle series for index trend gate
        self.nifty_candles_1d: List[Candle] = []
        self.nifty_candles_5m: List[Candle] = []   # 5m bars for session VWAP

        # ── Live prices ───────────────────────────────────────────────────────
        self.ltp:       Dict[str, float] = {}   # symbol → latest LTP
        self.nifty_ltp: float            = 0.0

        # ── Positions ─────────────────────────────────────────────────────────
        self.positions:    Dict[str, Position] = {}   # symbol → Position
        self.traded_today: Set[str]            = set()
        self.daily_pnl:    float               = 0.0

        # ── Scan diagnostics ──────────────────────────────────────────────────
        self.last_scan_results: Dict[str, dict]   = {}
        self.pending_signals:   List[EntrySignal] = []
        self.last_5m_bar_time:  Optional[str]     = None   # "HH:MM" of last scanned bar

        # Thread-safe lock for candle writes from the WebSocket callback thread
        self._candle_lock = threading.Lock()


def get_state() -> AppState:
    return AppState()
