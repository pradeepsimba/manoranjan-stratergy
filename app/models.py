from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ── Enumerations ──────────────────────────────────────────────────────────────

class TradingPhase(Enum):
    PRE_MARKET = "pre_market"   # Before 09:00 — idle
    WAIT_ZONE  = "wait_zone"    # 09:15–09:30 — init, no scans
    ACTIVE     = "active"       # 09:30–15:00 — scanning and trading
    CUTOFF     = "cutoff"       # 15:00–15:30 — no new entries; exit management continues
    CLOSED     = "closed"       # After 15:30 — session terminated


class PositionStatus(Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"


# ── Market Data ───────────────────────────────────────────────────────────────
# Candle format matches the custom server response exactly.
# Field order kept for API compatibility; always use keyword access.

@dataclass(slots=True)   # created on every tick + every historical bar — slots
class Candle:            # cuts per-instance memory ~40% and speeds attribute access
    start_time: str   = ""
    open:       float = 0.0
    close:      float = 0.0
    high:       float = 0.0
    low:        float = 0.0
    volume:     float = 0.0
    # Real per-trade quantity of the tick that produced/updated this bar,
    # parsed from the feed's `quote` text (e.g. "...qty 91..."). Historical
    # REST bars never carry this (only live WS ticks do) — defaults to 0.
    last_qty:   float = 0.0
    # Cumulative pending buy/sell order quantity at the moment of this tick,
    # parsed from the feed's `snap` text (e.g. "...BuyQty 1111915 SellQty
    # 1944411..."). Same live-WS-only availability as last_qty above —
    # historical REST bars never carry this, defaults to 0.
    buy_qty:    float = 0.0
    sell_qty:   float = 0.0

    def is_bullish(self) -> bool: return self.close > self.open
    def is_bearish(self) -> bool: return self.close < self.open


# ── Bank Nifty options strategy ───────────────────────────────────────────────

@dataclass(slots=True)   # built once per fired entry, live and backtest
class BNSignal:
    direction:         str            # "BUY" (-> long ATM CE) | "SELL" (-> long ATM PE)
    entry_index_price: float          # BankNifty spot at signal
    bar_time:          str            # start_time of the triggering 5m bar
    confidence:        float          # 0-100, leader-vote + qty-surge breadth
    green:             int            # leader stocks closing green
    red:               int            # leader stocks closing red
    strong_qty:        int            # leader stocks with a volume-surge bar
    leader_signal:     str            # "BUY" | "SELL" | "Nobuysell"
    bn_bull:           float         # composite indicator bull score
    bn_bear:           float         # composite indicator bear score
    strike:            int            # ATM strike at signal time
    expiry:            str            # ISO datetime of the option's weekly expiry
    entry_premium:     float          # theoretical Black-Scholes premium at signal
    iv_used:           float          # realized-vol estimate used for the premium


@dataclass(slots=True)   # the single active trade — at most one at a time
class BNTrade:
    direction:    str             # "BUY" | "SELL"
    entry_index_price: float
    entry_time:   str
    target:       float           # absolute BankNifty index price, frozen at entry
    current_sl:   float           # absolute BankNifty index price — ratchets over time
    strike:       int
    option_type:  str             # "CE" | "PE"
    expiry:       str             # ISO datetime
    entry_premium: float
    # Risk parameters frozen from cfg AT ENTRY — a live Settings change must
    # never retroactively alter an already-open trade's SL/target economics.
    stoploss_points:   float = 0.0
    breakeven_trigger: float = 0.0
    trail_trigger:     float = 0.0
    trail_distance:    float = 0.0
    lot_size:     int             = 30
    order_id:     str             = ""
    sl_stage:     str             = "Initial"   # "Initial" | "Breakeven" | "Trail"
    current_premium: float        = 0.0    # live mark, refreshed every exit-check tick
    current_iv:      float        = 0.0
    status:       PositionStatus  = PositionStatus.OPEN
    exit_index_price: Optional[float] = None
    exit_time:        Optional[str]   = None
    exit_premium:     Optional[float] = None
    pnl:              float           = 0.0     # ₹, from (exit_premium-entry_premium)*lot_size
    index_pnl_points:  float           = 0.0     # diagnostic only — never used for settlement
    confidence:        float           = 0.0
    entry_signal:      Optional[BNSignal] = None


# ── Nifty 50 options strategy — parallel to the BN dataclasses above, same
# field shapes (kept as distinct classes, not a shared base, matching this
# repo's convention of explicit duplication over inheritance). ──────────────

@dataclass(slots=True)
class NFSignal:
    direction:         str
    entry_index_price: float
    bar_time:          str
    confidence:        float
    green:             int
    red:               int
    strong_qty:        int
    leader_signal:     str
    bn_bull:           float
    bn_bear:           float
    strike:            int
    expiry:            str
    entry_premium:     float
    iv_used:           float


@dataclass(slots=True)   # the single active Nifty 50 trade — at most one at a time
class NFTrade:
    direction:    str
    entry_index_price: float
    entry_time:   str
    target:       float
    current_sl:   float
    strike:       int
    option_type:  str
    expiry:       str
    entry_premium: float
    stoploss_points:   float = 0.0
    breakeven_trigger: float = 0.0
    trail_trigger:     float = 0.0
    trail_distance:    float = 0.0
    lot_size:     int             = 65
    order_id:     str             = ""
    sl_stage:     str             = "Initial"
    current_premium: float        = 0.0
    current_iv:      float        = 0.0
    status:       PositionStatus  = PositionStatus.OPEN
    exit_index_price: Optional[float] = None
    exit_time:        Optional[str]   = None
    exit_premium:     Optional[float] = None
    pnl:              float           = 0.0
    index_pnl_points:  float           = 0.0
    confidence:        float           = 0.0
    entry_signal:      Optional[NFSignal] = None


@dataclass(slots=True)
class NFDiagnostic:
    time:            str
    bn_ltp:          float
    green:           int
    red:             int
    strong_qty:      int
    leader_rows:     List[dict] = field(default_factory=list)
    leader_signal:   str        = "Nobuysell"
    sideways_range:  Optional[float] = None
    momentum_ok:     bool             = False
    momentum_reason: str              = ""
    rsi:             Optional[float]  = None
    macd_dir:        Optional[str]    = None
    macd_val:        Optional[float]  = None
    ema_bullish:     Optional[bool]   = None
    ema_bearish:     Optional[bool]   = None
    bn_bull:         float            = 0.0
    bn_bear:         float            = 0.0
    bn_bullish:      bool             = False
    bn_bearish:      bool             = False
    no_trade_reason: Optional[str]    = None
    candle_close_ok: bool             = True
    cooldown_ms:     float            = 0.0
    market_open:     bool             = True
    atm_strike:      Optional[int]    = None
    atm_premium:     Optional[float]  = None
    atm_iv:          Optional[float]  = None
    cooldown_ok:      bool = True
    sideways_ok:      bool = False
    dir_count_ok:     bool = False
    qty_surge_ok:     bool = False
    same_direction_required: int = 0
    gates_clear:      bool = False
    entry_ready:      bool = False


@dataclass(slots=True)   # rebuilt every ~100ms tick for the dashboard's "why didn't it fire" panel
class BNDiagnostic:
    time:            str
    bn_ltp:          float
    green:           int
    red:             int
    strong_qty:      int
    leader_rows:     List[dict] = field(default_factory=list)
    leader_signal:   str        = "Nobuysell"
    sideways_range:  Optional[float] = None
    momentum_ok:     bool             = False
    momentum_reason: str              = ""
    rsi:             Optional[float]  = None
    macd_dir:        Optional[str]    = None
    macd_val:        Optional[float]  = None
    ema_bullish:     Optional[bool]   = None
    ema_bearish:     Optional[bool]   = None
    bn_bull:         float            = 0.0
    bn_bear:         float            = 0.0
    bn_bullish:      bool             = False
    bn_bearish:      bool             = False
    no_trade_reason: Optional[str]    = None
    candle_close_ok: bool             = True
    cooldown_ms:     float            = 0.0
    market_open:     bool             = True
    atm_strike:      Optional[int]    = None
    atm_premium:     Optional[float]  = None
    atm_iv:          Optional[float]  = None
    # Per-gate pass/fail, for the dashboard's Entry Loop Monitor (c.html-style
    # explicit ✔/✘ per row) — mirrors the same intermediate booleans
    # evaluate_entry already computes to build no_trade_reason/gates_clear,
    # just exposed individually instead of collapsed into one reason string.
    cooldown_ok:      bool = True
    sideways_ok:      bool = False
    dir_count_ok:     bool = False
    qty_surge_ok:     bool = False
    same_direction_required: int = 0
    gates_clear:      bool = False
    entry_ready:      bool = False
