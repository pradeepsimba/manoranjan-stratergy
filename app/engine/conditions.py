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

# check key → config toggle attribute
CONDITION_TOGGLES: Dict[str, str] = {
    "near_support":    "COND_NEAR_SUPPORT",
    "bullish_pattern": "COND_BULLISH_PATTERN",
    "adx_ok":          "COND_ADX",
    "rsi_ok":          "COND_RSI",
    "macd_cross":      "COND_MACD_CROSS",
    "volume_surge":    "COND_VOLUME_SURGE",
    "above_vwap":      "COND_ABOVE_VWAP",
    "depth_bullish":   "COND_DEPTH",
}


def build_entry_checks(ind: IndicatorResult,
                       depth_ratio: Optional[float]) -> Dict[str, bool]:
    """
    The 8 entry conditions from an IndicatorResult + live order-book ratio.
    depth_ratio=None (no snap data yet / backtest) defaults depth to PASS —
    it only vetoes a clearly sell-skewed book.
    """
    return {
        "near_support":    ind.near_support,
        "bullish_pattern": ind.bullish_pattern,
        "adx_ok":          ind.adx_ok,
        "rsi_ok":          ind.rsi_above_30 or ind.rsi_rising,
        "macd_cross":      ind.macd_bullish_cross,
        "volume_surge":    ind.volume_surge,
        "above_vwap":      ind.price_above_vwap,
        "depth_bullish":   (depth_ratio >= cfg.DEPTH_MIN_RATIO)
                           if depth_ratio is not None else True,
    }


def failed_entry_checks(checks: Dict[str, bool]) -> List[str]:
    """Failing checks among the ENABLED conditions (disabled = auto-pass)."""
    return [k for k, v in checks.items()
            if not v and getattr(cfg, CONDITION_TOGGLES[k])]
