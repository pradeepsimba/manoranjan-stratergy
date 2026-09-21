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
    strike:            int            # deep-ITM strike at signal time (2026-09-21 — was ATM)
    expiry:            str            # ISO datetime of the option's expiry
    entry_premium:     float          # theoretical Black-Scholes premium at signal
    iv_used:           float          # realized-vol estimate used for the premium
    basket_score:      float = 0.0    # Top-8 weighted-basket composite score that fired this signal
    wobi:              float = 0.0    # W-OBI value that cleared the execution filter


@dataclass(slots=True)   # the single active trade — at most one at a time
class BNTrade:
    direction:    str             # "BUY" | "SELL"
    entry_index_price: float
    entry_time:   str
    # target/current_sl are now absolute OPTION PREMIUM levels (₹), not
    # BankNifty index prices — see the 2026-09-21 scalp-strategy rewrite
    # (bn_entry_exit.evaluate_exit). Frozen at entry: target = entry_premium
    # + target_rs, current_sl = entry_premium - stop_rs; current_sl never
    # ratchets for this strategy (no breakeven/trailing — hard bracket +
    # time-stop only), but the field is kept mutable/named as-is for
    # BTPosition duck-typing compatibility (see CLAUDE.md's "shared decision
    # core" convention — evaluate_exit reads these same field names off
    # either dataclass).
    target:       float
    current_sl:   float
    strike:       int
    option_type:  str             # "CE" | "PE"
    expiry:       str             # ISO datetime
    entry_premium: float
    # Risk parameters frozen from cfg AT ENTRY — a live Settings change must
    # never retroactively alter an already-open trade's SL/target economics.
    # breakeven_trigger/trail_trigger/trail_distance are VESTIGIAL for this
    # strategy (no ratcheting stop any more) — kept, always 0.0, purely so
    # evaluate_exit can stay duck-type-compatible with any code that still
    # constructs a trade the old way; stoploss_points is likewise unused in
    # favor of stop_rs below (see "Static: Scalping strategy" in config.py).
    stoploss_points:   float = 0.0
    breakeven_trigger: float = 0.0
    trail_trigger:     float = 0.0
    trail_distance:    float = 0.0
    # 12-second execution lifecycle — frozen from cfg.BN_SCALP_TARGET_RS/
    # STOP_RS/TIME_STOP_S at entry, same "no retroactive Settings change"
    # rule as every other frozen risk param above.
    target_rs:         float = 0.0
    stop_rs:            float = 0.0
    time_stop_s:        float = 0.0
    # Diagnostic snapshot of what fired this trade — never used for
    # settlement, purely for the dashboard/trade log.
    basket_score_at_entry: float = 0.0
    wobi_at_entry:         float = 0.0
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
    # Real-option-LTP feature (2026-09-17, live-only — see app/services/
    # bn_trade.py and market_data.py's set_bn_option_symbol). option_symbol
    # is the vendor instrument this trade's option leg was subscribed under
    # at entry (e.g. "BANKNIFTY17SEP56400CE"); premium_synthetic is a
    # one-way latch, True until the first real WS tick for that symbol
    # arrives, after which current_premium/exit_premium use real LTP instead
    # of the Black-Scholes value. entry_premium above is ALWAYS the
    # Black-Scholes value regardless (no real tick can exist yet at the
    # exact instant a trade opens — see the design discussion this was
    # decided in). Backtest's BTPosition has no equivalent fields; this only
    # ever applies to a live BNTrade.
    option_symbol:      str  = ""
    premium_synthetic:  bool = True


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
    basket_score:      float = 0.0
    wobi:              float = 0.0


@dataclass(slots=True)   # the single active Nifty 50 trade — at most one at a time
class NFTrade:
    direction:    str
    entry_index_price: float
    entry_time:   str
    target:       float           # absolute PREMIUM level (₹) — see BNTrade's comment above
    current_sl:   float           # absolute PREMIUM level (₹)
    strike:       int
    option_type:  str
    expiry:       str
    entry_premium: float
    stoploss_points:   float = 0.0
    breakeven_trigger: float = 0.0   # vestigial — see BNTrade
    trail_trigger:     float = 0.0   # vestigial — see BNTrade
    trail_distance:    float = 0.0   # vestigial — see BNTrade
    target_rs:             float = 0.0
    stop_rs:               float = 0.0
    time_stop_s:           float = 0.0
    basket_score_at_entry: float = 0.0
    wobi_at_entry:         float = 0.0
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
    # NF mirror of BNTrade's real-option-LTP fields above — see there.
    option_symbol:      str  = ""
    premium_synthetic:  bool = True


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
    atm_premium:     Optional[float]  = None   # kept for the (currently unused) Entry Loop Monitor UI — mirrors atm_ce_premium
    atm_iv:          Optional[float]  = None
    # Live ATM CE/PE quote (2026-09-18, explicit user decision) — unlike
    # atm_premium above, these are computed EVERY closed bar regardless of
    # whether the entry gates actually pass, so the dashboard can show "what
    # this would cost right now" even with no trade open. Still the same
    # theoretical Black-Scholes estimate as entry_premium always was — no
    # real option-chain data backs this (see CLAUDE.md's "Options pricing").
    atm_ce_premium:  Optional[float]  = None
    atm_pe_premium:  Optional[float]  = None
    cooldown_ok:      bool = True
    sideways_ok:      bool = False
    dir_count_ok:     bool = False
    qty_surge_ok:     bool = False
    same_direction_required: int = 0
    gates_clear:      bool = False
    entry_ready:      bool = False
    # ── Top-8 weighted-basket scalp strategy (2026-09-21) — leader_rows above
    # is now populated with each basket leg's {stock, weight, vwap, ltp,
    # deviationPct} instead of {open, close, volume, surged} (see
    # nf_entry_exit.evaluate_entry) — same key names kept where the shape
    # overlaps, for the dashboard's existing table renderer.
    basket_score:      float = 0.0
    score_threshold:   float = 0.0
    top2_ok:           bool  = False
    top2_names:        List[str] = field(default_factory=list)
    wobi:              Optional[float] = None
    wobi_min_ratio:    float = 0.0
    window_ok:         bool  = True
    trades_today:      int   = 0
    max_trades_today:  int   = 0
    itm_offset_points:  float = 0.0
    target_rs:          float = 0.0
    stop_rs:            float = 0.0
    time_stop_s:        float = 0.0


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
    atm_premium:     Optional[float]  = None   # kept for the (currently unused) Entry Loop Monitor UI — mirrors atm_ce_premium
    atm_iv:          Optional[float]  = None
    # BN mirror of NFDiagnostic's live ATM CE/PE quote fields above — see there.
    atm_ce_premium:  Optional[float]  = None
    atm_pe_premium:  Optional[float]  = None
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
    # BN mirror of NFDiagnostic's Top-8 weighted-basket scalp fields above.
    basket_score:      float = 0.0
    score_threshold:   float = 0.0
    top2_ok:           bool  = False
    top2_names:        List[str] = field(default_factory=list)
    wobi:              Optional[float] = None
    wobi_min_ratio:    float = 0.0
    window_ok:         bool  = True
    trades_today:      int   = 0
    max_trades_today:  int   = 0
    itm_offset_points:  float = 0.0
    target_rs:          float = 0.0
    stop_rs:            float = 0.0
    time_stop_s:        float = 0.0
