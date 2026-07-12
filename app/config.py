from __future__ import annotations

"""
Configuration — static system settings plus the DYNAMIC tunables layer.

Static values (endpoints, credentials, structural pool/buffer sizes) are plain
module attributes and require a restart to change.

Everything else lives in _DEFAULTS and is resolved through the module-level
__getattr__ (PEP 562) with this precedence:

    1. thread-local overrides  — a running backtest's per-run parameters,
                                 active only inside its worker threads
    2. runtime overrides       — dashboard Settings page, persisted in the
                                 app_settings table and applied at startup
    3. the hard default below

`import app.config as cfg; cfg.RISK_PER_TRADE` therefore always returns the
CURRENT value. Code must read cfg attributes at call time — never copy them
into module-level constants or default-argument values, or they freeze at
import and stop being dynamic.

The editable registry (labels, types, bounds, grouping) lives in
app/services/settings.py — add new tunables in BOTH places.
"""

import itertools
import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

# ── Static: custom market data server ────────────────────────────────────────
API_HOST          = "35.234.219.141"
API_URL_TEMPLATE  = "https://{}:8000/api/historical-data/?from_date={}&to_date={}"
WS_URL            = f"ws://{API_HOST}:8083/historical-data"
CLIENT_STATUS_URL = f"https://{API_HOST}:8000/api/clientstatus/"

# ── Static: credentials / DSN ─────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
POSTGRES_DSN   = os.getenv(
    "POSTGRES_DSN",
    "postgresql://postgres:password@localhost/trading_db",
)

# ── Static: data intervals + NIFTY identity ───────────────────────────────────
INTERVAL_5M   = "5m"
# Live 1h store key: kept as "1h" to MATCH the WebSocket's hardcoded "1h" ticks
# that fill candles_1h during the session. The initial REST load with this id
# returns nothing (the REST server's hourly id is "60m") and the store is
# WS-filled — but it must NOT be "60m" here, or REST hourly bars and WS "1h"
# bars would mix in the same store with possibly misaligned timestamps. The
# backtest/viewer/MTF use TIMEFRAMES (below) directly, not this constant.
INTERVAL_1H   = "1h"
NIFTY50_TOKEN = "99926000"
NIFTY50_NAME  = "NIFTY 50"

# Supported timeframes for the REST-historical features (indicators viewer/MTF,
# backtest) — MUST match the market-data REST server's interval ids exactly.
# PROBED against the live server (2026-07-05): it serves 1m, 3m, 5m, 10m, 15m,
# 30m, 60m, 1d — and silently returns ZERO candles for any other id ("1h",
# "1hr", "1hour", "1day", "2m", …), so a wrong name here looks like missing
# data, not an error. Order = UI display order.
TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d"]

# Approximate minutes per bar — for warmup-day math. 1d uses the NSE session
# length (~375 min) so a "day" of lookback still spans real bars.
TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30,
    "60m": 60, "1d": 375,
}


def is_timeframe(tf: str) -> bool:
    return tf in TIMEFRAMES


# Timeframes the backtest engine can replay. Intraday ids (≤60m) use the
# parallel per-day engine (fresh portfolio, EOD square-off); "1d" uses the
# POSITIONAL mode (one portfolio across the range, overnight holds, exits on
# later days' bars, square-off at range end).
BACKTEST_TIMEFRAMES = list(TIMEFRAMES)

# How a backtest holds positions. "intraday" = per-day engine, EOD square-off.
# "delivery" = positional: one portfolio across the range, overnight holds,
# square-off at range end. The "1d" timeframe is positional by construction
# (its bars ARE days), so it replays as delivery regardless of this choice.
BACKTEST_MODES = ["intraday", "delivery"]

# Per-trade risk basis choices (see RISK_MODE in _DEFAULTS).
RISK_MODES = ["fixed_amount", "capital_pct"]

