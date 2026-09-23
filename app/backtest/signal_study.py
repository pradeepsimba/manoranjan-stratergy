from __future__ import annotations

"""
Leader-consensus signal study — NOT part of the main options P&L backtest
(app/backtest/engine.py). Answers a narrower question: when N-of-6 BN leader
stocks both cross their own BN_PRICE_ALERT_PTS_* threshold (raw points, not
%) AND agree on direction on bar T (the EXACT same condition
app/services/price_alerts.py's check_consensus fires the live ALERT
WebSocket push on), how often does BankNifty's own bar T+1 move the same
direction, and by how much?

Synchronous — a handful of days x ~75 bars x 6 stocks, no option pricing —
so unlike the real backtest this needs no run_id/polling/DB persistence,
just a direct request/response.

Data sources mirror what the live engine and the real backtest already use:
the self-recorded bn_index_bars archive for the index (the vendor gives no
historical BankNifty index data at all — see CLAUDE.md), and a REST fetch
for the 6 leader stocks over that same span (fully archived on the vendor
side, unlike the index).
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import app.config as cfg
from app.models import Candle
from app.services.historical_data import fetch_indicator_history

IST = ZoneInfo("Asia/Kolkata")


def _leader_signal(bar_by_token: Dict[str, Candle]) -> List[Dict[str, Any]]:
    """Python mirror of static/js/alerts.js's checkPriceAlerts crossed/direction check —
    used by mode="threshold" (a leader must ALSO cross its own move-alert points)."""
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


def _leader_direction(bar_by_token: Dict[str, Candle]) -> List[Dict[str, Any]]:
    """Plain green/red vote — a standalone close-vs-open direction check (no
    move-alert threshold/magnitude involved), evaluated over history here.
    Used by mode="direction": "N of 6 leaders simply closed the same color".
    (2026-09-23 fix, found in review: this used to describe itself as
    "mirrors bn_signals.leaders_momentum's direction check" — bn_signals.py
    was fully deleted 2026-09-21, see CLAUDE.md; nothing here has imported
    from it in a long time, this was just a stale pointer.)"""
    results = []
    for name, token in cfg.BN_LEADER_STOCKS.items():
        bar = bar_by_token.get(token)
        if bar is None or not bar.open or not bar.close:
            continue
        direction = "up" if bar.close > bar.open else ("down" if bar.close < bar.open else None)
        results.append({"name": name, "dir": direction})
    return results


async def run_bn_leader_consensus_study(
    db, mode: str = "threshold", days_back: Optional[int] = None,
    required: Optional[int] = None,
) -> Dict[str, Any]:
    """
    mode="threshold" (default, original behavior): a leader only counts if it
    BOTH closed red/green AND crossed its own BN_PRICE_ALERT_PTS_* threshold
    — the exact condition app/services/price_alerts.py's check_consensus
    fires the live ALERT WebSocket push on.
    mode="direction": a leader counts on plain close-vs-open color alone, no
    magnitude requirement — a standalone direction-only variant of the same
    N-of-6 consensus idea, evaluated over history here (not tied to any gate
    in the live entry decision — the 2026-09-21 scalp rewrite replaced the
    live engine's own leader-vote gate entirely; see CLAUDE.md).

    days_back=None (default) scans the full self-recorded bn_index_bars
    archive (this repo's only source of BankNifty index history — see
    CLAUDE.md); a number restricts to the most recent N days of it.

    required=None defaults to cfg.BN_ALERT_CONSENSUS_REQUIRED for BOTH
    modes (2026-09-23 fix, found in review: mode="direction" used to default
    to cfg.BN_SAME_DIRECTION_REQUIRED, a constant sized for the OLD 14-stock
    leader-vote population — this tool's _leader_direction/_leader_signal
    both only ever iterate cfg.BN_LEADER_STOCKS, exactly 6 entries, so that
    default of 9 was mathematically unreachable and silently returned
    total_signals: 0 on every unparameterized "direction" call. The API
    layer's own validation, dashboard.py's `1 <= required <= 6`, already
    only accepts values sized for THIS 6-stock population when `required`
    is passed explicitly — this default now matches that bound instead of
    bypassing it).
    """
    if days_back is not None:
        from_iso = (datetime.now(IST) - timedelta(days=days_back)).isoformat()
    else:
        from_iso = "2000-01-01T00:00:00"
    index_bars = await db.get_bn_index_bars(from_iso, "2100-01-01T00:00:00")
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

    if required is None:
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

        if mode == "direction":
            results = _leader_direction(leaders_now)
            up_count = sum(1 for r in results if r["dir"] == "up")
            down_count = sum(1 for r in results if r["dir"] == "down")
        else:
            results = _leader_signal(leaders_now)
            up_count = sum(1 for r in results if r["crossed"] and r["dir"] == "up")
            down_count = sum(1 for r in results if r["crossed"] and r["dir"] == "down")
        max_up_count = max(max_up_count, up_count)
        max_down_count = max(max_down_count, down_count)

        # Independent ifs, not if/elif (2026-09-23 fix, found in review):
        # up_count + down_count <= 6, so both can independently clear a
        # `required` of 3 or less on the same bar (e.g. 3 up / 3 down) — the
        # old elif silently only ever counted that bar as an "up" signal,
        # dropping a legitimate down-signal bar from signals_down/hits_down
        # and skewing win_rate_down low. Unreachable at the current default
        # (BN_ALERT_CONSENSUS_REQUIRED=4, and 4+4>6), but real for any
        # smaller `required` passed explicitly.
        if up_count >= required:
            signals_up += 1
            move = next_bar.close - next_bar.open
            move_points_up.append(move)
            if next_bar.close > next_bar.open:
                hits_up += 1
        if down_count >= required:
            signals_down += 1
            move = next_bar.close - next_bar.open
            move_points_down.append(move)
            if next_bar.close < next_bar.open:
                hits_down += 1

    result = {
        "mode": mode,
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
        setting_hint = ("BN_SAME_DIRECTION_REQUIRED" if mode == "direction"
                        else "BN_ALERT_CONSENSUS_REQUIRED or the per-stock point thresholds in Settings → BN Alerts")
        result["note"] = (
            f"Scanned {bars_scanned} bars ({bars_with_leader_data} had matching leader data). "
            f"Closest it ever got: {max(max_up_count, max_down_count)} of 6 leaders agreed "
            f"(need {required}) — the condition never fired, not a data problem. "
            f"Try lowering {setting_hint} to see how much data it would take to hit."
        )
    return result
