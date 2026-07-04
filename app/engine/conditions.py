from __future__ import annotations

"""
Shared entry-condition evaluation — the single place where the 8 entry checks
and their runtime enable/disable toggles are defined. Used by BOTH the live
entry engine and the backtest so the two can never drift.

A disabled condition auto-passes: it is excluded from the failure list but
still reported in the checks dict for the dashboard.
"""

from typing import Dict, List, Optional

import app.config as cfg
from app.models import IndicatorResult

# The single source of truth: check key → (config toggle attr, evaluator).
# depth_ratio=None (no snap data yet / backtest) defaults depth to PASS —
# it only vetoes a clearly sell-skewed book.
_CONDITIONS: Dict[str, tuple] = {
    "near_support":    ("COND_NEAR_SUPPORT",
                        lambda ind, r: ind.near_support),
    "bullish_pattern": ("COND_BULLISH_PATTERN",
                        lambda ind, r: ind.bullish_pattern),
    "adx_ok":          ("COND_ADX",
                        lambda ind, r: ind.adx_ok),
    "rsi_ok":          ("COND_RSI",
                        lambda ind, r: ind.rsi_above_30 or ind.rsi_rising),
    "macd_cross":      ("COND_MACD_CROSS",
                        lambda ind, r: ind.macd_bullish_cross),
    "volume_surge":    ("COND_VOLUME_SURGE",
                        lambda ind, r: ind.volume_surge),
    "above_vwap":      ("COND_ABOVE_VWAP",
                        lambda ind, r: ind.price_above_vwap),
    "depth_bullish":   ("COND_DEPTH",
                        lambda ind, r: (r >= cfg.DEPTH_MIN_RATIO) if r is not None else True),
}

CONDITION_TOGGLES: Dict[str, str] = {k: t for k, (t, _) in _CONDITIONS.items()}


def build_entry_checks(ind: IndicatorResult,
                       depth_ratio: Optional[float]) -> Dict[str, bool]:
    """All 8 condition outcomes (toggles NOT applied) — for diagnostics/UI."""
    return {k: fn(ind, depth_ratio) for k, (_, fn) in _CONDITIONS.items()}


def failed_entry_checks(checks: Dict[str, bool]) -> List[str]:
    """Failing checks among the ENABLED conditions (disabled = auto-pass)."""
    return [k for k, v in checks.items()
            if not v and getattr(cfg, CONDITION_TOGGLES[k])]


def entry_ok(ind: IndicatorResult, depth_ratio: Optional[float] = None) -> bool:
    """
    Short-circuit conjunction of the ENABLED conditions — the backtest's hot
    path (no dict build, stops at the first enabled failure). Same _CONDITIONS
    table as build_entry_checks/failed_entry_checks, so the two evaluation
    styles cannot drift.
    """
    for toggle, fn in _CONDITIONS.values():
        if getattr(cfg, toggle) and not fn(ind, depth_ratio):
            return False
    return True


# The conditions computable WITHOUT TA-Lib — compute_indicators evaluates
# these first and (in entry_short_circuit mode) skips the expensive RSI/MACD/
# ADX calls when one already vetoes the conjunctive entry.
_CHEAP_KEYS = ("near_support", "bullish_pattern", "above_vwap", "volume_surge")


def cheap_gates_veto(ind: IndicatorResult) -> bool:
    """
    True when an ENABLED cheap condition already fails — the RSI/MACD/ADX
    values then can't change the (conjunctive) entry decision. Driven by the
    same _CONDITIONS table so the short-circuit can never disagree with
    entry_ok / failed_entry_checks.
    """
    for key in _CHEAP_KEYS:
        toggle, fn = _CONDITIONS[key]
        if getattr(cfg, toggle) and not fn(ind, None):
            return True
    return False
