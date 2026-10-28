from __future__ import annotations

"""
Shared BN/NF risk guardrails — pure, no state (same "small dedicated pure-
function module" pattern as scalp_signals.py/wobi.py).

Extracted 2026-09-23 (found in review): in_trading_window used to be a
byte-identical function copy-pasted into both bn_entry_exit.py and
nf_entry_exit.py, each docstring calling it a "shared risk guardrail" while
actually being two independently-editable copies — exactly the kind of
drift this repo's shared-decision-core convention (CLAUDE.md) exists to
prevent. One implementation now, imported by both.

max_trades_ok joined it the same day (found in review): the same
copy-paste-despite-being-"shared" pattern existed for the daily-trade-cap
check (`trades_today < cfg.SCALP_MAX_TRADES_PER_DAY`), a few lines below
in_trading_window in both files — the exact class of drift this module
exists to prevent, just missed in the first pass.
"""

from datetime import datetime, time

import app.config as cfg


def in_trading_window(now: datetime) -> bool:
    """
    Entries only inside the two configured windows (default 09:45-11:15 /
    13:45-14:45 IST, dynamic since 2026-09-23 — see config.py's SCALP_WINDOW1/
    2_*). Shared by BN and NF (one set of windows, not a pair per instrument).
    """
    t = now.time()
    w1 = (time(cfg.SCALP_WINDOW1_START_HOUR, cfg.SCALP_WINDOW1_START_MIN)
          <= t <= time(cfg.SCALP_WINDOW1_END_HOUR, cfg.SCALP_WINDOW1_END_MIN))
    w2 = (time(cfg.SCALP_WINDOW2_START_HOUR, cfg.SCALP_WINDOW2_START_MIN)
          <= t <= time(cfg.SCALP_WINDOW2_END_HOUR, cfg.SCALP_WINDOW2_END_MIN))
    return w1 or w2


def max_trades_ok(trades_today: int) -> bool:
    """Daily trade cap — one shared cap (`cfg.SCALP_MAX_TRADES_PER_DAY`), not
    a pair per instrument, same as in_trading_window above."""
    return trades_today < cfg.SCALP_MAX_TRADES_PER_DAY


def day_candle_color(candles: list, current_price: float, today: str) -> str:
    """
    "GREEN"/"RED"/"FLAT" — today's day candle SO FAR for one instrument:
    today's first 5m bar's open vs `current_price` (the trade's own entry
    index price). "" if no bar for `today` exists yet in `candles` (e.g.
    right at WAIT_ZONE before the first candle has printed).

    Deliberately NOT threaded through evaluate_entry/BNSignal like
    basket_score/wobi/top2_names — this is a pure diagnostic snapshot that
    plays no part in the trading decision, so it's computed at the CALLER
    level (bn_trade.py/nf_trade.py's place_paper_order, which already has
    direct AppState access to the full multi-day candle buffer) rather than
    widening evaluate_entry's own signature, matching CLAUDE.md's "do it at
    the caller level, never by branching inside the shared functions" rule.
    `candles` is expected to be the FULL buffer (st.bn_index_candles_5m/
    nf_index_candles_5m, which persists across days), not a recent-tail
    slice — evaluate_entry's own bn_recent_candles (last 5 bars only) is
    NOT sufficient once the day is more than 5 bars old.
    """
    # A non-positive price means "no live tick yet" (e.g. right after a restart), not a real
    # price of 0 - without this it compared below every day_open and reported a bogus "RED",
    # which the trend filters would then act on. "" is what every caller already treats as
    # "no data" (the filters fail open on it).
    if current_price <= 0:
        return ""
    todays = [c for c in candles if c.start_time[:10] == today]
    if not todays:
        return ""
    day_open = todays[0].open
    if current_price > day_open:
        return "GREEN"
    if current_price < day_open:
        return "RED"
    return "FLAT"


