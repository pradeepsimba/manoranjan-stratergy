from __future__ import annotations

"""Performance metrics computed from a backtest's closed trades."""

from typing import Dict, List


def _empty(days: int) -> Dict:
    return {
        "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
        "win_rate": 0.0, "net_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        "profit_factor": None, "avg_win": 0.0, "avg_loss": 0.0,
        "expectancy": 0.0, "avg_r_multiple": 0.0, "max_drawdown": 0.0,
        "total_costs": 0.0, "days_traded": days, "equity_curve": [],
    }


def compute_metrics(trades: List, equity_curve: List, days: int) -> Dict:
    n = len(trades)
    if n == 0:
        return _empty(days)

    # Win/loss classification, profit factor, and the averages are all NET
    # (post-cost) — what the trader actually keeps. gross_profit/gross_loss are
    # TRUE pre-cost sums (classified by gross sign), so the identity
    # gross_profit + gross_loss − total_costs == net_pnl holds exactly.
    wins   = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]

    net_wins   = sum(t.net_pnl for t in wins)
    net_losses = sum(t.net_pnl for t in losses)            # ≤ 0
    net        = sum(t.net_pnl for t in trades)

    gross_profit = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_loss   = sum(t.gross_pnl for t in trades if t.gross_pnl < 0)   # ≤ 0
    total_costs  = sum(t.costs   for t in trades)

    profit_factor = (net_wins / abs(net_losses)) if net_losses < 0 else None

    # Max drawdown from the running-equity curve (peak-to-trough of cum net P&L).
    peak = 0.0
    max_dd = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "total_trades":   n,
        "winning_trades": len(wins),
        "losing_trades":  len(losses),
        "win_rate":       round(len(wins) / n, 4),
        "net_pnl":        round(net, 2),
        "gross_profit":   round(gross_profit, 2),
        "gross_loss":     round(gross_loss, 2),
        "profit_factor":  round(profit_factor, 3) if profit_factor is not None else None,
        "avg_win":        round(net_wins / len(wins), 2) if wins else 0.0,
        "avg_loss":       round(net_losses / len(losses), 2) if losses else 0.0,
        "expectancy":     round(net / n, 2),
        "avg_r_multiple": round(sum(t.r_multiple for t in trades) / n, 3),
        "max_drawdown":   round(max_dd, 2),
        "total_costs":    round(total_costs, 2),
        "days_traded":    days,
        "equity_curve":   [[ts, eq] for ts, eq in equity_curve],
    }