# ── Static: structural sizes (pools/buffers built once — restart to change) ──
HIST_BATCH_SIZE   = 100   # max stocks per single historical API request
SCAN_WORKERS      = 16    # ThreadPoolExecutor size for the parallel scan
MAX_CANDLE_BUFFER = 300   # per-symbol in-memory candle buffer (deque maxlen)

# ── Dynamic tunables — hard defaults ──────────────────────────────────────────
_DEFAULTS: Dict[str, Any] = {
    # AI pre-market screen
    "GEMINI_ENABLED":    True,
    "GEMINI_MODEL":      "gemini-2.5-flash",
    "GEMINI_MAX_STOCKS": 40,     # cap on the bullish shortlist / fallback list

    # Timing (IST)
    "PREMARKET_HOUR":   9,  "PREMARKET_MIN":   0,    # Gemini filter runs here
    "MARKET_OPEN_HOUR": 9,  "MARKET_OPEN_MIN": 15,   # Wait zone start
    "SCAN_START_HOUR":  9,  "SCAN_START_MIN":  45,   # Active scanning starts
    "CUTOFF_HOUR":      14, "CUTOFF_MIN":      30,   # No new entries after this
    "SESSION_END_HOUR": 15, "SESSION_END_MIN": 30,   # Terminate session

    # Risk & capital
    # How the per-trade risk (the ₹ lost when a stop hits) is defined:
    #   "fixed_amount" — RISK_PER_TRADE ₹ per setup (original blueprint)
    #   "capital_pct"  — RISK_CAPITAL_PCT × account capital per setup
    # Stop PLACEMENT is unchanged in both modes (structural swing-low stop,
    # floored at MIN_SL_OFFSET); the mode only changes how many shares are
    # sized against that stop distance.
    "RISK_MODE":                "fixed_amount",
    "RISK_PER_TRADE":           500.0,     # ₹ fixed risk capital per setup
    "RISK_CAPITAL_PCT":         0.02,      # 0.02 = a stop-out loses 2% of capital
    "ACCOUNT_BALANCE":          40_000.0,  # ₹ base capital
    "INTRADAY_LEVERAGE":        5,         # Standard NSE intraday equity leverage
    "MAX_CONCURRENT_POSITIONS": 3,         # Hard cap on simultaneous open positions
    "DAILY_LOSS_LIMIT":         2_000.0,   # ₹ daily drawdown ceiling

    # Strategy parameters
    "ADX_PERIOD":        14,
    "ADX_THRESHOLD":     20.0,
    "RSI_PERIOD":        14,
    "RSI_OVERSOLD":      30,      # the RSI level ("30") the RSI rule compares against
    "RSI_RISING_BARS":   3,       # RSI must rise for this many consecutive bars
    # How the RSI entry condition uses the level above:
    #   "above_or_rising" — RSI > level OR rising N bars (default / original)
    #   "above"           — RSI > level only
    #   "below"           — RSI < level (oversold-bounce entry)
    "RSI_MODE":          "above_or_rising",
    "SWING_LOW_BARS":    10,      # Lookback bars for structural support floor
    "SUPPORT_TOUCH_PCT": 0.015,   # Price within 1.5% of support = "at support"
    "MIN_SL_OFFSET":     5.0,     # Minimum SL distance in ₹
    "VOLUME_MA_PERIOD":  20,
    "VOLUME_MULTIPLIER": 1.5,     # Bar volume must exceed this × the volume MA
    "RR_RATIO":          1.5,     # target_offset = sl_offset × RR_RATIO
    "MACD_CROSS_BARS":   3,       # Allow entry up to N bars after a bullish cross
    "MACD_FAST":         12,      # MACD fast EMA period
    "MACD_SLOW":         26,      # MACD slow EMA period
    "MACD_SIGNAL":       9,       # MACD signal EMA period
    "DEPTH_MIN_RATIO":   0.4,     # Order-book buy-side ratio floor (live only)
    # Tail length fed to TA-Lib per scan. 120 bars lets RSI(14)/ADX(14)/MACD(26,9)
    # fully converge (Wilder smoothing) while skipping multi-day warmup history.
    "TALIB_LOOKBACK":    120,

    # Entry-condition toggles (all 8 required when enabled; disabled = auto-pass)
    "COND_NEAR_SUPPORT":    True,
    "COND_BULLISH_PATTERN": True,
    "COND_ADX":             True,
    "COND_RSI":             True,
    "COND_MACD_CROSS":      True,
    "COND_VOLUME_SURGE":    True,
    "COND_ABOVE_VWAP":      True,
    "COND_DEPTH":           True,

    # Trend-gate toggles (disabled gate = treated as green)
    "GATE_STOCK_DAILY":  True,
    "GATE_STOCK_HOURLY": True,
    "GATE_NIFTY_DAILY":  True,
    "GATE_NIFTY_VWAP":   True,

    # Custom entry rules (OR-of-ANDs; see app/engine/conditions.py).
    # mode "and" = extra condition on top of the fixed 8; "replace" = rules
    # replace the fixed conditions (trend gates still apply). Treat the dict as
    # IMMUTABLE — validation returns fresh copies; never mutate in place.
    "CUSTOM_ENTRY_RULES": {"enabled": False, "mode": "and", "groups": []},

    # Tick-wise engine
    "TICK_EVAL_INTERVAL_MS": 100,   # cadence of the ACTIVE evaluation loop
    "FULL_SCAN_INTERVAL_S":  300,   # full-watchlist indicator refresh cadence

    # Backtest
    "BACKTEST_TIMEFRAME":   "5m",   # bar interval the replay steps through
    "BACKTEST_MODE":  "intraday",   # intraday (EOD square-off) | delivery (overnight holds)
    "BACKTEST_WARMUP_DAYS": 7,      # extra calendar days fetched for warmup
    "SLIPPAGE_BPS":         2.0,    # slippage applied to entry and exit fills

    # Delivery mode (positional backtests: BACKTEST_MODE="delivery" and the "1d"
    # timeframe, which is always positional). Overnight/multi-day holds need
    # their own stop/target/risk/leverage profile — intraday's tight 5m-support
    # stop and 5x margin don't fit a swing hold. These SHADOW the plain
    # MIN_SL_OFFSET/RR_RATIO/RISK_PER_TRADE/MAX_CONCURRENT_POSITIONS/
    # DAILY_LOSS_LIMIT/INTRADAY_LEVERAGE/COND_*/GATE_* keys for the duration of
    # a positional replay only (see app.backtest.engine._delivery_overrides) —
    # calc_quantity/can_enter/conditions.py/trend_filter.py are never forked.
    "DELIVERY_MIN_SL_OFFSET":    15.0,    # wider structural stop for multi-day holds
    "DELIVERY_RR_RATIO":         2.5,     # swing trades target a bigger reward:risk
    "DELIVERY_RISK_MODE":        "fixed_amount",   # fixed_amount | capital_pct
    "DELIVERY_RISK_PER_TRADE":   500.0,   # ₹ fixed risk capital per setup
    "DELIVERY_RISK_CAPITAL_PCT": 0.02,    # 0.02 = a stop-out loses 2% of capital
    "DELIVERY_MAX_CONCURRENT_POSITIONS": 3,
    "DELIVERY_DAILY_LOSS_LIMIT": 2_000.0, # run-level loss stop (positional semantics)
    "DELIVERY_LEVERAGE":         1,       # CNC/delivery has no intraday margin by default

    # Delivery entry-condition + trend-gate toggles (independent from the live/
    # intraday ones above). No DELIVERY_COND_DEPTH — depth is live-only (no
    # order book in history) and always auto-passes in every backtest mode.
    "DELIVERY_COND_NEAR_SUPPORT":    True,
    "DELIVERY_COND_BULLISH_PATTERN": True,
    "DELIVERY_COND_ADX":             True,
    "DELIVERY_COND_RSI":             True,
    "DELIVERY_COND_MACD_CROSS":      True,
    "DELIVERY_COND_VOLUME_SURGE":    True,
    "DELIVERY_COND_ABOVE_VWAP":      True,
    "DELIVERY_GATE_STOCK_DAILY":     True,
    "DELIVERY_GATE_STOCK_HOURLY":    True,
    "DELIVERY_GATE_NIFTY_DAILY":     True,
    "DELIVERY_GATE_NIFTY_VWAP":      True,

    # Delivery (CNC) cost profile — NSE delivery is taxed very differently from
    # intraday: STT is 0.1% on BOTH legs (vs 0.025% sell-only), stamp 0.015%
    # (vs 0.003%), brokerage is usually 0 at discount brokers, and every sell
    # incurs a flat DP charge. Shadowed onto the plain COST_* keys during
    # positional replays; without this delivery P&L is overstated ~0.2%/trade.
    "DELIVERY_COST_BROKERAGE_PCT": 0.0,      # CNC brokerage (0 at discount brokers)
    "DELIVERY_COST_STT":           0.001,    # 0.1% STT — applied to BOTH legs
    "DELIVERY_COST_STAMP":         0.00015,  # 0.015% stamp duty, buy side
    "DELIVERY_COST_DP":            15.93,    # flat ₹ DP charge per sell

    # Realistic intraday-equity round-trip cost model (fractions of turnover)
    "COST_BROKERAGE_PCT": 0.0003,     # per executed order
    "COST_BROKERAGE_CAP": 20.0,       # ₹ cap per order
    "COST_STT_SELL":      0.00025,    # securities txn tax, sell side only
    "COST_STT_BUY":       0.0,        # buy-side STT — 0 intraday; delivery pays BOTH legs
    "COST_TXN_CHARGE":    0.0000297,  # NSE exchange transaction charge
    "COST_GST":           0.18,       # GST on (brokerage + txn charge)
    "COST_STAMP_BUY":     0.00003,    # stamp duty, buy side only
    "COST_SEBI":          0.000001,   # SEBI turnover fee
    "COST_DP_SELL":       0.0,        # flat ₹ DP charge per sell — 0 intraday, real for delivery
}

