from __future__ import annotations

"""
Shared helpers for per-symbol indicator-snapshot entries.

The same stub shape and order-book merge are needed in three places — the 1s
STATE_UPDATE payload, the 100ms INDICATOR_UPDATE delta push, and the REST
/api/indicators endpoint — so they live here instead of being copy-pasted.
If you add new fields to the snapshot, add them to the stub here.
"""


def stub_entry() -> dict:
    """Placeholder row for a stock the scanner hasn't processed yet (LTP set by caller)."""
    return {
        "bar_time": "—",
        "rsi": None, "adx": None, "plus_di": None, "minus_di": None,
        "macd": None, "macd_signal": None, "macd_hist": None,
        "support": None, "vwap": None, "above_vwap": None, "pattern": None,
    }


def apply_depth(entry: dict, depth: dict) -> dict:
    """
    Overlay live order-book fields onto a snapshot entry. Keys missing from the
    depth dict keep the entry's existing value (or become None so the key is
    always present in the payload — the frontend expects it).
    """
    if depth:
        entry["bid"] = round(depth["bid"], 2) if "bid" in depth else entry.get("bid")
        entry["ask"] = round(depth["ask"], 2) if "ask" in depth else entry.get("ask")
        for k in ("spread", "buy_qty", "sell_qty", "ratio"):
            entry[k] = depth[k] if k in depth else entry.get(k)
    return entry
