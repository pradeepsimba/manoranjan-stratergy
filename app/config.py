from __future__ import annotations

"""
Configuration — static system settings plus the DYNAMIC tunables layer.

Static values (endpoints, credentials, structural pool/buffer sizes, the
Bank Nifty instrument universe) are plain module attributes and require a
restart to change.

Everything else lives in _DEFAULTS and is resolved through the module-level
__getattr__ (PEP 562) with this precedence:

    1. thread-local overrides  — a running backtest's per-run parameters,
                                 active only inside its worker threads
    2. runtime overrides       — dashboard Settings page, persisted in the
                                 app_settings table and applied at startup
    3. the hard default below

`import app.config as cfg; cfg.BN_TARGET_POINTS` therefore always returns the
CURRENT value. Code must read cfg attributes at call time — never copy them
into module-level constants or default-argument values, or they freeze at
import and stop being dynamic.

The editable registry (labels, types, bounds, grouping) lives in
app/services/settings.py — add new tunables in BOTH places.
"""

import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

# ── Static: custom market data server ────────────────────────────────────────
API_HOST          = "35.234.219.141"
API_URL_TEMPLATE  = "https://{}:8000/api/historical-data/?from_date={}&to_date={}"
WS_URL            = f"ws://{API_HOST}:8083/historical-data"

# ── Static: credentials / DSN ─────────────────────────────────────────────────
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://postgres:password@localhost/trading_db",
)

# ── Static: data intervals ────────────────────────────────────────────────────
INTERVAL_5M = "5m"

# ── Static: Bank Nifty options strategy universe ──────────────────────────────
# The vendor migrated its WS/REST protocol from numeric Kite-style instrument
# tokens to real NSE trading-symbol strings (verified directly against the
# live server, 2026-07-23) — the OLD numeric tokens now silently return
# nothing for most instruments. BN_INDEX_TOKEN/BN_ALL_STOCKS values below are
# the new trading symbols; dict KEYS (our own internal display names) did
# NOT need to change — confirmed the vendor's stockname-matching accepts our
# existing ALL-CAPS names fine when paired with the new stock_symbol.
#
# BankNifty index itself now returns ZERO data (live or historical) under
# either the old or new identifier — the vendor appears to have stopped
# providing it entirely. BN_INDEX_TOKEN is kept pointed at the new symbol
# anyway so the app auto-recovers if the vendor ever resumes streaming it;
# in the meantime app/services/market_data.py synthesizes the index candle
# from these 11 stocks' BN_INDEX_WEIGHTS-weighted % change (see
# MarketDataService._update_synthetic_index).
BN_INDEX_NAME = "BANKNIFTY"
BN_INDEX_TOKEN = "NIFTY BANK"   # was "26009" — dead under the new protocol too

# The 6 stocks that actually drive the trade decision (leader-vote + BN
# composite indicator gate).
BN_LEADER_STOCKS: Dict[str, str] = {
    "HDFC BANK":            "HDFCBANK",     # was "1333"
    "ICICI BANK":           "ICICIBANK",    # was "4963"
    "AXIS BANK":            "AXISBANK",     # was "5900"
    "STATE BANK OF INDIA":  "SBIN",         # was "3045"
    "KOTAK BANK":           "KOTAKBANK",    # was "1922" — server's canonical name for this stock (NOT "Kotak Mahindra Bank")
    "INDUSIND BANK":        "INDUSINDBK",   # was "5258"
}

# Exact c.html STOCK_QTY_THRESHOLD table (per-stock, at 1m granularity),
# mapped onto this repo's leader-stock names (Kotak's key here is "KOTAK
# BANK", not c.html's "KOTAK MAHINDRA BANK" — same stock, see the Kotak
# naming gotcha above). c.html compares these against a raw per-trade qty
# field; the new vendor protocol finally exposes one too (embedded in each
# tick's `quote` text, parsed into Candle.last_qty — see market_data.py),
# so this threshold table is now used against real per-trade quantities
# again, not the bar-volume proxy this repo used while that field was
# unavailable.
#
# The actual threshold VALUES are dynamic tunables (see _DEFAULTS below,
# BN_QTY_THRESHOLD_* keys) — editable live from the Settings page. This map
# is just the static "which stock uses which settings key" wiring, not a
# tunable itself.
BN_QTY_THRESHOLD_ATTR: Dict[str, str] = {
    "HDFC BANK":            "BN_QTY_THRESHOLD_HDFC",
    "ICICI BANK":           "BN_QTY_THRESHOLD_ICICI",
    "STATE BANK OF INDIA":  "BN_QTY_THRESHOLD_SBI",
    "AXIS BANK":            "BN_QTY_THRESHOLD_AXIS",
    "KOTAK BANK":           "BN_QTY_THRESHOLD_KOTAK",
    "INDUSIND BANK":        "BN_QTY_THRESHOLD_INDUSIND",
}