_runtime_overrides: Dict[str, Any] = {}
_thread_ctx = threading.local()

# Bumped on every runtime-override mutation (Settings page apply/reset) — the
# single choke point for "did a dynamic tunable change". Callers that cache
# results derived from dynamic cfg values (e.g. the indicators-viewer row
# memo) fold this into their cache key so a settings change invalidates them
# without needing its own bespoke signal.
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


# Monotonic id for each thread_overrides scope entry — never reused (unlike
# id() of the dict, which the allocator can recycle), and restored on exit so
# nested scopes (delivery replays) resolve correctly.
_ctx_token_counter = itertools.count(1)


def resolution_token() -> tuple:
    """
    Opaque key identifying the CURRENT dynamic-config resolution state for
    this thread: changes whenever a Settings-page apply/reset lands OR the
    thread enters/exits a thread_overrides scope. Hot paths that resolve many
    cfg values per call (entry_ok, trend_blockers, can_enter) cache their
    resolved plan against this token — the module __getattr__ costs ~20× a
    plain attribute read, and a long backtest performs tens of millions of
    such reads.
    """
    return (_settings_generation, getattr(_thread_ctx, "token", 0))


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
    so a run's parameters never leak into the live engine, whose scan-pool
    threads and event loop keep reading the global runtime values.
    """
    prev       = getattr(_thread_ctx, "overrides", None)
    prev_token = getattr(_thread_ctx, "token", 0)
    merged = dict(prev) if prev else {}
    merged.update(overrides)
    _thread_ctx.overrides = merged
    _thread_ctx.token     = next(_ctx_token_counter)
    try:
        yield
    finally:
        _thread_ctx.overrides = prev
        _thread_ctx.token     = prev_token
