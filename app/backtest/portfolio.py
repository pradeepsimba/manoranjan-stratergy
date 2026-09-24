from __future__ import annotations

"""
Isolated portfolio state for a Bank Nifty options backtest run.

Single-active-trade semantics (matches c.html and the live engine — never
more than one open BN trade at a time), held in plain objects instead of the
AppState singleton so a backtest never touches live state.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import app.config as cfg
from app.backtest.fills import round_trip_costs_options


@dataclass(slots=True)   # the day's open trade, mirrors BNTrade minus DB/live-only fields
class BTPosition:
    direction:    str
    entry_time:   str
    entry_index_price: float
    entry_gidx:   int          # index into the BN series (exit only after this)
    target:       float        # absolute PREMIUM level (₹) — see BNTrade's comment in models.py
    current_sl:   float        # absolute PREMIUM level (₹)
    sl_stage:     str
    strike:       int
    option_type:  str
    expiry:       str
    entry_premium: float
    # stoploss_points/breakeven_trigger/trail_trigger/trail_distance are
    # vestigial (2026-09-21 scalp-strategy rewrite — no ratcheting stop any
    # more) — kept at 0.0 purely for field-name parity with BNTrade, which
    # evaluate_exit relies on (this repo's "shared decision core" duck-
    # typing convention — see CLAUDE.md). target_rs/stop_rs/time_stop_s are
    # the real frozen-at-entry risk params now.
    stoploss_points:   float = 0.0
    breakeven_trigger: float = 0.0
    trail_trigger:     float = 0.0
    trail_distance:    float = 0.0
    target_rs:             float = 0.0
    stop_rs:               float = 0.0
    time_stop_s:           float = 0.0
    # Added 2026-09-23 (found in review): BNTrade/NFTrade (app/models.py)
    # both carry scratch_slippage_rs, frozen at entry and read by
    # bn_entry_exit.evaluate_exit/nf_entry_exit.evaluate_exit for the
    # TIME_SCRATCH exit branch — CLAUDE.md's shared-decision-core duck-
    # typing convention explicitly requires BTPosition to carry every field
    # name evaluate_exit touches. This one field was missed when
    # scratch_slippage_rs was added, so any future caller that routes
    # backtest exits through the real evaluate_exit (instead of engine.py's
    # current inline duplicate, which reads cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS
    # directly) would hit AttributeError: 'BTPosition' object has no
    # attribute 'scratch_slippage_rs'. Frozen at open the same way as
    # target_rs/stop_rs/time_stop_s above — see _open_position in engine.py.
    scratch_slippage_rs:   float = 0.0
    basket_score_at_entry: float = 0.0
    wobi_at_entry:         float = 0.0
    # cfg.BN_LOT_SIZE, not a hardcoded 30 (2026-09-24, found in review) —
    # models.py's BNTrade.lot_size was fixed to reference cfg.BN_LOT_SIZE
    # directly on 2026-09-24 for exactly this staleness reason (a real
    # contract-spec value NSE revises periodically), but this duck-typed
    # backtest twin was missed. Harmless today only because _open_position
    # (engine.py) always passes lot_size=cfg.BN_LOT_SIZE explicitly.
    lot_size:     int = cfg.BN_LOT_SIZE
    confidence:   float = 0.0
    iv_used:      float = 0.0


@dataclass(slots=True)
class BTTrade:
    symbol:      str
    token:       str
    entry_time:  str
    entry_price: float          # underlying BankNifty index price
    exit_time:   str
    exit_price:  float          # underlying BankNifty index price
    qty:         int            # lot size
    stop_loss:   float          # final (possibly trailed) index SL level
    target:      float          # index target level
    outcome:     str            # "TARGET" | "STOP" | "EOD" | "TIME_SCRATCH" (2026-09-24 fix, found in review — this comment omitted TIME_SCRATCH, the 2026-09-21+ scalp strategy's most common outcome; the DB column that stores this was undersized for exactly this reason, see database.py)
    gross_pnl:   float
    costs:       float
    net_pnl:     float
    r_multiple:  float
    direction:      str
    strike:         int
    option_type:    str
    expiry:         str
    entry_premium:  float
    exit_premium:   float
    iv_used:        Optional[float] = None


@dataclass
class Portfolio:
    active:       Optional[BTPosition] = None
    last_exit_time: Optional[datetime] = None   # bar-time based cooldown (evaluate_entry)
    daily_pnl:    float                = 0.0
    trades:       List[BTTrade]        = field(default_factory=list)
    cum_net:      float                = 0.0
    equity_curve: List[tuple]          = field(default_factory=list)

    def open_position(self, pos: BTPosition) -> None:
        self.active = pos

    def close_position(self, now: datetime, exit_index_price: float,
                       exit_premium: float, outcome: str) -> Optional[BTTrade]:
        pos = self.active
        if pos is None:
            return None
        self.active = None
        self.last_exit_time = now
        exit_time = now.isoformat()

        buy_value  = pos.entry_premium * pos.lot_size
        sell_value = exit_premium      * pos.lot_size
        gross      = sell_value - buy_value
        costs      = round_trip_costs_options(buy_value, sell_value)
        net        = gross - costs
        # R-multiple risk denominator is now stop_rs (the frozen premium-₹
        # stop distance), not the old index-points stoploss_points — see
        # BTPosition's comment above.
        risk       = pos.stop_rs * pos.lot_size
        r_mult     = (net / risk) if risk > 0 else 0.0

        self.daily_pnl += net
        self.cum_net   += net
        self.equity_curve.append((exit_time, round(self.cum_net, 2)))

        trade = BTTrade(
            symbol=cfg.BN_INDEX_NAME, token=cfg.BN_INDEX_TOKEN,
            entry_time=pos.entry_time, entry_price=round(pos.entry_index_price, 2),
            exit_time=exit_time, exit_price=round(exit_index_price, 2),
            qty=pos.lot_size, stop_loss=round(pos.current_sl, 2), target=round(pos.target, 2),
            outcome=outcome,
            gross_pnl=round(gross, 2), costs=round(costs, 2), net_pnl=round(net, 2),
            r_multiple=round(r_mult, 3),
            direction=pos.direction, strike=pos.strike, option_type=pos.option_type,
            expiry=pos.expiry, entry_premium=round(pos.entry_premium, 2),
            exit_premium=round(exit_premium, 2), iv_used=round(pos.iv_used, 4),
        )
        self.trades.append(trade)
        return trade
