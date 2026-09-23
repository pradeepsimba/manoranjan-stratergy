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
