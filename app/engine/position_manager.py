from __future__ import annotations

import threading
from typing import Container, Optional, Set

import app.config as cfg

# can_enter runs for every symbol-bar in a backtest before anything else; its
# two limit reads go through config's module __getattr__ (~20× a plain
# attribute). Cache them per cfg.resolution_token() — identical semantics.
_limits_local = threading.local()


def _risk_limits() -> tuple:
    tok    = cfg.resolution_token()
    cached = getattr(_limits_local, "limits", None)
    if cached is not None and cached[0] == tok:
        return cached[1]
    limits = (cfg.MAX_CONCURRENT_POSITIONS, cfg.DAILY_LOSS_LIMIT)
    _limits_local.limits = (tok, limits)
    return limits


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
        capital_pct  — RISK_CAPITAL_PERCENT % of total_capital (entered as a
                       true percentage: 10 = 10% of capital per stop-out).
                       Stop PLACEMENT is identical in both modes; only the
                       share count changes.

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

    # % stop mode: the stop sits SL_PCT% below entry — independent of the
    # swing low, so the support>entry reject doesn't apply. The MIN_SL_OFFSET
    # ₹-floor does NOT apply either: the % is already price-proportional, and
    # flooring it would silently widen a 10% stop to 15% on low-priced stocks
    # (the ₹ floor exists to protect the STRUCTURAL stop from paise-thin
    # swing-low distances, a failure mode the % stop cannot have).
    sl_pct = cfg.SL_PCT
    if sl_pct > 0:
        sl_offset = round(entry_price * sl_pct / 100.0, 2)
        if cfg.RISK_MODE == "capital_pct":
            risk = total_capital * cfg.RISK_CAPITAL_PERCENT / 100.0
        else:
            risk = cfg.RISK_PER_TRADE
        qty = max(1, int(risk / sl_offset))
        target_offset = round(sl_offset * cfg.RR_RATIO, 2)
        capital_needed = (entry_price * qty) / cfg.INTRADAY_LEVERAGE
        if capital_needed > capital:
            qty = int((capital * cfg.INTRADAY_LEVERAGE) / entry_price)
            if qty < 1:
                return 0, sl_offset, target_offset
        return qty, sl_offset, target_offset

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
        risk = total_capital * cfg.RISK_CAPITAL_PERCENT / 100.0
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


def can_enter_scalp(
    symbol:        str,
    open_symbols:  Container[str],
    scalp_open:    int,
    trades_symbol: int,
    trades_today:  int,
    last_exit_ago: Optional[float],
    daily_pnl:     float,
    scalp_pnl:     float,
) -> tuple[bool, str]:
    """
    Circuit breakers for a SCALP entry — the scalper's counterpart to can_enter,
    with state injected the same way so it is testable without AppState.

    Deliberately different from the core rules in three ways:

      * Re-entry IS allowed. The core strategy takes one shot per symbol per day
        (`traded_today`); a scalper that could not re-enter a symbol it just made
        ₹40 on would be crippled. Churn is bounded instead by a per-symbol trade
        cap, a per-day cap, and a cooldown after each exit.
      * Concurrency is counted over SCALP-tagged positions only
        (SCALP_MAX_CONCURRENT_POSITIONS), so the two strategies can't starve each
        other. Total open positions are therefore bounded by the SUM of the two
        caps — set them with that in mind.
      * Two loss limits apply: the scalper's own realized-P&L limit AND the
        account-wide DAILY_LOSS_LIMIT, because an account-level breaker must stop
        every engine, not just the one that tripped it.

    `last_exit_ago` is seconds since this symbol's last scalp exit (None = never
    traded today). Returns (allowed, rejection_reason).
    """
    if symbol in open_symbols:
        return False, f"{symbol} already has an open position"

    if scalp_open >= cfg.SCALP_MAX_CONCURRENT_POSITIONS:
        return False, f"Max {cfg.SCALP_MAX_CONCURRENT_POSITIONS} concurrent scalps reached"

    if trades_symbol >= cfg.SCALP_MAX_TRADES_PER_SYMBOL:
        return False, (f"{symbol} hit its {cfg.SCALP_MAX_TRADES_PER_SYMBOL}-trade "
                       f"daily cap")

    if trades_today >= cfg.SCALP_MAX_TRADES_PER_DAY:
        return False, f"Daily scalp trade cap ({cfg.SCALP_MAX_TRADES_PER_DAY}) reached"

    cooldown = cfg.SCALP_REENTRY_COOLDOWN_S
    if last_exit_ago is not None and last_exit_ago < cooldown:
        return False, (f"{symbol} in re-entry cooldown "
                       f"({cooldown - last_exit_ago:.0f}s left)")

    if scalp_pnl <= -cfg.SCALP_DAILY_LOSS_LIMIT:
        return False, f"Scalp daily loss limit ₹{cfg.SCALP_DAILY_LOSS_LIMIT} hit"

    if daily_pnl <= -cfg.DAILY_LOSS_LIMIT:
        return False, f"Account daily loss limit ₹{cfg.DAILY_LOSS_LIMIT} hit"

    return True, ""


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
    max_pos, loss_limit = _risk_limits()

    if len(open_symbols) >= max_pos:
        return False, f"Max {max_pos} concurrent positions reached"

    if symbol in traded_today:
        return False, f"{symbol} already traded today"

    if symbol in open_symbols:
        return False, f"{symbol} already has an open position"

    if daily_pnl <= -loss_limit:
        return False, f"Daily loss limit ₹{loss_limit} hit"

    return True, ""