# All 11 stocks fetched/displayed (matches c.html's own universe, 12 tokens
# total together with the index) — the 6
# beyond the leaders never feed the entry decision but are kept for display /
# future use per an explicit user decision, not because they're needed.
BN_ALL_STOCKS: Dict[str, str] = {
    **BN_LEADER_STOCKS,
    "AU SMALL FINANCE BANK": "AUBANK",      # was "21238"
    "FEDERAL BANK":          "FEDERALBNK",  # was "1023"
    "IDFC FIRST BANK":       "IDFCFIRSTB",  # was "11184"
    "PUNJAB NATIONAL BANK":  "PNB",         # was "10666"
    "CANARA BANK":           "CANBK",       # was "10794"
}

# Exact c.html INDEX_WEIGHTS table (Nifty Bank per-stock weight, % as of
# the source's "Oct 30, 2025" snapshot) — keyed by the same trading-symbol
# strings as BN_ALL_STOCKS' values (c.html keys this by `stock_symbol` too;
# this repo's candles_5m is likewise stock_symbol-keyed — see CLAUDE.md's
# "candles_5m is keyed by TOKEN" convention, now token = trading symbol).
# Same 11 stocks as BN_ALL_STOCKS, no new universe needed. Used for the
# weighted global-signal/contribution-analysis port (app/engine/bn_breakout.py)
# and the synthetic BankNifty index candle (app/services/market_data.py).
BN_INDEX_WEIGHTS: Dict[str, float] = {
    "HDFCBANK":   31.86,   # HDFC BANK
    "ICICIBANK":  20.14,   # ICICI BANK
    "SBIN":       17.83,   # STATE BANK OF INDIA
    "KOTAKBANK":  8.79,    # KOTAK BANK
    "AXISBANK":   7.96,    # AXIS BANK
    "INDUSINDBK": 2.92,    # INDUSIND BANK
    "PNB":        2.86,    # PUNJAB NATIONAL BANK
    "CANBK":      2.40,    # CANARA BANK
    "IDFCFIRSTB": 1.40,    # IDFC FIRST BANK
    "AUBANK":     1.35,    # AU SMALL FINANCE BANK
    "FEDERALBNK": 1.19,    # FEDERAL BANK
}

# BankNifty exchange lot size — a contract-spec fact, not a user tunable.
BN_LOT_SIZE = 30

# ── Static: structural sizes (pools/buffers built once — restart to change) ──
HIST_BATCH_SIZE   = 100   # max stocks per single historical API request
MAX_CANDLE_BUFFER = 300   # per-symbol in-memory candle buffer (deque maxlen)

# Backtest v1 is intraday/5m only — nothing in c.html holds an option position
# across days, so positional (delivery / 1d) replay is not built.
BACKTEST_TIMEFRAMES = ["5m"]
BACKTEST_MODES      = ["intraday"]
SCAN_WORKERS        = 4    # per-day backtest parallelism (ThreadPoolExecutor)

