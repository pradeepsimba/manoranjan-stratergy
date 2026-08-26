from __future__ import annotations

"""
Leader-consensus signal study — NOT part of the main options P&L backtest
(app/backtest/engine.py). Answers a narrower question: when N-of-6 BN leader
stocks both cross their own BN_PRICE_ALERT_PTS_* threshold (raw points, not
%) AND agree on direction on bar T (the EXACT same condition
static/js/alerts.js checks live off the latest bar — see
checkPriceAlerts/checkConsensusAlert there), how often does BankNifty's own
bar T+1 move the same direction, and by how much?

Synchronous — a handful of days x ~75 bars x 6 stocks, no option pricing —
so unlike the real backtest this needs no run_id/polling/DB persistence,
just a direct request/response.

Data sources mirror what the live engine and the real backtest already use:
the self-recorded bn_index_bars archive for the index (the vendor gives no
historical BankNifty index data at all — see CLAUDE.md), and a REST fetch
for the 6 leader stocks over that same span (fully archived on the vendor
side, unlike the index).
"""

from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import app.config as cfg
from app.models import Candle
from app.services.historical_data import fetch_indicator_history

IST = ZoneInfo("Asia/Kolkata")


def _leader_signal(bar_by_token: Dict[str, Candle]) -> List[Dict[str, Any]]:
    """Python mirror of static/js/alerts.js's checkPriceAlerts crossed/direction check."""
    results = []
    for name, token in cfg.BN_LEADER_STOCKS.items():
        bar = bar_by_token.get(token)
        if bar is None or not bar.open or not bar.close:
            continue
        attr = cfg.BN_PRICE_ALERT_ATTR.get(name)
        pts = getattr(cfg, attr) if attr else None
        if pts is None:
            continue
        move_pts = abs(bar.close - bar.open)
        direction = "up" if bar.close > bar.open else ("down" if bar.close < bar.open else None)
        results.append({"name": name, "crossed": move_pts >= pts, "dir": direction})
    return results


async def run_bn_leader_consensus_study(db) -> Dict[str, Any]:
    # 1. The full self-recorded BankNifty index archive — no date range
    # filtering needed, this repo's only source of BankNifty index history.
    index_bars = await db.get_bn_index_bars("2000-01-01T00:00:00", "2100-01-01T00:00:00")
    if len(index_bars) < 2:
        return {
            "total_signals": 0,
            "note": "Not enough self-recorded BankNifty index history yet — "
                    "the live engine needs to have run at least one full day.",
        }

    from_date = datetime.fromisoformat(index_bars[0].start_time).date()
    to_date = datetime.fromisoformat(index_bars[-1].start_time).date()
    days_back = max((datetime.now(IST).date() - from_date).days + 1, 1)

    # 2. Leader stocks over that same span (fully archived on the vendor).
    leader_hist = await fetch_indicator_history(cfg.BN_LEADER_STOCKS, cfg.INTERVAL_5M, days_back=days_back)
    leader_by_time: Dict[str, Dict[str, Candle]] = {}
    for token, bars in leader_hist.items():
        for b in bars:
            leader_by_time.setdefault(b.start_time, {})[token] = b

    required = cfg.BN_ALERT_CONSENSUS_REQUIRED
    signals_up = signals_down = 0
    hits_up = hits_down = 0
    move_points_up: List[float] = []
    move_points_down: List[float] = []

    # Diagnostics — so a 0-signal result is distinguishable from "barely any
    # history yet" vs "plenty of history, this condition just never fired"
    # (a strict, simultaneous N-of-6-on-the-same-bar condition is inherently
    # rarer than any one stock crossing its own threshold alone).
    bars_scanned = 0
    bars_with_leader_data = 0
    max_up_count = 0
    max_down_count = 0

    for i in range(len(index_bars) - 1):
        bar, next_bar = index_bars[i], index_bars[i + 1]
        # Same-day only — an overnight gap into next_bar isn't a genuine
        # intraday "next candle" relationship (matches the real backtest's
        # own intraday-only, fresh-day-boundary convention).
        if bar.start_time[:10] != next_bar.start_time[:10]:
            continue
        bars_scanned += 1
        leaders_now = leader_by_time.get(bar.start_time)
        if not leaders_now:
            continue
        bars_with_leader_data += 1

        results = _leader_signal(leaders_now)
        up_count = sum(1 for r in results if r["crossed"] and r["dir"] == "up")
        down_count = sum(1 for r in results if r["crossed"] and r["dir"] == "down")
        max_up_count = max(max_up_count, up_count)
        max_down_count = max(max_down_count, down_count)

        if up_count >= required:
            signals_up += 1
            move = next_bar.close - next_bar.open
            move_points_up.append(move)
            if next_bar.close > next_bar.open:
                hits_up += 1
        elif down_count >= required:
            signals_down += 1
            move = next_bar.close - next_bar.open
            move_points_down.append(move)
            if next_bar.close < next_bar.open:
                hits_down += 1

    result = {
        "from_date": str(from_date), "to_date": str(to_date),
        "total_signals": signals_up + signals_down,
        "consensus_required": required,
        "signals_up": signals_up, "hits_up": hits_up,
        "win_rate_up": round(hits_up / signals_up, 3) if signals_up else None,
        "avg_move_points_up": round(sum(move_points_up) / len(move_points_up), 2) if move_points_up else None,
        "signals_down": signals_down, "hits_down": hits_down,
        "win_rate_down": round(hits_down / signals_down, 3) if signals_down else None,
        "avg_move_points_down": round(sum(move_points_down) / len(move_points_down), 2) if move_points_down else None,
        "bars_scanned": bars_scanned,
        "bars_with_leader_data": bars_with_leader_data,
        "max_up_count": max_up_count,
        "max_down_count": max_down_count,
    }
    if result["total_signals"] == 0:
        result["note"] = (
            f"Scanned {bars_scanned} bars ({bars_with_leader_data} had matching leader data). "
            f"Closest it ever got: {max(max_up_count, max_down_count)} of 6 leaders agreed "
            f"(need {required}) — the condition never fired, not a data problem. "
            f"Try lowering BN_ALERT_CONSENSUS_REQUIRED or the per-stock point thresholds in "
            f"Settings → BN Alerts to see how much data it would take to hit."
        )
    return result
