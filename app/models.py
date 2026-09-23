from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import numpy as np


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


def closed_tail(candles: List[Candle], n: int) -> List[Candle]:
    """
    The last `n` CLOSED bars from `candles`, excluding a still-forming
    final bar — candles[-1] is always in-progress, mutated in place tick
    by tick (see market_data._upsert), so it must never be fed into
    estimate_iv's log-return calculation: a return spanning only however
    many seconds the bar has been open would get annualized as if it were
    a full closed 5-minute move (2026-09-22, found in review — was the
    root cause of a real production bug: a scalp trade's exit-side IV
    estimate disagreeing with its entry-side estimate blew straight
    through the strategy's tight ~₹2-3 premium target/stop bracket).

    A plain slice — `candles[-(n+1):-1]` — already does the right thing in
    every edge case (empty list, single forming-only bar, fewer than n+1
    bars, exactly n+1, more than n+1: Python clips an out-of-range negative
    start to 0 and naturally yields `[]` when the computed stop precedes
    the start), so there's no hand-rolled branching to get wrong. Slicing
    the tail first (n+1 elements), not the whole possibly-~300-bar buffer,
    is what keeps this cheap when called every ~100ms from the tick loop.
    """
    return candles[-(n + 1):-1]


def closed_tail_closes(candles: List[Candle], n: int) -> np.ndarray:
    """
    closed_tail() as a float64 close-price array — every one of this
    function's 10 call sites (found in review, 2026-09-22) previously
    repeated `np.fromiter((c.close for c in tail), np.float64, len(tail))`
    by hand after calling closed_tail(); this is the one shared
    implementation of that second step too, so a future change to how
    closes are extracted only needs to happen here.
    """
    tail = closed_tail(candles, n)
    return np.fromiter((c.close for c in tail), np.float64, len(tail))


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
    # TIME_SCRATCH settlement slippage — frozen from cfg.BN_SCALP_SCRATCH_
    # SLIPPAGE_RS at entry (2026-09-23 fix, found in review), same freeze
    # rule as target_rs/stop_rs/time_stop_s above: evaluate_exit used to
    # read cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS directly at exit time instead of
    # from the trade, meaning a live Settings-page change mid-trade could
    # retroactively alter an already-open trade's TIME_SCRATCH payout — the
    # exact class of bug the freeze convention exists to prevent.
    scratch_slippage_rs: float = 0.0
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
    # option_symbol is the vendor instrument this trade's option leg would
    # be subscribed under (e.g. "BANKNIFTY17SEP56400CE") — still computed
    # and displayed as a label for which contract this trade models, even
    # though nothing subscribes to it for a live tick any more.
    #
    # There used to be a premium_synthetic field here too — a one-way latch
    # that switched current_premium/exit_premium to a real option-market LTP
    # once a real tick arrived, instead of the Black-Scholes mark.  REMOVED
    # 2026-09-22 (field and all) after that switch let a real tick snap
    # settlement past the scalp strategy's whole ~₹2-3 target/stop bracket
    # (see bn_trade.check_tick_exit's docstring for the full incident
    # writeup). entry_premium is, and always was, ALWAYS the Black-Scholes
    # value. Backtest's BTPosition has no equivalent fields; this only ever
    # applied to a live BNTrade.
    option_symbol:      str  = ""


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
    scratch_slippage_rs:   float = 0.0   # frozen at entry — see BNTrade's comment above
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
    # NF mirror of BNTrade's option_symbol comment above — see there
    # (including why premium_synthetic used to live here too).
    option_symbol:      str  = ""


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
    # NOTE (2026-09-23 fix): these fields are named itm_* (not atm_*) because
    # they hold the deep-ITM strike/premium the scalp strategy would actually
    # trade (bn_pricing.get_itm_strike, offset by cfg.NF_ITM_OFFSET_POINTS
    # from spot) — NOT the true at-the-money strike. Before this fix these
    # were misnamed atm_strike/atm_premium/etc. and rendered in the dashboard
    # as "ATM {strike}", which visibly disagreed with the real ATM strike
    # shown by the separate live ATM CE/PE watchlist (bn_atm_ce_symbol/etc.
    # in state.py) — confirmed user-facing confusion, not a P&L bug (nothing
    # here feeds settlement), but a real naming/labeling defect.
    itm_strike:      Optional[int]    = None
    itm_premium:     Optional[float]  = None   # kept for the (currently unused) Entry Loop Monitor UI — mirrors itm_ce_premium
    itm_iv:          Optional[float]  = None
    # Live ITM CE/PE quote (2026-09-18, explicit user decision) — unlike
    # itm_premium above, these are computed EVERY closed bar regardless of
    # whether the entry gates actually pass, so the dashboard can show "what
    # this would cost right now" even with no trade open. Still the same
    # theoretical Black-Scholes estimate as entry_premium always was — no
    # real option-chain data backs this (see CLAUDE.md's "Options pricing").
    itm_ce_premium:  Optional[float]  = None
    itm_pe_premium:  Optional[float]  = None
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
    # See NFDiagnostic's itm_strike NOTE above — same rename, same reason.
    itm_strike:      Optional[int]    = None
    itm_premium:     Optional[float]  = None   # kept for the (currently unused) Entry Loop Monitor UI — mirrors itm_ce_premium
    itm_iv:          Optional[float]  = None
    # BN mirror of NFDiagnostic's live ITM CE/PE quote fields above — see there.
    itm_ce_premium:  Optional[float]  = None
    itm_pe_premium:  Optional[float]  = None
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
