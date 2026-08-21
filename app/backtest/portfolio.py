from __future__ import annotations

"""
Isolated portfolio state for a backtest run.

Mirrors the live circuit-breaker rules (max concurrent positions, no same-day
re-entry, daily loss limit, daily reset) but holds everything in plain objects
instead of the AppState singleton — so a backtest never touches live state and
many runs could execute independently.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import app.config as cfg
from app.backtest.fills import round_trip_costs


@dataclass(slots=True)   # built per gated scan across the whole replay
class BTPosition:
    symbol:      str
    token:       str
    entry_time:  str
    entry_price: float    # already slipped
    qty:         int
    stop_loss:   float
    target:      float
    sl_offset:   float
    entry_gidx:  int       # index into the symbol's full series (exit only after this)
    # indicator snapshot at entry (optional — None for legacy positions)
    entry_rsi:     Optional[float] = None
    entry_adx:     Optional[float] = None
    entry_pattern: Optional[str]   = None
    entry_macd:    Optional[float] = None
    entry_support: Optional[float] = None


@dataclass(slots=True)
class BTTrade:
    symbol:      str
    token:       str
    entry_time:  str
    entry_price: float
    exit_time:   str
    exit_price:  float
    qty:         int
    stop_loss:   float
    target:      float
    outcome:     str       # "TARGET" | "STOP" | "EOD"
    gross_pnl:   float
    costs:       float
    net_pnl:     float
    r_multiple:  float      # net_pnl / (sl_offset * qty)
    # indicator snapshot at entry
    entry_rsi:     Optional[float] = None
    entry_adx:     Optional[float] = None
    entry_pattern: Optional[str]   = None
    entry_macd:    Optional[float] = None
    entry_support: Optional[float] = None


@dataclass
class Portfolio:
    positions:    Dict[str, BTPosition] = field(default_factory=dict)
    traded_today: Set[str]              = field(default_factory=set)
    daily_pnl:    float                 = 0.0
    trades:       List[BTTrade]         = field(default_factory=list)
    # Set once the loss stop has blocked entries at least once during this
    # portfolio's life. Purely diagnostic — the gate itself still reads
    # daily_pnl — but without it a run truncated by the loss limit is
    # indistinguishable from one that simply found no more setups, which is the
    # single most confusing way for a backtest to end early.
    loss_limit_hit: bool                = False
    # NOTE: no equity curve here — simulate() rebuilds it from the merged,
    # exit-time-sorted trade stream (per-close accumulation would be in the
    # wrong order once square-offs/parallel days merge).

    # ── Daily lifecycle ─────────────────────────────────────────────────────
    def reset_day(self) -> None:
        self.positions.clear()
        self.traded_today.clear()
        self.daily_pnl = 0.0

    def snapshot(self):
        """Read-only state for can_enter, safe to share across parallel scans."""
        return set(self.positions.keys()), set(self.traded_today), self.daily_pnl

    def margin_used(self) -> float:
        """
        Capital currently committed by OPEN positions (position value ÷
        leverage). New entries may only be sized from what's left of the
        account — without this, each of the 3 concurrent positions could
        consume the FULL buying power (3× the stated capital).
        """
        lev = cfg.INTRADAY_LEVERAGE
        return sum(p.entry_price * p.qty for p in self.positions.values()) / lev

    # ── Open / close ────────────────────────────────────────────────────────
    def open_position(self, pos: BTPosition) -> None:
        self.positions[pos.symbol] = pos
        self.traded_today.add(pos.symbol)

    def close_position(
        self,
        symbol:     str,
        exit_time:  str,
        exit_price: float,
        outcome:    str,
    ) -> Optional[BTTrade]:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None

        buy_value  = pos.entry_price * pos.qty
        sell_value = exit_price      * pos.qty
        gross      = sell_value - buy_value
        costs      = round_trip_costs(buy_value, sell_value)
        net        = gross - costs
        # Risk against the REALIZED entry (slipped fill) and actual stop level, not
        # the pre-slippage sl_offset — otherwise R disagrees with net P&L.
        risk       = (pos.entry_price - pos.stop_loss) * pos.qty
        r_mult     = (net / risk) if risk > 0 else 0.0

        self.daily_pnl += net

        trade = BTTrade(
            symbol=pos.symbol, token=pos.token,
            entry_time=pos.entry_time, entry_price=round(pos.entry_price, 2),
            exit_time=exit_time, exit_price=round(exit_price, 2),
            qty=pos.qty, stop_loss=round(pos.stop_loss, 2), target=round(pos.target, 2),
            outcome=outcome,
            gross_pnl=round(gross, 2), costs=round(costs, 2), net_pnl=round(net, 2),
            r_multiple=round(r_mult, 3),
            entry_rsi=round(pos.entry_rsi, 1) if pos.entry_rsi is not None else None,
            entry_adx=round(pos.entry_adx, 1) if pos.entry_adx is not None else None,
            entry_pattern=pos.entry_pattern,
            entry_macd=round(pos.entry_macd, 4) if pos.entry_macd is not None else None,
            entry_support=round(pos.entry_support, 2) if pos.entry_support is not None else None,
        )
        self.trades.append(trade)
        return trade
