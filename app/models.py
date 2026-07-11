from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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
