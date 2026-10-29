"""
Real option prices for paper trading.

Paper trades used to be priced with a Black-Scholes calculation. When USE_REAL_OPTION_PRICES is on
(default) they are priced from the broker's actual option last-traded price instead: it arrives over
the websocket server on the dedicated option connection (see MarketDataService.set_option_ladder,
which keeps the deep-ITM strikes this strategy trades subscribed) and is cached per symbol in
state.opt_ltp by MarketDataService._process_tick.

  * OPENING a trade needs a RECENT real price (OPTION_PRICE_MAX_AGE_S). With none, the entry is
    skipped - it is never priced from a model instead.
  * CLOSING a trade uses the latest known price for its contract whatever its age: an exit must
    never be blocked by a quiet feed.

The earlier real-price implementation (removed 2026-09-22) mixed a CALCULATED entry premium with REAL
exit ticks, so the first real tick could jump the premium straight past the tight target/stop bracket.
Entry and exit both use the same real source now, so there is no such discontinuity.
"""
import time
from typing import Optional

import app.config as cfg
from app.state import get_state


def use_real_prices() -> bool:
    return bool(cfg.USE_REAL_OPTION_PRICES)


def live_option_premium(symbol: str, max_age_s: Optional[float] = None) -> Optional[float]:
    """Latest real LTP for `symbol`; None if never seen, or older than `max_age_s` when given."""
    if not symbol:
        return None
    entry = get_state().opt_ltp.get(symbol)
    if entry is None:
        return None
    ltp, ts = entry
    if max_age_s is not None and time.time() - ts > max_age_s:
        return None
    return ltp


def exit_mark(trade) -> Optional[float]:
    """Mark for an OPEN trade's own contract, or None when this trade is priced by calculation
    (callers then use the Black-Scholes mark). Follows the basis the trade was OPENED with
    (trade.premium_real), not the live setting, so toggling the setting mid-trade can't switch an
    open trade between two price sources. Latest real LTP of any age; if none has ever been seen
    (e.g. a trade restored after a restart before its first tick) hold at the trade's last mark."""
    basis = trade.premium_real if trade.premium_real is not None else use_real_prices()
    if not basis:
        return None
    real = live_option_premium(trade.option_symbol)
    if real is not None:
        return real
    return trade.current_premium if trade.current_premium > 0 else trade.entry_premium
