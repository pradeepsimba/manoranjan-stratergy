from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Candle:
    start_time: str  = ""
    open:       float = 0.0
    close:      float = 0.0
    high:       float = 0.0
    low:        float = 0.0
    volume:     float = 0.0

    def is_bullish(self) -> bool: return self.close > self.open
    def is_bearish(self) -> bool: return self.close < self.open
    def body(self)       -> float: return abs(self.close - self.open)
    def range(self)      -> float: return self.high - self.low


@dataclass
class ActiveTrade:
    type:       str    # "BUY" or "SELL"
    entry:      float
    entry_time: str
    confidence: str
    num_lots:   int
    current_sl: float = 0.0

    def __post_init__(self):
        import app.config as cfg
        if self.current_sl == 0.0:
            self.current_sl = (
                self.entry - cfg.STOPLOSS if self.type == "BUY"
                else self.entry + cfg.STOPLOSS
            )


@dataclass
class Trade:
    id:             int            = 0
    type:           str            = ""   # BUY / SELL / BUY_EXIT / SELL_EXIT
    price:          float          = 0.0
    time:           str            = ""
    confidence:     str            = ""
    pnl:            float          = 0.0
    option_premium: Optional[float] = None


@dataclass
class EmaStack:
    ema20:   float = 0.0
    ema50:   float = 0.0
    bullish: bool  = False
    bearish: bool  = False


@dataclass
class PatternMatch:
    stock:   str
    pattern: str


@dataclass
class LeaderPatterns:
    bull_count: int                    = 0
    bear_count: int                    = 0
    matches:    List[PatternMatch]     = field(default_factory=list)


@dataclass
class BNIndicators:
    rsi:        Optional[float]        = None
    macd_dir:   Optional[str]          = None
    macd_val:   Optional[float]        = None
    ema_stack:  Optional[EmaStack]     = None
    leader_pat: Optional[LeaderPatterns] = None
    bull:       float                  = 0.0
    bear:       float                  = 0.0
    bullish:    bool                   = False
    bearish:    bool                   = False
