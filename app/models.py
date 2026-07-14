from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ── Enumerations ──────────────────────────────────────────────────────────────

class MarketPhase(Enum):
    PRE_MARKET = "pre_market"   # Before market open — no order placement
    OPEN       = "open"         # Market open — orders accepted, live ticks flow
    CLOSED     = "closed"       # After market close — MIS positions squared off


class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT  = "LIMIT"


class Product(Enum):
    CNC = "CNC"   # delivery — feeds Holdings, persists indefinitely
    MIS = "MIS"   # intraday — feeds Positions, auto-squared-off at end of day


class OrderStatus(Enum):
    PENDING   = "PENDING"
    COMPLETE  = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"


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