# ── Dynamic tunables — hard defaults ──────────────────────────────────────────
_DEFAULTS: Dict[str, Any] = {
    # Session timings (IST) — SCAN_START/CUTOFF reproduce c.html's real
    # 09:30-15:00 trading window using the existing phase-driver machinery.
    "PREMARKET_HOUR":   9,  "PREMARKET_MIN":   0,
    "MARKET_OPEN_HOUR": 9,  "MARKET_OPEN_MIN": 15,   # historical load + WS subscribe
    "SCAN_START_HOUR":  9,  "SCAN_START_MIN":  30,   # entries allowed from here
    "CUTOFF_HOUR":      15, "CUTOFF_MIN":      0,    # no new entries after this
    "SESSION_END_HOUR": 15, "SESSION_END_MIN": 30,   # terminate session

    # BN Strategy — sideways / momentum / leader-vote / volume-surge gates
    "BN_SIDEWAYS_RANGE_MIN":   12.0,   # min 5-bar BankNifty close range to trade
    "BN_MOMENTUM_THRESHOLD":   28.0,   # fixed 5m momentum threshold (points)
    "BN_ATR_PERIOD":           10,
    "BN_SAME_DIRECTION_REQUIRED": 3,   # of 6 leaders must agree
    "BN_ENTRY_COOLDOWN_S":     60,     # no new entry within this long of the last exit

    # BN Strategy — per-stock volume-surge thresholds, compared against each
    # leader's latest 5m bar volume (see BN_QTY_THRESHOLD_ATTR above and
    # bn_entry_exit._leader_qty_surge). Calibrated 2026-07-27 from ~15 live
    # bars/stock (~1.5x each stock's observed average bar volume, so a
    # genuine spike is needed to fire, not every bar):
    #   HDFC ~37.5k avg -> 55k | ICICI ~32.3k avg -> 48k | AXIS ~18.7k avg -> 28k
    #   SBI ~11.8k avg -> 18k  | KOTAK ~28.7k avg -> 43k | INDUSIND ~11k avg -> 16.5k
    "BN_QTY_THRESHOLD_HDFC":     55_000.0,
    "BN_QTY_THRESHOLD_ICICI":    48_000.0,
    "BN_QTY_THRESHOLD_SBI":      18_000.0,
    "BN_QTY_THRESHOLD_AXIS":     28_000.0,
    "BN_QTY_THRESHOLD_KOTAK":    43_000.0,
    "BN_QTY_THRESHOLD_INDUSIND": 16_500.0,
    "BN_QTY_INTERVAL_MULTIPLIER": 1.0,

    # BN Strategy — composite indicator gate (RSI/MACD/EMA/pattern scoring)
    "BN_INDICATOR_LOOKBACK_BARS": 200,
    "BN_RSI_PERIOD":       14,
    "BN_EMA_FAST":         20,
    "BN_EMA_SLOW":         50,
    "BN_MACD_FAST":        12,
    "BN_MACD_SLOW":        26,
    "BN_RSI_BULL_LEVEL":   58,
    "BN_RSI_BEAR_LEVEL":   42,
    "BN_RSI_OVERBOUGHT":   72,
    "BN_RSI_OVERSOLD":     28,
    "BN_EMA_EXTENSION_PCT": 1.2,
    "BN_SCORE_MIN":        2.0,
    "BN_SCORE_MARGIN":     0.9,

    # BN Risk — target/stop/trailing on the underlying BankNifty index (points)
    "BN_TARGET_POINTS":     35.0,
    "BN_STOPLOSS_POINTS":   18.0,
    "BN_BREAKEVEN_TRIGGER": 12.0,
    "BN_TRAIL_TRIGGER":     18.0,
    "BN_TRAIL_DISTANCE":    12.0,
    "BN_STARTING_FUNDS":    100_000.0,   # ₹ — seeds the persisted funds balance once

    # BN Options Pricing — synthetic Black-Scholes premium, no real option data
    "BN_RISK_FREE_RATE": 0.065,
    "BN_IV_MIN":         0.20,
    "BN_IV_MAX":         0.70,
    "BN_IV_DEFAULT":     0.28,
    "BN_IV_LOOKBACK_BARS": 50,
    "BN_IV_MANUAL_ENABLED": False,
    "BN_IV_MANUAL_VALUE":   0.30,

    # BN Options Costs — placeholder rates (India options STT/txn charges
    # change periodically; confirm current figures before trusting absolute
    # backtest ₹ P&L — relative signal quality is insensitive to this).
    "BN_COST_BROKERAGE_FLAT": 20.0,      # ₹ per executed order, flat
    "BN_COST_STT_SELL_PCT":   0.001,     # STT on sell-side premium value
    "BN_COST_TXN_PCT":        0.0005,    # exchange transaction charge
    "BN_COST_GST_PCT":        0.18,      # GST on (brokerage + txn)
    "BN_COST_SEBI_PCT":       0.000001,  # SEBI turnover fee

    # Tick-wise engine
    "TICK_EVAL_INTERVAL_MS": 100,

    # Backtest
    "BACKTEST_WARMUP_DAYS": 7,
    "SLIPPAGE_BPS":         2.0,
}

_runtime_overrides: Dict[str, Any] = {}
_thread_ctx = threading.local()

# Bumped on every runtime-override mutation (Settings page apply/reset) — the
# single choke point for "did a dynamic tunable change".
_settings_generation = 0


def __getattr__(name: str) -> Any:
    """PEP 562 resolver for dynamic tunables (static attrs never reach here)."""
    try:
        default = _DEFAULTS[name]
    except KeyError:
        raise AttributeError(
            f"module 'app.config' has no attribute {name!r}"
        ) from None
    local = getattr(_thread_ctx, "overrides", None)
    if local is not None and name in local:
        return local[name]
    return _runtime_overrides.get(name, default)


def __dir__() -> List[str]:
    return sorted(list(globals().keys()) + list(_DEFAULTS.keys()))


# ── Runtime-override management (Settings page / DB) ──────────────────────────

def is_dynamic(name: str) -> bool:
    return name in _DEFAULTS


def dynamic_defaults() -> Dict[str, Any]:
    return dict(_DEFAULTS)


def runtime_overrides() -> Dict[str, Any]:
    return dict(_runtime_overrides)


def settings_generation() -> int:
    return _settings_generation


def set_runtime_overrides(changes: Dict[str, Any]) -> None:
    """Apply validated overrides globally (event-loop callers only)."""
    global _settings_generation
    unknown = set(changes) - set(_DEFAULTS)
    if unknown:
        raise KeyError(f"unknown config keys: {sorted(unknown)}")
    _runtime_overrides.update(changes)
    _settings_generation += 1


def clear_runtime_overrides(keys: Optional[List[str]] = None) -> None:
    global _settings_generation
    if keys is None:
        _runtime_overrides.clear()
    else:
        for k in keys:
            _runtime_overrides.pop(k, None)
    _settings_generation += 1


# ── Per-thread overrides (backtest workers ONLY — never the event loop) ──────

@contextmanager
def thread_overrides(overrides: Dict[str, Any]) -> Iterator[None]:
    """
    Scope config overrides to the current thread. Used by backtest day-workers
    so a run's parameters never leak into the live engine, whose event loop
    keeps reading the global runtime values.
    """
    prev = getattr(_thread_ctx, "overrides", None)
    merged = dict(prev) if prev else {}
    merged.update(overrides)
    _thread_ctx.overrides = merged
    try:
        yield
    finally:
        _thread_ctx.overrides = prev
