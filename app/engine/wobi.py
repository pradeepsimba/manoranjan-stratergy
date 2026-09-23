from __future__ import annotations

"""
Weighted Order-Book-Imbalance (W-OBI) execution filter.

No real option order-book depth exists anywhere in this system, live or
backtest — no broker/exchange connection at all (see CLAUDE.md's "Options
pricing" note: strike/premium have always been synthetic Black-Scholes off
the underlying spot, never real option-chain data). synthetic_depth()
below is a deterministic, clearly-documented PROXY for a top-2-level
bid/ask ladder, not real market data — it exists purely so the "is the
path of least resistance clear" execution filter the strategy spec calls
for has SOMETHING principled to gate on, using inputs (lot size, IV, and
the basket momentum score that just fired the entry) both live and
backtest already have on hand — so, per this repo's shared-decision-core
convention, the exact same function runs in both.

Model: resting size per level scales with lot size (a bigger contract
attracts bigger clips) and inversely with IV (a calmer option holds a
thicker book); the bid/ask TILT is driven directly by the basket momentum
score's MAGNITUDE (`abs(basket_score)`) — a stronger move in EITHER
direction is modeled as a thicker bid side (synthetic_depth always
thickens bid over ask; it takes no direction/option-type argument, so this
is symmetric for both BUY and SELL signals, not literally "whichever side
favors the trade about to be placed" as an earlier draft of this docstring
overclaimed — found in review, 2026-09-23). This is standard
market-microstructure intuition (order flow follows momentum), not a real
observed order book, and since compute_wobi's pass/fail threshold only
ever depends on this same magnitude-driven tilt, W-OBI functions as a
second, independent check on CONVICTION STRENGTH layered on top of the
basket-score gate, not a directional order-flow filter. A signal that only
just barely clears BN_SCALP_SCORE_THRESHOLD/NF_SCALP_SCORE_THRESHOLD
produces a tilt around ~1.5x, which alone does not clear
BN_WOBI_MIN_RATIO/NF_WOBI_MIN_RATIO (2.5) — not a rubber stamp that always
passes once the score fires.
"""

import random
from dataclasses import dataclass


@dataclass(slots=True)
class DepthLevel:
    bid_qty: float
    ask_qty: float


@dataclass(slots=True)
class Depth2:
    level1: DepthLevel
    level2: DepthLevel


def compute_wobi(depth: Depth2) -> float:
    """
    ((2*bid1 + bid2) / (2*ask1 + ask2)) over the top 2 depth levels — the
    front level is weighted double since it's the one about to trade.
    Returns +inf if the ask side is completely exhausted (treated as
    maximally favorable, not an error/NaN).
    """
    bid_weighted = 2.0 * depth.level1.bid_qty + depth.level2.bid_qty
    ask_weighted = 2.0 * depth.level1.ask_qty + depth.level2.ask_qty
    if ask_weighted <= 0:
        return float("inf")
    return bid_weighted / ask_weighted


def synthetic_depth(lot_size: int, iv: float, basket_score: float, seed: str) -> Depth2:
    """
    Deterministic synthetic top-2 depth for the option contract about to be
    traded — see module docstring. `seed` should change bar-to-bar (e.g.
    f"{option_symbol}:{bar_time}") so repeated calls for the SAME decision
    (e.g. a dashboard re-render) are stable, but two different entries never
    collide onto the same imaginary book.
    """
    rng = random.Random(seed)
    base = max(lot_size * 3.0, 50.0) / max(iv, 0.05)
    tilt = 1.0 + min(3.0, abs(basket_score) * 6.0)   # 1x (no conviction) .. 4x (strong conviction)
    bid_base, ask_base = base * tilt, base / tilt

    def _j() -> float:
        return rng.uniform(0.85, 1.15)

    return Depth2(
        level1=DepthLevel(bid_qty=bid_base * _j(), ask_qty=ask_base * _j()),
        level2=DepthLevel(bid_qty=bid_base * 0.6 * _j(), ask_qty=ask_base * 0.6 * _j()),
    )
