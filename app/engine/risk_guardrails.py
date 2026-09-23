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
