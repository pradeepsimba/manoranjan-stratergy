from __future__ import annotations

from typing import Container, Optional, Set

import app.config as cfg


def calc_quantity(
    entry_price:   float,
    support:       float,
    capital:       Optional[float] = None,
    total_capital: Optional[float] = None,
) -> tuple[int, float, float]:
    """
    Compute trade quantity using the blueprint formula:
        Qty = risk / (entry - support)

    where `risk` — the ₹ lost when the stop hits — is resolved from RISK_MODE:
        fixed_amount — RISK_PER_TRADE ₹ (original blueprint)
        capital_pct  — RISK_CAPITAL_PCT × total_capital (e.g. 2% of the
                       account per stop-out). Stop PLACEMENT is identical in
                       both modes; only the share count changes.

    `capital` is the AVAILABLE capital (account minus margin already committed
    by open positions) — the affordability ceiling. `total_capital` is the
    FULL account/run equity, the basis for capital_pct risk; it must not
    shrink as positions open, or the risk per trade would silently decay.
    Both default to cfg.ACCOUNT_BALANCE so the backtest can run on a
    user-supplied balance without touching global config. (Resolved at call
    time — a default-argument cfg read would freeze the dynamic value.)

    Returns (quantity, sl_offset, target_offset).
    Returns (0, ...) if the setup is invalid or capital is insufficient.
    """
    if capital is None:
        capital = cfg.ACCOUNT_BALANCE
    if total_capital is None:
        total_capital = cfg.ACCOUNT_BALANCE

    # Support at/above entry means no structural stop BELOW the entry price.
    # Flooring to MIN_SL_OFFSET would put the stop at an arbitrary entry−₹5 and
    # size the position off that nonsensical distance (RISK/5 = a large qty).
    # The near_support condition normally guarantees entry ≥ support, so this is
    # only reachable with COND_NEAR_SUPPORT disabled — reject rather than size a
    # trade on a meaningless stop. (support ≤ 0 = no data → entry−support = entry,
    # a huge stop / tiny qty, which is harmless and left as-is.)
    if support > entry_price:
        return 0, 0.0, 0.0

    sl_offset = round(max(entry_price - support, cfg.MIN_SL_OFFSET), 2)

    if cfg.RISK_MODE == "capital_pct":
        risk = total_capital * cfg.RISK_CAPITAL_PCT
    else:
        risk = cfg.RISK_PER_TRADE
    raw_qty = risk / sl_offset
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
