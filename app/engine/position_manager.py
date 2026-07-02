from __future__ import annotations

from typing import Container, Set

import app.config as cfg


def calc_quantity(
    entry_price: float,
    support:     float,
    capital:     float = cfg.ACCOUNT_BALANCE,
) -> tuple[int, float, float]:
    """
    Compute trade quantity using the blueprint formula:
        Qty = RISK_PER_TRADE / (entry - support)

    `capital` overrides cfg.ACCOUNT_BALANCE so the backtest can be run with a
    user-supplied starting balance without touching global config.

    Returns (quantity, sl_offset, target_offset).
    Returns (0, ...) if the setup is invalid or capital is insufficient.
    """
    sl_offset = round(max(entry_price - support, cfg.MIN_SL_OFFSET), 2)

    raw_qty = cfg.RISK_PER_TRADE / sl_offset
    qty     = max(1, int(raw_qty))

    target_offset = round(sl_offset * cfg.RR_RATIO, 2)

    # Effective capital check: 5× intraday leverage. If even one share exceeds
    # the leveraged capital, the setup is unaffordable — return qty 0 so the
    # caller's `if qty == 0` guard rejects it (live and backtest both check).
    capital_needed = (entry_price * qty) / cfg.INTRADAY_LEVERAGE
    if capital_needed > capital:
        qty = int((capital * cfg.INTRADAY_LEVERAGE) / entry_price)
        if qty < 1:
            return 0, sl_offset, target_offset

    return qty, sl_offset, target_offset


def can_enter(
    symbol:       str,
    open_symbols: Container[str],
    traded_today: Set[str],
    daily_pnl:    float,
) -> tuple[bool, str]:
    """
    Run all circuit-breaker checks before allowing a new entry.

    State is injected (not read from any global) so the exact same rules drive
    both the live engine (passing AppState) and the backtest engine (passing a
    BacktestPortfolio).

    open_symbols — current open positions, supporting `in` and `len`
    Returns (allowed, rejection_reason).
    """
    if len(open_symbols) >= cfg.MAX_CONCURRENT_POSITIONS:
        return False, f"Max {cfg.MAX_CONCURRENT_POSITIONS} concurrent positions reached"

    if symbol in traded_today:
        return False, f"{symbol} already traded today"

    if symbol in open_symbols:
        return False, f"{symbol} already has an open position"

    if daily_pnl <= -cfg.DAILY_LOSS_LIMIT:
        return False, f"Daily loss limit ₹{cfg.DAILY_LOSS_LIMIT} hit"

    return True, ""
