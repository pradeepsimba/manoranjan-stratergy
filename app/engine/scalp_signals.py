from __future__ import annotations

"""
Top-N weighted-basket VWAP-momentum signal — shared by BN and NF (see
cfg.BN_SCALP_BASKET/NF_SCALP_BASKET, each the top cfg.SCALP_BASKET_SIZE
constituents by index weight, renormalized to sum to 1.0).

VWAP deviation is computed from 5-MINUTE bar data (typical price × volume,
cumulative from session start), not raw tick-by-tick prices — a deliberate
deviation from a literal "real-time tick VWAP", for the same reason
app.engine.bn_pricing.estimate_iv already made an equivalent tradeoff for
realized volatility (see its own docstring): backtest only ever has 5m
OHLC bars, never a tick stream, so a tick-level VWAP could never be
replayed there. Using bar data for both live and backtest keeps them on
the exact same evaluate_entry implementation — this repo's hard "shared
decision core" convention (see CLAUDE.md).

Entry is evaluated once per newly-closed 5m bar (same convention the old
leader-vote rule used) — this score is therefore a per-bar snapshot, not a
continuously-updating tick value either, matching that cadence.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from app.models import Candle


def session_vwap(candles: List[Candle], today: date) -> Optional[float]:
    """
    Typical-price VWAP — sum((H+L+C)/3 * volume) / sum(volume) — over
    `candles` restricted to bars whose start_time falls on `today`. None if
    there's no volume yet today (session just started, or a feed gap for
    this stock) — callers must treat that leg as having no data, not zero.
    """
    pv = vol = 0.0
    prefix = today.isoformat()
    for c in candles:
        if not c.start_time.startswith(prefix):
            continue
        if c.volume <= 0:
            continue
        typical = (c.high + c.low + c.close) / 3.0 if (c.high and c.low) else c.close
        pv += typical * c.volume
        vol += c.volume
    return (pv / vol) if vol > 0 else None


@dataclass(slots=True)
class BasketLeg:
    name:          str
    token:         str
    weight:        float
    vwap:          Optional[float]
    ltp:           Optional[float]
    deviation_pct: Optional[float]   # (ltp - vwap) / vwap * 100


@dataclass(slots=True)
class BasketReading:
    legs:             List[BasketLeg] = field(default_factory=list)
    score:            float           = 0.0    # weighted sum of deviation_pct across legs with data
    top2_direction_ok: bool           = False   # the 2 heaviest-weighted legs both agree with score's sign
    top2_names:       Tuple[str, str] = ("", "")


def compute_basket_reading(
    basket: Dict[str, float],
    candles_by_token: Dict[str, List[Candle]],
    name_by_token: Dict[str, str],
    today: date,
) -> BasketReading:
    """
    basket: {token: weight}, already renormalized to sum 1.0 (see
        cfg.BN_SCALP_BASKET/NF_SCALP_BASKET).
    candles_by_token: this basket's own tokens' recent closed 5m candles —
        caller slices to CLOSED bars only, same convention as the old
        leader_recent dict (see bn_entry_exit.evaluate_entry's caller in
        scheduler.py / the backtest engine's _leader_recent_at).
    "ltp" for each leg is that leg's own last CLOSED bar's close — not a
    separately-tracked live tick — so live and backtest read the identical
    value for the identical bar (no separate real-time price feed needed
    for this signal at all).
    """
    legs: List[BasketLeg] = []
    score = 0.0
    for token, weight in basket.items():
        name = name_by_token.get(token, token)
        candles = candles_by_token.get(token) or []
        vwap = session_vwap(candles, today)
        ltp = candles[-1].close if candles else None
        dev: Optional[float] = None
        if vwap and vwap > 0 and ltp:
            dev = (ltp - vwap) / vwap * 100.0
            score += weight * dev
        legs.append(BasketLeg(name=name, token=token, weight=weight,
                              vwap=vwap, ltp=ltp, deviation_pct=dev))

    top2 = sorted(legs, key=lambda l: -l.weight)[:2]
    top2_names = (top2[0].name, top2[1].name) if len(top2) == 2 else ("", "")
    if score > 0:
        top2_ok = len(top2) == 2 and all(l.deviation_pct is not None and l.deviation_pct > 0 for l in top2)
    elif score < 0:
        top2_ok = len(top2) == 2 and all(l.deviation_pct is not None and l.deviation_pct < 0 for l in top2)
    else:
        top2_ok = False

    return BasketReading(legs=legs, score=score, top2_direction_ok=top2_ok, top2_names=top2_names)
