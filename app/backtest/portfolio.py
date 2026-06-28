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

from app.backtest.fills import round_trip_costs


@dataclass
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


@dataclass
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


@dataclass
class Portfolio:
    positions:    Dict[str, BTPosition] = field(default_factory=dict)
    traded_today: Set[str]              = field(default_factory=set)
    daily_pnl:    float                 = 0.0
    trades:       List[BTTrade]         = field(default_factory=list)
    cum_net:      float                 = 0.0          # running net P&L (equity)
    equity_curve: List[tuple]           = field(default_factory=list)  # (timestamp, cum_net)

    # ── Daily lifecycle ─────────────────────────────────────────────────────
    def reset_day(self) -> None:
        self.positions.clear()
        self.traded_today.clear()
        self.daily_pnl = 0.0

    def snapshot(self):
        """Read-only state for can_enter, safe to share across parallel scans."""
        return set(self.positions.keys()), set(self.traded_today), self.daily_pnl

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
        risk       = pos.sl_offset * pos.qty
        r_mult     = (net / risk) if risk > 0 else 0.0

        self.daily_pnl += net
        self.cum_net   += net
        self.equity_curve.append((exit_time, round(self.cum_net, 2)))

        trade = BTTrade(
            symbol=pos.symbol, token=pos.token,
            entry_time=pos.entry_time, entry_price=round(pos.entry_price, 2),
            exit_time=exit_time, exit_price=round(exit_price, 2),
            qty=pos.qty, stop_loss=round(pos.stop_loss, 2), target=round(pos.target, 2),
            outcome=outcome,
            gross_pnl=round(gross, 2), costs=round(costs, 2), net_pnl=round(net, 2),
            r_multiple=round(r_mult, 3),
        )
        self.trades.append(trade)
        return trade
