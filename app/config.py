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
API_HOST          = "algo.vaangamart.com"
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

# Per-stock dashboard price-move alert threshold wiring (client-side only —
# see static/js/alerts.js) — same "which stock uses which settings key" shape
# as BN_QTY_THRESHOLD_ATTR above, not itself a tunable. Values are in raw
# index/stock POINTS (matching the Stock Candles table's own cell numbers
# directly), not a % — an explicit user decision, since % obscures the
# relationship to what's actually displayed on screen.
BN_PRICE_ALERT_ATTR: Dict[str, str] = {
    "HDFC BANK":            "BN_PRICE_ALERT_PTS_HDFC",
    "ICICI BANK":           "BN_PRICE_ALERT_PTS_ICICI",
    "STATE BANK OF INDIA":  "BN_PRICE_ALERT_PTS_SBI",
    "AXIS BANK":            "BN_PRICE_ALERT_PTS_AXIS",
    "KOTAK BANK":           "BN_PRICE_ALERT_PTS_KOTAK",
    "INDUSIND BANK":        "BN_PRICE_ALERT_PTS_INDUSIND",
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

# ── Static: Nifty 50 options strategy universe (parallel to the BN block
# above — a second, independent instrument, not a replacement) ──────────────
# The 32-stock stock_symbol list originally supplied for this universe used
# the OLD pre-migration Kite-style numeric tokens (e.g. "1333" for HDFC
# BANK) — the exact dead scheme BN_ALL_STOCKS already moved off of (see the
# migration note above). Values below are the corrected current NSE trading-
# symbol strings; dict KEYS (stockname text sent to the vendor) are kept as
# originally supplied, with the same Kotak fix BN_ALL_STOCKS already needed
# ("KOTAK BANK", not "KOTAK MAHINDRA BANK" — vendor matches by stockname text).
NF_INDEX_NAME = "NIFTY50"
NF_INDEX_TOKEN = "NIFTY 50"

# The 12 highest-weighted of the 32 (by real-world NSE index weight) — drive
# the leader-vote + volume-surge gates, same role BN_LEADER_STOCKS plays.
NF_LEADER_STOCKS: Dict[str, str] = {
    "HDFC BANK":                "HDFCBANK",
    "RELIANCE INDUSTRIES":      "RELIANCE",
    "ICICI BANK":               "ICICIBANK",
    "INFOSYS":                  "INFY",
    "BHARTI AIRTEL":            "BHARTIARTL",
    "ITC":                      "ITC",
    "HCL TECHNOLOGIES":         "HCLTECH",   # was TCS (any stockname/symbol variant tried) — confirmed via direct vendor query that TCS has NO data at all under this vendor, not a naming mismatch; swapped for HCL Technologies, which does
    "LARSEN & TOUBRO":          "LT",
    "KOTAK BANK":               "KOTAKBANK",   # was "KOTAK MAHINDRA BANK" — same gotcha as BN_ALL_STOCKS
    "AXIS BANK":                "AXISBANK",
    "STATE BANK OF INDIA":      "SBIN",
    "HINDUSTAN UNILEVER":       "HINDUNILVR",
}

NF_QTY_THRESHOLD_ATTR: Dict[str, str] = {
    "HDFC BANK":                "NF_QTY_THRESHOLD_HDFC",
    "RELIANCE INDUSTRIES":      "NF_QTY_THRESHOLD_RELIANCE",
    "ICICI BANK":               "NF_QTY_THRESHOLD_ICICI",
    "INFOSYS":                  "NF_QTY_THRESHOLD_INFY",
    "BHARTI AIRTEL":            "NF_QTY_THRESHOLD_BHARTIARTL",
    "ITC":                      "NF_QTY_THRESHOLD_ITC",
    "HCL TECHNOLOGIES":         "NF_QTY_THRESHOLD_HCLTECH",
    "LARSEN & TOUBRO":          "NF_QTY_THRESHOLD_LT",
    "KOTAK BANK":               "NF_QTY_THRESHOLD_KOTAK",
    "AXIS BANK":                "NF_QTY_THRESHOLD_AXIS",
    "STATE BANK OF INDIA":      "NF_QTY_THRESHOLD_SBI",
    "HINDUSTAN UNILEVER":       "NF_QTY_THRESHOLD_HUL",
}

# NF mirror of BN_PRICE_ALERT_ATTR above — also raw points, not %.
NF_PRICE_ALERT_ATTR: Dict[str, str] = {
    "HDFC BANK":                "NF_PRICE_ALERT_PTS_HDFC",
    "RELIANCE INDUSTRIES":      "NF_PRICE_ALERT_PTS_RELIANCE",
    "ICICI BANK":               "NF_PRICE_ALERT_PTS_ICICI",
    "INFOSYS":                  "NF_PRICE_ALERT_PTS_INFY",
    "BHARTI AIRTEL":            "NF_PRICE_ALERT_PTS_BHARTIARTL",
    "ITC":                      "NF_PRICE_ALERT_PTS_ITC",
    "HCL TECHNOLOGIES":         "NF_PRICE_ALERT_PTS_HCLTECH",
    "LARSEN & TOUBRO":          "NF_PRICE_ALERT_PTS_LT",
    "KOTAK BANK":               "NF_PRICE_ALERT_PTS_KOTAK",
    "AXIS BANK":                "NF_PRICE_ALERT_PTS_AXIS",
    "STATE BANK OF INDIA":      "NF_PRICE_ALERT_PTS_SBI",
    "HINDUSTAN UNILEVER":       "NF_PRICE_ALERT_PTS_HUL",
}

# All 32 stocks fetched/displayed — the 20 beyond the leaders never feed the
# entry decision but are kept for parity with the BN universe's own
# "leaders + extras" shape.
NF_ALL_STOCKS: Dict[str, str] = {
    **NF_LEADER_STOCKS,
    "BAJAJ FINANCE":            "BAJFINANCE",
    "ASIAN PAINTS":             "ASIANPAINT",
    "TITAN":                    "TITAN",   # was "TITAN COMPANY" — vendor returned zero candles for that stockname; "TITAN" itself matches (same gotcha class as Kotak/HCL Tech above)
    "WIPRO":                    "WIPRO",
    "NTPC":                     "NTPC",
    "ULTRATECH CEMENT":         "ULTRACEMCO",
    "JSW STEEL":                "JSWSTEEL",
    "TATA MOTORS":              "TATAMOTORS",
    "TECH MAHINDRA":            "TECHM",
    "BAJAJ AUTO":               "BAJAJ-AUTO",
    "INDUSIND BANK":            "INDUSINDBK",
    "AU SMALL FINANCE BANK":    "AUBANK",
    "FEDERAL BANK":             "FEDERALBNK",
    "IDFC FIRST BANK":          "IDFCFIRSTB",
    "PUNJAB NATIONAL BANK":     "PNB",
    "CANARA BANK":              "CANBK",
    # Replacements for MARUTI SUZUKI INDIA / SUN PHARMACEUTICAL IND L /
    # POWER GRID CORP. — all three confirmed to have ZERO vendor data under
    # every stockname/symbol variant tried (not a naming mismatch, a genuine
    # coverage gap). These 4 were confirmed working via direct vendor query.
    "MAHINDRA & MAHINDRA":      "M&M",
    "TATA STEEL":               "TATASTEEL",
    "SBI LIFE INSURANCE":       "SBILIFE",
    "HDFC LIFE INSURANCE":      "HDFCLIFE",
}

# Equal weight across all 32 (100/32) — no real per-stock Nifty 50 weights
# were supplied (unlike BN_INDEX_WEIGHTS' real Oct-2025 snapshot), per an
# explicit user decision. Keyed by stock_symbol, same convention as
# BN_INDEX_WEIGHTS. Used only for the synthetic-index fallback.
NF_INDEX_WEIGHTS: Dict[str, float] = {
    token: 100.0 / len(NF_ALL_STOCKS) for token in NF_ALL_STOCKS.values()
}

# Nifty 50 exchange lot size — a contract-spec fact, not a user tunable.
NF_LOT_SIZE = 65

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

    # ── NF (Nifty 50) Strategy — parallel to the BN block above. Point-based
    # thresholds (sideways/momentum/target/stop/breakeven/trail) start scaled
    # down ~0.45x from BN's own calibrated values, matching Nifty 50 trading
    # at roughly 0.45x BankNifty's spot level (~25,000 vs ~55,000) — a
    # starting point only, same "recalibrate from live bars" caveat as BN's
    # own qty thresholds. Dimensionless gates (RSI/EMA/MACD periods, score
    # levels) reuse BN's exact defaults — those don't scale with spot price.
    "NF_SIDEWAYS_RANGE_MIN":   6.0,    # min 5-bar Nifty 50 close range to trade
    "NF_MOMENTUM_THRESHOLD":   13.0,   # fixed 5m momentum threshold (points)
    "NF_ATR_PERIOD":           10,
    "NF_SAME_DIRECTION_REQUIRED": 6,   # of 12 leaders must agree
    "NF_ENTRY_COOLDOWN_S":     60,

    # NF Strategy — per-stock volume-surge thresholds. PLACEHOLDER values —
    # no live volume data yet for these stocks on this feed; calibrate the
    # same way BN's own thresholds were (see BN_QTY_THRESHOLD_* comment).
    "NF_QTY_THRESHOLD_HDFC":       55_000.0,
    "NF_QTY_THRESHOLD_RELIANCE":   40_000.0,
    "NF_QTY_THRESHOLD_ICICI":      48_000.0,
    "NF_QTY_THRESHOLD_INFY":       35_000.0,
    "NF_QTY_THRESHOLD_BHARTIARTL": 30_000.0,
    "NF_QTY_THRESHOLD_ITC":        30_000.0,
    "NF_QTY_THRESHOLD_HCLTECH":    20_000.0,
    "NF_QTY_THRESHOLD_LT":         15_000.0,
    "NF_QTY_THRESHOLD_KOTAK":      43_000.0,
    "NF_QTY_THRESHOLD_AXIS":       28_000.0,
    "NF_QTY_THRESHOLD_SBI":        18_000.0,
    "NF_QTY_THRESHOLD_HUL":        15_000.0,
    "NF_QTY_INTERVAL_MULTIPLIER": 1.0,

    # NF Strategy — composite indicator gate (same dimensionless defaults as BN)
    "NF_INDICATOR_LOOKBACK_BARS": 200,
    "NF_RSI_PERIOD":       14,
    "NF_EMA_FAST":         20,
    "NF_EMA_SLOW":         50,
    "NF_MACD_FAST":        12,
    "NF_MACD_SLOW":        26,
    "NF_RSI_BULL_LEVEL":   58,
    "NF_RSI_BEAR_LEVEL":   42,
    "NF_RSI_OVERBOUGHT":   72,
    "NF_RSI_OVERSOLD":     28,
    "NF_EMA_EXTENSION_PCT": 1.2,
    "NF_SCORE_MIN":        2.0,
    "NF_SCORE_MARGIN":     0.9,

    # NF Risk — target/stop/trailing on the underlying Nifty 50 index (points)
    "NF_TARGET_POINTS":     16.0,
    "NF_STOPLOSS_POINTS":   8.0,
    "NF_BREAKEVEN_TRIGGER": 5.5,
    "NF_TRAIL_TRIGGER":     8.0,
    "NF_TRAIL_DISTANCE":    5.5,
    # No NF_STARTING_FUNDS — BN and NF share one paper account balance
    # (st.funds), seeded once from BN_STARTING_FUNDS; see scheduler._load_funds.

    # NF Options Pricing — synthetic Black-Scholes premium, no real option data
    "NF_RISK_FREE_RATE": 0.065,
    "NF_IV_MIN":         0.20,
    "NF_IV_MAX":         0.70,
    "NF_IV_DEFAULT":     0.28,
    "NF_IV_LOOKBACK_BARS": 50,
    "NF_IV_MANUAL_ENABLED": False,
    "NF_IV_MANUAL_VALUE":   0.30,

    # NF Options Costs — same placeholder rates as BN (confirm current India
    # options STT/exchange-txn figures before trusting absolute ₹ P&L).
    "NF_COST_BROKERAGE_FLAT": 20.0,
    "NF_COST_STT_SELL_PCT":   0.001,
    "NF_COST_TXN_PCT":        0.0005,
    "NF_COST_GST_PCT":        0.18,
    "NF_COST_SEBI_PCT":       0.000001,

    # ── Dashboard price-move alerts (browser Notification API, client-side
    # only — not read anywhere in the trading engine) — per-leader-stock,
    # fires when THAT stock's latest bar |close-open| move exceeds its own
    # threshold, in raw POINTS (see BN_PRICE_ALERT_ATTR/NF_PRICE_ALERT_ATTR
    # above — matches the Stock Candles table's own cell numbers directly,
    # an explicit user decision over %, which obscures that relationship).
    # Defaults are ballparked per stock's own price level (roughly what a
    # 0.5% move would have been), not one flat number — a flat points value
    # would be trivially crossed on an expensive stock (e.g. LT ~4000) and
    # nearly unreachable on a cheap one (e.g. KOTAK ~400). Recalibrate from
    # observed live bars, same caveat as the qty-surge thresholds.
    "BN_PRICE_ALERT_PTS_HDFC":     3.5,   # ~725
    "BN_PRICE_ALERT_PTS_ICICI":    7.0,   # ~1413
    "BN_PRICE_ALERT_PTS_SBI":      5.0,   # ~1035
    "BN_PRICE_ALERT_PTS_AXIS":     6.0,   # ~1237
    "BN_PRICE_ALERT_PTS_KOTAK":    2.0,   # ~398
    "BN_PRICE_ALERT_PTS_INDUSIND": 5.0,   # ~1015

    "NF_PRICE_ALERT_PTS_HDFC":       3.5,   # ~725
    "NF_PRICE_ALERT_PTS_RELIANCE":   6.5,   # ~1300
    "NF_PRICE_ALERT_PTS_ICICI":      7.0,   # ~1413
    "NF_PRICE_ALERT_PTS_INFY":       5.5,   # ~1130
    "NF_PRICE_ALERT_PTS_BHARTIARTL": 9.5,   # ~1945
    "NF_PRICE_ALERT_PTS_ITC":        1.5,   # ~270
    "NF_PRICE_ALERT_PTS_HCLTECH":    8.0,   # ~1600
    "NF_PRICE_ALERT_PTS_LT":         20.0,  # ~4077
    "NF_PRICE_ALERT_PTS_KOTAK":      2.0,   # ~398
    "NF_PRICE_ALERT_PTS_AXIS":       6.0,   # ~1237
    "NF_PRICE_ALERT_PTS_SBI":        5.0,   # ~1035
    "NF_PRICE_ALERT_PTS_HUL":        10.0,  # ~2043

    # "Leader consensus" alert — fires when at least this many leaders have
    # BOTH crossed their own BN_PRICE_ALERT_PTS_*/NF_PRICE_ALERT_PTS_*
    # threshold AND are moving the same direction (all up or all down) on
    # the same tick. Separate from (and in addition to) the per-stock alert
    # above. Client-side only, see static/js/alerts.js.
    "BN_ALERT_CONSENSUS_REQUIRED": 4,   # of 6 leaders
    "NF_ALERT_CONSENSUS_REQUIRED": 8,   # of 12 leaders
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
