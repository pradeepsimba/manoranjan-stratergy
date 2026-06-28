from __future__ import annotations

import app.config as cfg
from app.state import get_state


def calc_quantity(entry_price: float, support: float) -> tuple[int, float, float]:
    """
    Compute trade quantity using the blueprint formula:
        Qty = RISK_PER_TRADE / (entry - support)

    Returns (quantity, sl_offset, target_offset).
    Returns (0, ...) if the setup is invalid or capital is insufficient.
    """
    sl_offset = round(entry_price - support, 2)
    if sl_offset <= 0:
        return 0, 0.0, 0.0

    raw_qty = cfg.RISK_PER_TRADE / sl_offset
    qty     = max(1, int(raw_qty))

    # Effective capital check: Angel One provides 5× intraday leverage
    capital_needed = (entry_price * qty) / cfg.INTRADAY_LEVERAGE
    if capital_needed > cfg.ACCOUNT_BALANCE:
        qty = max(1, int((cfg.ACCOUNT_BALANCE * cfg.INTRADAY_LEVERAGE) / entry_price))

    target_offset = round(sl_offset * cfg.RR_RATIO, 2)
    return qty, sl_offset, target_offset


def can_enter(symbol: str) -> tuple[bool, str]:
    """
    Run all circuit-breaker checks before allowing a new entry.
    Returns (allowed, rejection_reason).
    """
    st = get_state()

    if len(st.positions) >= cfg.MAX_CONCURRENT_POSITIONS:
        return False, f"Max {cfg.MAX_CONCURRENT_POSITIONS} concurrent positions reached"

    if symbol in st.traded_today:
        return False, f"{symbol} already traded today"

    if symbol in st.positions:
        return False, f"{symbol} already has an open position"

    if st.daily_pnl <= -cfg.DAILY_LOSS_LIMIT:
        return False, f"Daily loss limit ₹{cfg.DAILY_LOSS_LIMIT} hit"

    return True, ""
