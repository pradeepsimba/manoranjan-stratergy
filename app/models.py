from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple, Optional, Tuple


# ── Enumerations ──────────────────────────────────────────────────────────────

class TradingPhase(Enum):
    PRE_MARKET = "pre_market"   # Before 09:00 — idle
    WAIT_ZONE  = "wait_zone"    # 09:15–09:45 — init, no scans
    ACTIVE     = "active"       # 09:45–14:30 — scanning and trading
    CUTOFF     = "cutoff"       # 14:30–15:30 — no new entries; OCO exits manage positions
    CLOSED     = "closed"       # After 15:30 — session terminated


class PositionStatus(Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"


# Which strategy opened a position. "core" = the 8-condition indicator strategy;
# "scalp" = the order-book/tape scalper (app/engine/scalper.py). Both books live
# in the SAME AppState.positions dict (so exits, DB persistence, the dashboard,
# EOD square-off and restart recovery are shared, unforked machinery) — this tag
# is what lets the scalper apply its own concurrency cap, loss limit, re-entry
# rule and square-off time to only its own trades.
STRATEGY_CORE  = "core"
STRATEGY_SCALP = "scalp"


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

    def is_bullish(self) -> bool: return self.close > self.open
    def is_bearish(self) -> bool: return self.close < self.open


# ── Indicators ────────────────────────────────────────────────────────────────

@dataclass(slots=True)   # built on every scan (dozens/sec across the pool)
class IndicatorResult:
    # RSI
    rsi:                Optional[float] = None
    rsi_above_30:       bool            = False
    rsi_rising:         bool            = False   # rose each of last 3 bars

    # MACD
    macd_line:          Optional[float] = None
    macd_signal_line:   Optional[float] = None
    macd_histogram:     Optional[float] = None
    macd_bullish_cross: bool             = False  # line just crossed above signal

    # ADX
    adx:      Optional[float] = None
    plus_di:  Optional[float] = None
    minus_di: Optional[float] = None
    adx_ok:   bool             = False   # ADX > 20 AND +DI > -DI

    # VWAP
    vwap:             float = 0.0
    price_above_vwap: bool  = False

    # Volume
    avg_volume_20: float = 0.0
    volume_surge:  bool  = False   # latest bar volume > 1.5× avg

    # Structural support
    support_level: float = 0.0
    near_support:  bool  = False   # price within 0.5% of swing low

    # Candlestick
    candle_pattern:  Optional[str] = None
    bullish_pattern: bool          = False

    # Raw price/volume context — lets the custom-rule engine express
    # price-relative clauses (e.g. "within 0.5% of VWAP", "volume 2× average").
    ltp:          float           = 0.0    # close of the bar the scan ran on
    volume_ratio: Optional[float] = None   # latest bar volume ÷ volume MA


@dataclass(slots=True)   # built on every gated scan, live and backtest
class TrendGate:
    daily_green:       bool = False   # stock LTP > today's daily open
    hourly_green:      bool = False   # current 1H candle close > open
    nifty_daily_green: bool = False   # NIFTY LTP > NIFTY daily open
    nifty_above_vwap:  bool = False   # NIFTY LTP > NIFTY session VWAP
    # NOTE: gate PASS/FAIL decisions live in trend_filter.trend_blockers(),
    # which applies the runtime GATE_* toggles — don't add a hard-coded
    # conjunction here; it would silently re-enable disabled gates.


# ── Order book / tape (scalper) ────────────────────────────────────────────────

class BookLevel(NamedTuple):
    """One order-book level. `orders` is None when the feed doesn't carry a
    per-level order COUNT (the anti-spoofing filter then can't run — see
    OrderBook.orders_seen)."""
    price:  float
    qty:    int
    orders: Optional[int] = None


class TapeEvent(NamedTuple):
    """
    One traded-volume observation, appended per WS tick by market_data.

    `qty` is the traded quantity since the previous tick, taken from the FORMING
    candle's volume delta — always available whatever the snap format, and it
    aggregates every print in the interval where the feed's LTQ field (used as
    the fallback when there is no delta) describes only the most recent one.
    `bid`/`ask` are the book at that instant, so the classifier can tell an
    ask-hitting (aggressive buy) print from a bid-hitting one; 0.0 when unknown.
    """
    ts:    float   # time.monotonic()
    price: float
    qty:   float
    bid:   float
    ask:   float


@dataclass(slots=True)
class OrderBook:
    """
    Parsed 5-level book for one symbol (see app/engine/orderbook.py).

    Written by the WS thread, read lock-free by the scalp engine on the event
    loop: readers always take a whole-object reference out of AppState.book, and
    the writer publishes a NEW instance per change (atomic dict swap, the same
    GIL-safe pattern as AppState.ltp/depth). `ts` is the one field mutated in
    place — a lone float write, refreshed when an unchanged snap re-confirms the
    book so the staleness guard doesn't reject a quiet-but-live book.
    """
    bids:        Tuple[BookLevel, ...] = ()
    asks:        Tuple[BookLevel, ...] = ()
    ltp:         float                 = 0.0   # LTP carried in the snap itself
    ltq:         Optional[int]         = None  # last traded qty, when published
    buy_qty:     Optional[int]         = None  # exchange total buy quantity
    sell_qty:    Optional[int]         = None  # exchange total sell quantity
    orders_seen: bool                  = False # any per-level order count parsed
    ts:          float                 = 0.0   # time.monotonic() of last confirm

    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    def spread(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return round(self.asks[0].price - self.bids[0].price, 4)


@dataclass(slots=True)
class ScalpDecision:
    """
    Outcome of one order-book/tape evaluation. `ok=False` carries the FIRST
    failing filter in `reason` (checks run cheapest-first), and `metrics` is
    always populated with whatever was computed before the veto so the dashboard
    can show why a symbol keeps getting rejected.
    """
    ok:      bool
    reason:  str  = ""
    metrics: dict = field(default_factory=dict)


@dataclass(slots=True)
class ScalpSession:
    """Which time-of-day window the exchange clock is in, and the imbalance
    threshold that window demands (see scalper.session_profile)."""
    window:         str    # closed | warmup | morning | midday | afternoon | squareoff
    execute:        bool   # False = scan/diagnose only, never place an order
    required_ratio: float  # weighted bid ÷ ask ratio an entry must clear
    note:           str = ""


# ── Trading ───────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class EntrySignal:
    symbol:         str
    token:          str
    ltp:            float
    support:        float
    sl_offset:      float          # entry − support (stop distance)
    target_offset:  float          # sl_offset × RR_RATIO (target distance)
    quantity:       int
    capital_needed: float          # quantity × entry / LEVERAGE
    indicators:     IndicatorResult = field(default_factory=IndicatorResult)
    trend:          TrendGate       = field(default_factory=TrendGate)
    bar_time:       str             = ""   # "HH:MM" of the triggering 5m bar
    strategy:       str             = STRATEGY_CORE
    # Scalper-only diagnostics (W-OBI ratio, tape volume, spread …) — empty for
    # core signals. Carried so the fill log / dashboard can show WHY it fired.
    scalp:          dict            = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    symbol:        str
    token:         str
    entry_price:   float
    entry_time:    str             # ISO timestamp string
    quantity:      int
    stop_loss:     float           # absolute price level
    target:        float           # absolute price level
    sl_offset:     float           # stop distance from entry
    target_offset: float           # target distance from entry
    order_id:      str
    status:        PositionStatus  = PositionStatus.OPEN
    exit_price:    Optional[float] = None
    exit_time:     Optional[str]   = None
    pnl:           float           = 0.0
    indicators:    Optional[IndicatorResult] = None
    trend:         Optional[TrendGate]       = None
    # STRATEGY_CORE | STRATEGY_SCALP — persisted (positions.strategy) so a
    # mid-session restart restores each book to the engine that owns it.
    strategy:      str             = STRATEGY_CORE
    # time.monotonic() of the fill, for the scalper's max-hold time stop. NOT
    # persisted: monotonic values are meaningless across processes, so a restored
    # scalp position falls back to the session square-off / SL / target only.
    opened_at:     Optional[float] = None
