from __future__ import annotations

"""
Realistic fill + cost model for backtesting.

  * Slippage: a fixed bps haircut applied against the trade direction (worse
    entry, worse exit).
  * Gap-through: if a bar's open is already beyond the stop or target, the fill
    happens at the OPEN (capturing gap risk) rather than at the level.
  * Round-trip costs: brokerage, STT, exchange txn, GST, stamp duty, SEBI fee —
    Indian intraday-equity defaults, all tunable in config.
"""

import app.config as cfg


# ── Slippage ──────────────────────────────────────────────────────────────────

def _slip_buy(price: float, bps: float) -> float:
    return price * (1 + bps / 10_000.0)


def _slip_sell(price: float, bps: float) -> float:
    return price * (1 - bps / 10_000.0)


def entry_fill(close: float, bps: float) -> float:
    """Long entry at the signal bar close, slipped upward."""
    return _slip_buy(close, bps)


def stop_fill(stop_level: float, bar_open: float, bps: float) -> float:
    """
    Stop-loss exit. If the bar gapped below the stop, fill at the open (worse);
    otherwise fill at the stop level. Then slipped downward.
    """
    raw = bar_open if bar_open < stop_level else stop_level
    return _slip_sell(raw, bps)


def target_fill(target_level: float, bar_open: float, bps: float) -> float:
    """
    Target exit. If the bar gapped above the target, fill at the open (better);
    otherwise fill at the target level. Then slipped downward.
    """
    raw = bar_open if bar_open > target_level else target_level
    return _slip_sell(raw, bps)


def square_off_fill(close: float, bps: float) -> float:
    """Forced EOD exit at the last bar close, slipped downward."""
    return _slip_sell(close, bps)


# ── Costs ─────────────────────────────────────────────────────────────────────

def round_trip_costs(buy_value: float, sell_value: float) -> float:
    """
    Total transaction cost for one buy + one sell leg (absolute ₹).

    COST_STT_BUY and COST_DP_SELL are 0 for intraday; delivery-mode replays
    shadow them (via engine._delivery_overrides) because CNC pays STT on BOTH
    legs plus a flat DP charge per sell — without them positional P&L is
    overstated by roughly 0.2% of turnover per round trip.
    """
    brokerage = (
        min(cfg.COST_BROKERAGE_CAP, cfg.COST_BROKERAGE_PCT * buy_value)
        + min(cfg.COST_BROKERAGE_CAP, cfg.COST_BROKERAGE_PCT * sell_value)
    )
    stt   = cfg.COST_STT_SELL * sell_value + cfg.COST_STT_BUY * buy_value
    txn   = cfg.COST_TXN_CHARGE * (buy_value + sell_value)
    gst   = cfg.COST_GST        * (brokerage + txn)
    stamp = cfg.COST_STAMP_BUY  * buy_value
    sebi  = cfg.COST_SEBI       * (buy_value + sell_value)
    return brokerage + stt + txn + gst + stamp + sebi + cfg.COST_DP_SELL