def is_flat_market(candles: list, lookback_bars: int, min_range_points: float) -> bool:
    """
    True when the index has moved less than `min_range_points` (high-low
    range) over the last `lookback_bars` CLOSED bars — a cheap proxy for
    "nothing is really happening right now." Added 2026-10-23 (explicit
    user decision) after a real incident (2026-10-22: BankNifty traded the
    SAME strike all day, 20% win rate, 5 straight stop-outs in one ~2-hour
    stretch) raised the question of whether a flat-market filter would help.

    Checked directly against that day's own self-recorded 5m bars before
    building this: that stretch's range was actually NORMAL-TO-HIGHER than
    several genuinely good trading days, not unusually quiet — the problem
    there was no NET direction (chop), not literally low movement. This
    filter catches the "nothing moving" case specifically, not general chop
    — don't expect it alone to prevent a repeat of that exact day.
    `BN_REQUIRE_MIN_VOLATILITY`/`NF_REQUIRE_MIN_VOLATILITY` gate whether
    this runs at all — OFF by default (unlike the trend-fade filter, this
    hasn't been validated against real trade history, just sanity-checked).

    `candles[-(lookback_bars+1):-1]` — same closed-bar-only slicing
    convention as models.closed_tail (excludes the still-forming last bar,
    mutated in place tick by tick, which would understate this exact tick's
    own range). Returns False (never blocks) if fewer than `lookback_bars`
    closed bars exist yet — same fail-open choice in_trading_window/
    day_candle_color make for insufficient data; this filter should never
    be what silently freezes entries during the first few bars of a session.
    """
    closed = candles[-(lookback_bars + 1):-1]
    if len(closed) < lookback_bars:
        return False
    return (max(c.high for c in closed) - min(c.low for c in closed)) < min_range_points


def which_window(now: datetime) -> str:
    """
    Which of the two configured scalp windows `now` actually falls inside —
    "WINDOW1"/"WINDOW2", or "" if neither (shouldn't happen for a real
    entry, since open_trade_from_signal only ever runs after
    in_trading_window has already gated it true, but returns a safe empty
    string rather than raising if ever called outside that guarantee).
    Added 2026-09-29 (explicit user decision) so a trade can record WHICH
    window it fired in, not just that it was inside "a" window.
    """
    t = now.time()
    if (time(cfg.SCALP_WINDOW1_START_HOUR, cfg.SCALP_WINDOW1_START_MIN)
            <= t <= time(cfg.SCALP_WINDOW1_END_HOUR, cfg.SCALP_WINDOW1_END_MIN)):
        return "WINDOW1"
    if (time(cfg.SCALP_WINDOW2_START_HOUR, cfg.SCALP_WINDOW2_START_MIN)
            <= t <= time(cfg.SCALP_WINDOW2_END_HOUR, cfg.SCALP_WINDOW2_END_MIN)):
        return "WINDOW2"
    return ""


def trading_window_description() -> str:
    """
    Human-readable rendering of the CURRENT live window config, e.g.
    "09:45-11:15 / 13:45-14:45 IST" — added 2026-09-23 (found in review) so
    the "Outside scalp trading window" diagnostic/rejection text (entry
    diagnostics + place_paper_order's ValueError, in both BN and NF) can
    read the same dynamic cfg.SCALP_WINDOW1/2_* values in_trading_window
    itself already gates on, instead of a hardcoded default-value string.
    The gating logic was always correctly live (in_trading_window reads cfg
    fresh every call) — only the displayed/raised TEXT was stale after a
    Settings-page change to these windows, silently misleading whoever's
    reading the dashboard's "why didn't it fire" panel or a manual-order
    rejection about what window is actually configured.
    """
    def _fmt(h: int, m: int) -> str:
        return f"{h:02d}:{m:02d}"
    w1 = f"{_fmt(cfg.SCALP_WINDOW1_START_HOUR, cfg.SCALP_WINDOW1_START_MIN)}-" \
         f"{_fmt(cfg.SCALP_WINDOW1_END_HOUR, cfg.SCALP_WINDOW1_END_MIN)}"
    w2 = f"{_fmt(cfg.SCALP_WINDOW2_START_HOUR, cfg.SCALP_WINDOW2_START_MIN)}-" \
         f"{_fmt(cfg.SCALP_WINDOW2_END_HOUR, cfg.SCALP_WINDOW2_END_MIN)}"
    return f"{w1} / {w2} IST"
