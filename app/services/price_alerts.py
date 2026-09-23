from __future__ import annotations

"""
Server-side leader-consensus price-move alert check.

Moved here from static/js/alerts.js (explicit user decision, 2026-09-09):
that version only re-evaluated once/second, off the dashboard's own
STATE_UPDATE push, and only ran at all while a browser tab had the
dashboard open. This version runs every TICK_EVAL_INTERVAL_MS inside
SchedulerService's tick loop (scheduler.py's _tick_alerts), directly off
the same live st.candles_5m data the trading engine itself reads — so it
fires the instant the condition is met on the server, independent of
whether/how often any browser is watching, and the browser only has to
receive and display a pushed ALERT WebSocket message (see dashboard.js's
ws.onmessage) rather than compute anything.

Purely informational, exactly like the client-side version it replaces —
never touches evaluate_entry/evaluate_exit, never feeds a trading decision.
"""

from typing import Dict, List

import app.config as cfg
from app.state import AppState

# Edge-trigger state, keyed "BankNifty:up"/"BankNifty:down"/"Nifty 50:up"/...
# — module-level, not locked: only ever touched from the tick loop running on
# the event loop (same reasoning as st.bn_trades_today's lack of a lock).
_was_consensus: Dict[str, bool] = {}


def reset_consensus_state() -> None:
    """
    Clear all committed edge-trigger state (found in review, 2026-09-23) —
    called by scheduler.py's _tick_alerts on a 0->positive dashboard-client
    transition, so a condition that cycled true->false->true entirely while
    no client was connected (which the has_clients commit-gate above does
    NOT by itself catch — see _tick_alerts's comment) is guaranteed to look
    like a fresh edge on the first post-reconnect check if it's still active.
    """
    _was_consensus.clear()


def _leader_results(st: AppState, leader_stocks: Dict[str, str],
                    price_alert_attr: Dict[str, str]) -> List[dict]:
    """
    Per leader stock: does its CURRENT (possibly still-forming) 5m bar's
    |close-open| move cross its own configured points threshold, and which
    direction — the exact same live-bar check static/js/alerts.js's
    checkPriceAlerts used to make client-side.
    """
    results: List[dict] = []
    for name, token in leader_stocks.items():
        attr = price_alert_attr.get(name)
        pts = getattr(cfg, attr) if attr else None
        if pts is None:
            continue
        with st.candle_lock(token):
            candles = st.candles_5m.get(token)
            candle = candles[-1] if candles else None
        if not candle or not candle.open or not candle.close:
            continue
        move = abs(candle.close - candle.open)
        direction = "up" if candle.close > candle.open else ("down" if candle.close < candle.open else None)
        results.append({"stock": name, "crossed": move >= pts, "dir": direction})
    return results


def check_consensus(st: AppState, instr_label: str, leader_stocks: Dict[str, str],
                    price_alert_attr: Dict[str, str], required: int,
                    has_clients: bool = True) -> List[dict]:
    """
    Evaluate the "N leaders crossed their own threshold AND agree on
    direction" condition for one instrument. Edge-triggered per direction
    (fires once when the count first reaches `required`, goes quiet until it
    drops back below and crosses again) — same semantics as the client-side
    checkConsensusAlert it replaces.

    `has_clients` (2026-09-23, found in review): whether to COMMIT this
    tick's met/not-met result into `_was_consensus`. Detection itself always
    runs regardless of browser presence (the whole point of this module, per
    its own docstring) — but if state were committed even while nobody's
    connected, an edge detected with zero clients would be "consumed" and
    never seen: a client connecting later, while the condition is STILL true
    (never dropped back below threshold), would see met=True/was=True — no
    new edge, so it never fires for them, contradicting the documented goal
    of the condition being independent of whether/how often a browser is
    watching. Skipping the commit while disconnected keeps the last
    COMMITTED state frozen, so the first check after a client (re)connects
    correctly sees it as a fresh edge if the condition is still active.

    Returns a list of {title, body} dicts for whichever direction(s) newly
    fired THIS call (usually 0, occasionally 1; both directions firing on
    the same tick is possible in principle — e.g. 6 leaders split 3-up/3-down
    with required=3 — so this always returns a list, never assumes at most one).
    """
    results = _leader_results(st, leader_stocks, price_alert_attr)
    up = [r for r in results if r["crossed"] and r["dir"] == "up"]
    down = [r for r in results if r["crossed"] and r["dir"] == "down"]

    fired: List[dict] = []
    for direction, matching in (("up", up), ("down", down)):
        key = f"{instr_label}:{direction}"
        met = len(matching) >= required
        was = _was_consensus.get(key, False)
        if has_clients:
            _was_consensus[key] = met
        if met and not was:
            names = ", ".join(r["stock"] for r in matching)
            fired.append({
                "title": f"{instr_label}: {len(matching)}/{len(results)} leaders moved {direction} together",
                "body": f"{names} — each crossed its own move-alert threshold (need ≥{required})",
            })
    return fired
