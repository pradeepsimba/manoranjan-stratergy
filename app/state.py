from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import app.config as cfg
from app.models import ActiveTrade, BNIndicators, Candle, Trade


# ── Value objects ──────────────────────────────────────────────────────────────

@dataclass
class PendingSignal:
    type:   str
    reason: str


@dataclass
class SRLevels:
    supports:    List[float]
    resistances: List[float]


@dataclass
class MomResult:
    ok:     bool
    reason: str


@dataclass
class StockStat:
    stock:     str
    candle:    Optional[Candle]
    qty:       float
    threshold: float


@dataclass
class EntryDiagnostics:
    market_open:           bool
    time_window_ok:        bool
    no_active_trade:       bool
    cooldown_ms:           float
    sideways_range:        Optional[float]
    candle_close_ok:       bool
    leader_signal_type:    str
    leader_signal_reason:  str
    green:                 int
    red:                   int
    strong_qty:            int
    already_traded_candle: bool
    bn_ind:                Optional[BNIndicators]
    bn_candle:             Optional[Candle]
    stocks:                List[StockStat]
    momentum:              Optional[MomResult]
    candle_close_time:     Optional[str]


# ── Singleton state ────────────────────────────────────────────────────────────

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
        # symbol → list[Candle], selected interval only (up to 200)
        self.last_n_candles: Dict[str, List[Candle]] = {}

        # interval → symbol → list[Candle] (up to 5, for multi-frame display)
        self.all_interval_candles: Dict[str, Dict[str, List[Candle]]] = {}

        # BankNifty candles kept for indicator calcs (up to 300)
        self.bn_indicator_candles: List[Candle] = []
        self._bn_ind_lock = threading.Lock()

        self.selected_interval: str  = "5m"
        self.num_candles:       int  = 3
        self.candle_offset:     int  = 0

        self.current_candle_time: Optional[str] = None
        self.signal_locked:       bool           = False

        self.active_trade:     Optional[ActiveTrade]   = None
        self.last_trade_candle: Optional[str]          = None
        self.last_exit_time:   float                   = 0.0  # monotonic seconds
        self.pending_signal:   Optional[PendingSignal] = None

        self.available_funds: float = cfg.DEFAULT_FUNDS

        # per-stock live quantities (updated every tick)
        self.latest_minute_qty: Dict[str, float] = {}
        self.latest_buy_qty:    Dict[str, int]   = {}
        self.latest_sell_qty:   Dict[str, int]   = {}

        # S/R levels per stock, per timeframe
        self.sr5m:  Dict[str, SRLevels] = {}
        self.sr15m: Dict[str, SRLevels] = {}

        self.api_status: str = "—"
        self.ws_status:  str = "—"

        self.bn_ltp:        float                       = 0.0
        self.bn_indicators: Optional[BNIndicators]      = None
        self.global_signal: str                         = "NEUTRAL"

        self.big_trades_snapshot: Optional[Any]          = None
        self.entry_diagnostics:   Optional[EntryDiagnostics] = None


def get_state() -> AppState:
    return AppState()
