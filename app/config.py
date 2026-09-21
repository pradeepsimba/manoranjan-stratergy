from __future__ import annotations

"""
Configuration — static system settings plus a small DYNAMIC tunables layer.

Static values (endpoints, credentials, structural pool/buffer sizes, the
Bank Nifty instrument universe, and — as of 2026-09-09, explicit user
decision — every strategy/risk/pricing/cost/session-timing parameter for
both BN and NF) are plain module attributes and require a restart to change.

Only the BN/NF Alerts settings (per-stock move-alert point thresholds +
consensus-required counts, purely client-side notification tuning — never
read by the trading engine) remain dynamic, living in _DEFAULTS and resolved
through the module-level __getattr__ (PEP 562) with this precedence:

    1. thread-local overrides  — active only inside backtest worker threads;
                                 moot now since no bt=True tunable remains
    2. runtime overrides       — dashboard Settings page, persisted in the
                                 app_settings table and applied at startup
    3. the hard default below

`import app.config as cfg; cfg.BN_ALERT_CONSENSUS_REQUIRED` therefore always
returns the CURRENT value. Code must read a dynamic cfg attribute at call
time — never copy it into a module-level constant or default-argument value,
or it freezes at import and stops being dynamic. A STATIC attribute (the vast
majority now) has no such restriction — it's just a plain Python value.

The editable registry (labels, types, bounds, grouping) for the remaining
dynamic tunables lives in app/services/settings.py — add new ones in BOTH
places.
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
    "postgresql://postgres:postgres@localhost/mano_trading_db",
)

# Whole-app login (see app/auth.py) - single shared credential, gates every
# page/API route except /ws/dashboard and /login itself. Same credential the
# Settings page used to gate on its own before this covered the whole app.
SETTINGS_USER     = os.getenv("SETTINGS_USER", "admin")
SETTINGS_PASSWORD = os.getenv("SETTINGS_PASSWORD", "")

# Signs the login session cookie (itsdangerous) - a random signing key, not a
# password; generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

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
# BankNifty index previously returned ZERO data (live or historical) with
# BN_INDEX_TOKEN="NIFTY BANK" (an unconfirmed guess made at migration time,
# not sourced from the vendor's own instrument list) and the pre-migration
# numeric "26009". A user-supplied instrument reference (exchange_token=
# 26009, trading_symbol="BANKNIFTY", instrumental_token=26009) shows the
# correct stock_symbol is the plain trading symbol "BANKNIFTY" — matching
# the same trading-symbol-string convention BN_ALL_STOCKS already uses post-
# migration, not "NIFTY BANK". A second, independent instrument-master export
# (2026-09-17) confirms the same row again (exchange_token 26009,
# trading_symbol "BANKNIFTY", instrument_type INDEX). Retrying with this
# corrected value; if the vendor still returns nothing, app/services/
# market_data.py's synthetic index (from the BN stocks' BN_INDEX_WEIGHTS-
# weighted % change — see MarketDataService._update_synthetic_index) remains
# the fallback either way.
BN_INDEX_NAME = "BANKNIFTY"
BN_INDEX_TOKEN = "BANKNIFTY"   # was "NIFTY BANK", before that "26009" — both unconfirmed guesses

# Options-underlying symbol prefix used to build a real option instrument
# symbol (e.g. "BANKNIFTY26SEP56400CE" — monthly, no day; see app/engine/
# bn_pricing.build_monthly_option_symbol) for the paper-trading engine's
# real-LTP feature (2026-09-17, explicit user decision). Same as
# BN_INDEX_TOKEN for BankNifty, but kept as its own constant since NF's
# equivalent (NF_OPTION_UNDERLYING, below) deliberately differs from
# NF_INDEX_TOKEN.
BN_OPTION_UNDERLYING = BN_INDEX_TOKEN

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

# The real 14-member NIFTY BANK index universe (2026-09-16 addition of Bank
# of Baroda/Union Bank of India/Yes Bank + 2026-09-17 correction, both
# below; 2026-09-17 removal of the 11 non-index "extras" this dict briefly
# also carried, per explicit user decision to track only the real index
# members). BN_LEADER_STOCKS' 6 drive the entry decision; the other 8 below
# are tracked for display/weighted-signal purposes only.
BN_ALL_STOCKS: Dict[str, str] = {
    **BN_LEADER_STOCKS,
    "AU SMALL FINANCE BANK": "AUBANK",      # was "21238"
    "FEDERAL BANK":          "FEDERALBNK",  # was "1023"
    "IDFC FIRST BANK":       "IDFCFIRSTB",  # was "11184"
    "PUNJAB NATIONAL BANK":  "PNB",         # was "10666"
    "CANARA BANK":           "CANBK",       # was "10794"
    # Completes the real NIFTY BANK index (2026-09-16 + correction 2026-09-17)
    # — previously left out as BN_UNTRACKED_WEIGHTS-only (no confirmed vendor
    # symbol). Now wired as a real instrument, and the "BANKBARODA" guess is
    # confirmed correct (2026-09-17) against a user-supplied vendor
    # instrument-master export (exchange_token/trading_symbol/
    # instrumental_token/instrument_type columns) — exact match: trading_
    # symbol "BANKBARODA".
    "BANK OF BARODA":        "BANKBARODA",
    # These 2 complete the real NIFTY BANK index's roster of 14 members
    # (2026-09-17 correction — the official NSE factsheet, fetched live, says
    # "No. of Constituents: 14", and a full constituent listing — later also
    # independently confirmed by a user-supplied NSE screenshot listing all
    # 14 symbols, exact match — confirmed the other 12 already tracked above
    # plus exactly these two; earlier that same day I had wrongly assumed 12
    # was the complete index and briefly filed both of these under a "non-
    # index extras" block instead). Real members, but — like PNB/CANBK above
    # — with no confirmed individual weight number, so they sit on the
    # equal-weight placeholder rather than in BN_INDEX_WEIGHTS_CONFIRMED
    # below.
    "UNION BANK OF INDIA":   "UNIONBANK",
    "YES BANK":              "YESBANK",
}

# Nifty Bank per-stock weight, % — keyed by the same trading-symbol strings
# as BN_ALL_STOCKS' values (this repo's candles_5m is likewise stock_symbol-
# keyed — see CLAUDE.md's "candles_5m is keyed by TOKEN" convention, now
# token = trading symbol). Used for the weighted global-signal/contribution-
# analysis port (app/engine/bn_breakout.py) and the synthetic BankNifty
# index candle (app/services/market_data.py). Equal weight across all
# BN_ALL_STOCKS is the base — the fallback for any stock not covered by the
# real-weight overlay below (mirrors NF_INDEX_WEIGHTS' own pattern).
BN_INDEX_WEIGHTS: Dict[str, float] = {
    token: 100.0 / len(BN_ALL_STOCKS) for token in BN_ALL_STOCKS.values()
}
# Real weights for 10 of the 14 actual NIFTY BANK index members — 9 from a
# 2026-09-07 user-supplied snapshot (HDFCBANK/ICICIBANK/SBIN/KOTAKBANK/
# AXISBANK/INDUSINDBK/AUBANK/IDFCFIRSTB/FEDERALBNK, replacing the older
# "Oct 30, 2025" c.html figures), and BANKBARODA folded in from the old
# BN_UNTRACKED_WEIGHTS side-channel now that it's a real tracked instrument
# (2026-09-16, see BN_ALL_STOCKS above). Independently re-confirmed
# 2026-09-17 against the live NSE factsheet (archives.nseindia.com/content/
# indices/ind_nifty_bank.pdf, dated August 31, 2026) — its "Top constituents
# by weightage" table lists these exact same 10 names at these exact same
# weight values. The remaining 4 real members — PNB/CANBK (unchanged at an
# older snapshot's values, not in the 2026-09-07 one) and Union Bank of
# India/Yes Bank (no weight ever supplied — these two were even miscategorized
# as non-index "extras" until the 2026-09-17 correction above) — have no
# current weight number and are NOT part of the rescale below.
#
# The 10-stock snapshot below sums to 86.81, not 100 — the snapshot just
# didn't cover the full index. Rescaled proportionally (preserving each
# stock's relative size) so the "Weightage (confirmed stocks)" badge's
# denominator reads a clean 100 instead of that partial-coverage number
# (2026-09-17, same fix already applied to NF's equivalent badge) — PNB/
# CANBK/UNIONBANK/YESBANK are NOT part of this rescale, since they're
# excluded from BN_INDEX_WEIGHTS_CONFIRMED below and don't feed that badge.
_BN_REAL_WEIGHTS_CONFIRMED_RAW = {
    "HDFCBANK":   17.02,   # HDFC BANK
    "ICICIBANK":  14.86,   # ICICI BANK
    "SBIN":       10.27,   # STATE BANK OF INDIA
    "KOTAKBANK":  9.88,    # KOTAK BANK
    "AXISBANK":   9.20,    # AXIS BANK
    "FEDERALBNK": 7.15,    # FEDERAL BANK
    "INDUSINDBK": 5.45,    # INDUSIND BANK
    "AUBANK":     4.82,    # AU SMALL FINANCE BANK
    "IDFCFIRSTB": 4.68,    # IDFC FIRST BANK
    "BANKBARODA": 3.48,    # BANK OF BARODA
}
_bn_confirmed_scale = 100.0 / sum(_BN_REAL_WEIGHTS_CONFIRMED_RAW.values())
_BN_REAL_WEIGHTS = {
    tok: round(w * _bn_confirmed_scale, 2) for tok, w in _BN_REAL_WEIGHTS_CONFIRMED_RAW.items()
}
_BN_REAL_WEIGHTS.update({
    "PNB":   2.86,    # PUNJAB NATIONAL BANK — unchanged, not in the new snapshot, not rescaled (excluded from BN_INDEX_WEIGHTS_CONFIRMED)
    "CANBK": 2.40,    # CANARA BANK — unchanged, not in the new snapshot, not rescaled (excluded from BN_INDEX_WEIGHTS_CONFIRMED)
})
BN_INDEX_WEIGHTS.update(_BN_REAL_WEIGHTS)

# The 10 index members with a real, user-verified weight — the weighted
# red/green split's TOTAL is computed over this set only (see
# app.engine.bn_breakout.compute_weighted_red_green), and sums to exactly 100
# thanks to the rescale above. The other 4 real members (PNB/CANBK — the user
# did not re-supply weights for those two — and Union Bank of India/Yes Bank
# — no weight ever supplied) are deliberately excluded, and so are all 11
# non-index "extras" above, which have no real weight at all, only the
# equal-weight placeholder.
BN_INDEX_WEIGHTS_CONFIRMED = {
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
    "FEDERALBNK", "INDUSINDBK", "AUBANK", "IDFCFIRSTB", "BANKBARODA",
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
#
# NF_INDEX_NAME was "NIFTY50" (no space) — exactly the same class of bug as
# the Kotak fix above: a user-supplied instrument reference (name/
# trading_symbol="NIFTY 50", WITH a space) shows the vendor's exact stockname
# text needs the space; the previous no-space value would have silently
# returned zero data the same way "Kotak Mahindra Bank" did for Kotak Bank.
# A second, independent instrument-master export (2026-09-17) confirms the
# same row again (exchange_token 99926000, trading_symbol "NIFTY 50" with the
# space, instrument_type INDEX).
NF_INDEX_NAME = "NIFTY 50"
NF_INDEX_TOKEN = "NIFTY 50"

# Options-underlying symbol prefix (2026-09-17, see BN_OPTION_UNDERLYING
# above) — deliberately "NIFTY", NOT NF_INDEX_TOKEN's "NIFTY 50": real NSE
# Nifty 50 index options trade under the plain "NIFTY" underlying symbol,
# unlike the index/stock candle data feed's "NIFTY 50" (with the space).
# Confirmed correct 2026-09-19 against a real user-supplied example symbol
# ("NIFTY2692223250PE" — see app/engine/nf_pricing.build_weekly_option_symbol)
# — but this vendor's feed still doesn't stream any option data at all under
# that (or any other) symbol, confirmed via direct WS/REST probing the same
# day; see CLAUDE.md's "Real-option-LTP paper trading" note.
NF_OPTION_UNDERLYING = "NIFTY"

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

# All 47 stocks fetched/displayed — the 35 beyond the leaders never feed the
# entry decision but are kept for parity with the BN universe's own
# "leaders + extras" shape.
NF_ALL_STOCKS: Dict[str, str] = {
    **NF_LEADER_STOCKS,
    # Re-added 2026-09-21 for the Top-8 weighted-basket scalp strategy (see
    # "Static: Scalping strategy" below) — the user's own basket spec names
    # TCS explicitly. Previously swapped OUT for HCL Technologies (2026-07
    # era comment on NF_LEADER_STOCKS above) after a direct vendor query
    # found zero data under any stockname/symbol variant at the time; if
    # that's still true, TCS's VWAP/score will just sit at None/0 weight
    # contribution — the same "missing candle -> skipped" tolerance every
    # other stock in this universe already has, not a crash.
    "TATA CONSULTANCY SERVICES": "TCS",
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
    # Remaining Nifty 50 index constituents not previously tracked (2026-09-16,
    # explicit user decision to add the "rest of Nifty 50" — originally filled
    # in from general knowledge, not vendor-confirmed). As of 2026-09-17, the
    # surviving 15 symbols were cross-checked against a user-supplied vendor
    # instrument-master export (exchange_token/trading_symbol/
    # instrumental_token/instrument_type columns) and are an exact match — no
    # longer a guess for the symbol, though live/historical data flow through
    # this specific vendor feed is still unverified beyond that (the master
    # list only proves the identifier is valid, not that this vendor actually
    # streams it — same caveat as BANKNIFTY/NIFTY 50's own tokens).
    #
    # LTIMINDTREE / NESTLE INDIA / OIL & NATURAL GAS CORP were tried and
    # REMOVED (2026-09-17) — confirmed via direct vendor query (a live POST to
    # the historical-data endpoint, with a known-good control stock in the
    # same request batch to rule out a broader outage) that all three return
    # zero candles under every stockname/symbol variant tried, over a 5-day
    # window — a genuine coverage gap, not a naming mismatch (LTIMINDTREE
    # wasn't even in the instrument-master export to begin with; NESTLEIND
    # and ONGC were, so being listed there is no guarantee of real data). Same
    # class as the confirmed-dead Maruti/Sun Pharma/Power Grid/TCS entries
    # below — don't re-add any of these six without a fresh direct vendor
    # query confirming they now return real bars.
    "ADANI ENTERPRISES":        "ADANIENT",
    "ADANI PORTS & SEZ":        "ADANIPORTS",
    "APOLLO HOSPITALS":         "APOLLOHOSP",
    "BAJAJ FINSERV":            "BAJAJFINSV",
    "BHARAT ELECTRONICS":       "BEL",
    "CIPLA":                    "CIPLA",
    "COAL INDIA":               "COALINDIA",
    "DR REDDYS LABORATORIES":   "DRREDDY",
    "EICHER MOTORS":            "EICHERMOT",
    "GRASIM INDUSTRIES":        "GRASIM",
    "HERO MOTOCORP":            "HEROMOTOCO",   # NOT in the official current Nifty 50 list the user supplied 2026-09-17 (dropped in a reconstitution) — kept for now, unconfirmed whether to remove; see config.py's NF_ALL_STOCKS module comment
    "HINDALCO INDUSTRIES":      "HINDALCO",
    "SHRIRAM FINANCE":          "SHRIRAMFIN",
    "TATA CONSUMER PRODUCTS":   "TATACONSUM",
    "TRENT":                    "TRENT",
    # Added 2026-09-17 against a user-supplied official Nifty 50 constituent
    # list (50 rows: name/sector/symbol/series/ISIN) that revealed several
    # index members this dict was missing entirely. Direct vendor query
    # confirmed real data (218 bars, same as a healthy control) for these 3:
    "INTERGLOBE AVIATION":      "INDIGO",
    "JIO FINANCIAL SERVICES":   "JIOFIN",
    "MAX HEALTHCARE INSTITUTE": "MAXHEALTH",
    # The same list also named 5 more real members not yet added here:
    # MARUTI/NESTLEIND/ONGC/POWERGRID/SUNPHARMA/TCS are the already-
    # confirmed-dead six noted above (still correctly excluded); "Eternal
    # Ltd." (the current name for the former Zomato) — vendor query timed
    # out before a working stockname/symbol variant could be confirmed, so
    # it's deliberately left out rather than guessed; and "Tata Motors
    # Passenger Vehicles Ltd." (symbol TMPV) — the post-demerger index
    # constituent — returned zero data under that name/symbol, while the
    # pre-demerger combined "TATA MOTORS"/"TATAMOTORS" entry below still
    # returns real, current data, so that's deliberately kept as-is rather
    # than swapped to the technically-correct-but-vendor-dead TMPV symbol.
}

# Equal weight across all 50 (100/50) as the base — the fallback for any
# stock not covered by the overlay below. Keyed by stock_symbol, same
# convention as BN_INDEX_WEIGHTS. Used for the weighted global-signal/
# contribution-analysis port (app/engine/bn_breakout.py) and the synthetic
# Nifty 50 index candle (app/services/market_data.py).
NF_INDEX_WEIGHTS: Dict[str, float] = {
    token: 100.0 / len(NF_ALL_STOCKS) for token in NF_ALL_STOCKS.values()
}
# Overlay real/approximate Nifty 50 index weights so the "Weightage
# (confirmed stocks)" badge covers as much of the universe as possible
# instead of falling back to the flat equal-weight placeholder above, and
# sums to exactly 100 so the badge's denominator reads as a clean "% of the
# real index" total rather than a partial-coverage number.
#
# Real, user-supplied weights (2026-09-07 snapshot) — kept exactly as given,
# never rescaled.
_NF_REAL_WEIGHTS = {
    "HDFCBANK":   9.97,    # HDFC BANK
    "ICICIBANK":  9.32,    # ICICI BANK
    "RELIANCE":   8.17,    # RELIANCE INDUSTRIES
    "BHARTIARTL": 5.12,    # BHARTI AIRTEL
    "LT":         4.24,    # LARSEN & TOUBRO
    "SBIN":       3.84,    # STATE BANK OF INDIA
    "INFY":       3.62,    # INFOSYS
    "AXISBANK":   3.34,    # AXIS BANK
    "KOTAKBANK":  2.86,    # KOTAK BANK
    "BAJFINANCE": 2.60,    # BAJAJ FINANCE
}
# Best-known approximate weights for the rest of the real Nifty 50
# constituents (2026-09-16, explicit user decision — same "fill in from
# general knowledge" risk accepted as the NF_ALL_STOCKS expansion above). NOT
# sourced from an official NSE factsheet — the RELATIVE size of each number
# is the meaningful part; rescaled below (proportionally, preserving those
# relative sizes) to fill exactly the 100 - sum(_NF_REAL_WEIGHTS) budget left
# after the real block above. If the displayed split ever looks obviously
# wrong for one of these, suspect this number first.
_NF_APPROX_WEIGHTS_RAW = {
    "TCS":        3.50,    # TATA CONSULTANCY SERVICES — re-added 2026-09-21, see NF_ALL_STOCKS above;
                           # approximate like every other value in this block (not an NSE factsheet figure)
    "ITC":        3.90,    # ITC
    "HCLTECH":    1.35,    # HCL TECHNOLOGIES
    "HINDUNILVR": 2.10,    # HINDUSTAN UNILEVER
    "ASIANPAINT": 1.05,    # ASIAN PAINTS
    "TITAN":      1.15,    # TITAN
    "WIPRO":      0.85,    # WIPRO
    "NTPC":       1.30,    # NTPC
    "ULTRACEMCO": 1.10,    # ULTRATECH CEMENT
    "JSWSTEEL":   0.95,    # JSW STEEL
    "TATAMOTORS": 1.45,    # TATA MOTORS
    "TECHM":      0.75,    # TECH MAHINDRA
    "BAJAJ-AUTO": 0.95,    # BAJAJ AUTO
    "INDUSINDBK": 0.70,    # INDUSIND BANK
    "M&M":        1.80,    # MAHINDRA & MAHINDRA
    "TATASTEEL":  0.90,    # TATA STEEL
    "SBILIFE":    0.90,    # SBI LIFE INSURANCE
    "HDFCLIFE":   1.10,    # HDFC LIFE INSURANCE
    "ADANIENT":   0.80,    # ADANI ENTERPRISES
    "ADANIPORTS": 0.85,    # ADANI PORTS & SEZ
    "APOLLOHOSP": 0.55,    # APOLLO HOSPITALS
    "BAJAJFINSV": 0.95,    # BAJAJ FINSERV
    "BEL":        0.65,    # BHARAT ELECTRONICS
    "CIPLA":      0.60,    # CIPLA
    "COALINDIA":  0.80,    # COAL INDIA
    "DRREDDY":    0.60,    # DR REDDYS LABORATORIES
    "EICHERMOT":  0.55,    # EICHER MOTORS
    "GRASIM":     0.55,    # GRASIM INDUSTRIES
    "HEROMOTOCO": 0.45,    # HERO MOTOCORP
    "HINDALCO":   0.65,    # HINDALCO INDUSTRIES
    "SHRIRAMFIN": 0.55,    # SHRIRAM FINANCE
    "TATACONSUM": 0.55,    # TATA CONSUMER PRODUCTS
    "TRENT":      0.65,    # TRENT
    "INDIGO":     0.75,    # INTERGLOBE AVIATION — added 2026-09-17
    "JIOFIN":     0.50,    # JIO FINANCIAL SERVICES — added 2026-09-17
    "MAXHEALTH":  0.45,    # MAX HEALTHCARE INSTITUTE — added 2026-09-17
}
# Deliberately NOT included above: AU SMALL FINANCE BANK, FEDERAL BANK, IDFC
# FIRST BANK, PUNJAB NATIONAL BANK, CANARA BANK — these are BN's own "extras"
# (see NF_ALL_STOCKS comment above), not real Nifty 50 constituents, so they
# have no genuine index weight to assign. They stay on the equal-weight
# placeholder and are excluded from the "confirmed" badge/set below.
# (LTIMindtree/Nestle India/ONGC needed no such placeholder-exclusion note —
# they were removed from NF_ALL_STOCKS entirely, see above, so there's no
# symbol left for a weight to attach to either way.)
_nf_approx_budget = 100.0 - sum(_NF_REAL_WEIGHTS.values())
_nf_approx_scale  = _nf_approx_budget / sum(_NF_APPROX_WEIGHTS_RAW.values())
_NF_INDEX_WEIGHTS_CONFIRMED_VALUES = {
    **_NF_REAL_WEIGHTS,
    **{tok: round(w * _nf_approx_scale, 2) for tok, w in _NF_APPROX_WEIGHTS_RAW.items()},
}
NF_INDEX_WEIGHTS.update(_NF_INDEX_WEIGHTS_CONFIRMED_VALUES)

# NF mirror of BN_INDEX_WEIGHTS_CONFIRMED above — the NF_INDEX_WEIGHTS keys
# with a real-or-approximated weight (see the two blocks above for which is
# which), as opposed to the 5 non-constituent extras still on the
# equal-weight placeholder. Derived from the overlay dict itself so the two
# can never drift apart.
NF_INDEX_WEIGHTS_CONFIRMED = set(_NF_INDEX_WEIGHTS_CONFIRMED_VALUES.keys())

# Nifty 50 exchange lot size — a contract-spec fact, not a user tunable.
NF_LOT_SIZE = 65

# ── Static: structural sizes (pools/buffers built once — restart to change) ──
HIST_BATCH_SIZE   = 100   # max stocks per single historical API request
MAX_CANDLE_BUFFER = 300   # per-symbol in-memory candle buffer (deque maxlen)

# The vendor's documented output-buffer limit is ~40 symbol-interval pairs per
# WS connection (see market_data.py). Adding the rest of the Nifty 50
# universe (2026-09-16) pushed the combined BN+NF filter count past that cap,
# so MarketDataService now splits filters into chunks of this size, one
# connection per chunk, instead of a single connection. Kept a few below the
# documented ~40 as a safety margin.
WS_MAX_FILTERS_PER_CONN = 35

# Backtest v1 is intraday/5m only — nothing in c.html holds an option position
# across days, so positional (delivery / 1d) replay is not built.
BACKTEST_TIMEFRAMES = ["5m"]
BACKTEST_MODES      = ["intraday"]
SCAN_WORKERS        = min(8, max(4, os.cpu_count() or 4))   # per-day backtest parallelism (ThreadPoolExecutor)

# Moved out of the dynamic Settings-page tunables (2026-09-09, explicit user
# decision) — the Backtest UI panel is gone from the dashboard, so per-run
# tuning from Settings no longer has a use; the backtest engine/API/DB history
# all still work exactly as before, just with these fixed instead of editable.
BACKTEST_WARMUP_DAYS = 7     # days of pre-range history loaded so indicators have converged by from_date
SLIPPAGE_BPS         = 2.0   # applied to the option premium fill

# ── Static: session timings (IST) — SCAN_START/CUTOFF reproduce c.html's
# real 09:30-15:00 trading window using the existing phase-driver machinery.
# Moved out of the dynamic Settings-page tunables 2026-09-09 (explicit user
# decision, "remove all except threshold") — restart-only to change now.
PREMARKET_HOUR,   PREMARKET_MIN   = 9,  0
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9,  15   # historical load + WS subscribe
SCAN_START_HOUR,  SCAN_START_MIN  = 9,  30   # entries allowed from here
CUTOFF_HOUR,      CUTOFF_MIN      = 15, 0    # no new entries after this
SESSION_END_HOUR, SESSION_END_MIN = 15, 30   # terminate session

# ── Static: BN Strategy.
#
# BN_SAME_DIRECTION_REQUIRED is the ONLY survivor of the old (2026-09-19)
# leader-vote rule and the (2026-07-era) sideways-range/momentum/volume-
# surge/composite-indicator gate sequence before it — both fully REMOVED
# 2026-09-21 alongside app/engine/bn_signals.py/nf_signals.py (which
# implemented them) once the Top-8 weighted-basket scalp strategy replaced
# bn_entry_exit.evaluate_entry entirely (see "Static: Scalping strategy"
# below). It survives ONLY because app/backtest/signal_study.py — a
# standalone historical-analysis tool, entirely separate from the live/
# backtest trading engine, never called by evaluate_entry/evaluate_exit —
# still reads it for its own "what would the old leader-vote rule have
# done" study. If signal_study.py is ever removed too, this can go with it.
BN_SAME_DIRECTION_REQUIRED  = 9     # of 14 real NIFTY BANK stocks must agree — signal_study.py only, see above

# BN Strategy — per-stock volume-surge thresholds, compared against each
# leader's latest 5m bar volume (see BN_QTY_THRESHOLD_ATTR above and
# bn_entry_exit._leader_qty_surge) — an unrelated, purely informational
# "surged" annotation on the stockCandles payload's Big Trades panel, NOT
# part of the trading decision. Calibrated 2026-07-27 from ~15 live
# bars/stock (~1.5x each stock's observed average bar volume, so a genuine
# spike is needed to fire, not every bar):
#   HDFC ~37.5k avg -> 55k | ICICI ~32.3k avg -> 48k | AXIS ~18.7k avg -> 28k
#   SBI ~11.8k avg -> 18k  | KOTAK ~28.7k avg -> 43k | INDUSIND ~11k avg -> 16.5k
BN_QTY_THRESHOLD_HDFC       = 55_000.0
BN_QTY_THRESHOLD_ICICI      = 48_000.0
BN_QTY_THRESHOLD_SBI        = 18_000.0
BN_QTY_THRESHOLD_AXIS       = 28_000.0
BN_QTY_THRESHOLD_KOTAK      = 43_000.0
BN_QTY_THRESHOLD_INDUSIND   = 16_500.0
BN_QTY_INTERVAL_MULTIPLIER  = 1.0

# Sizes the IV-estimate lookback window (app.engine.bn_pricing.estimate_iv
# is fed a slice of this many closes — see scheduler.py/backtest/data.py/
# backtest/engine.py) and backtest's warmup-days calculation. Originally
# sized for the now-removed composite RSI/MACD/EMA indicator gate's own
# convergence requirement (hence the name) — repurposed, not dead, so it
# stays; the RSI/EMA/MACD/pattern-score constants that gate actually used
# were removed with bn_signals.py itself.
BN_INDICATOR_LOOKBACK_BARS = 200

# BN Risk — starting paper-account balance. The old index-points target/
# stop/breakeven/trail constants (BN_TARGET_POINTS etc.) were removed
# 2026-09-21 alongside the scalp-strategy rewrite — see BN_SCALP_TARGET_RS/
# STOP_RS/TIME_STOP_S under "Static: Scalping strategy" below, which are
# premium-₹-denominated, not index-points, and frozen into the trade the
# same way these used to be.
BN_STARTING_FUNDS     = 100_000.0   # ₹ — seeds the persisted funds balance once

# BN Options Pricing — synthetic Black-Scholes premium, no real option data
BN_RISK_FREE_RATE      = 0.065
BN_IV_MIN               = 0.20
BN_IV_MAX               = 0.70
BN_IV_DEFAULT            = 0.28
BN_IV_LOOKBACK_BARS      = 50
BN_IV_MANUAL_ENABLED     = False
BN_IV_MANUAL_VALUE       = 0.30

# BN Options Costs — placeholder rates (India options STT/txn charges change
# periodically; confirm current figures before trusting absolute backtest
# ₹ P&L — relative signal quality is insensitive to this).
BN_COST_BROKERAGE_FLAT = 20.0        # ₹ per executed order, flat
BN_COST_STT_SELL_PCT   = 0.001       # STT on sell-side premium value
BN_COST_TXN_PCT        = 0.0005      # exchange transaction charge
BN_COST_GST_PCT        = 0.18        # GST on (brokerage + txn)
BN_COST_SEBI_PCT       = 0.000001    # SEBI turnover fee

# Tick-wise engine
TICK_EVAL_INTERVAL_MS = 100

# ── Static: NF (Nifty 50) Strategy — parallel to the BN block above.
#
# The old sideways-range/momentum/leader-vote/volume-surge/composite-
# indicator gate sequence (NF_SIDEWAYS_RANGE_MIN, NF_MOMENTUM_THRESHOLD,
# NF_ATR_PERIOD, NF_SAME_DIRECTION_REQUIRED, NF_ENTRY_COOLDOWN_S, and the
# RSI/EMA/MACD/score constants below) was fully REMOVED 2026-09-21 alongside
# nf_signals.py (which implemented it) — see BN's equivalent comment above.
# Unlike BN_SAME_DIRECTION_REQUIRED, NF's own version had no other reader
# (no NF equivalent of signal_study.py exists), so it's gone with the rest.

# NF Strategy — per-stock volume-surge thresholds. PLACEHOLDER values — no
# live volume data yet for these stocks on this feed; calibrate the same way
# BN's own thresholds were (see BN_QTY_THRESHOLD_* comment above). Same
# "informational stockCandles annotation, not the trading decision" caveat.
NF_QTY_THRESHOLD_HDFC       = 55_000.0
NF_QTY_THRESHOLD_RELIANCE   = 40_000.0
NF_QTY_THRESHOLD_ICICI      = 48_000.0
NF_QTY_THRESHOLD_INFY       = 35_000.0
NF_QTY_THRESHOLD_BHARTIARTL = 30_000.0
NF_QTY_THRESHOLD_ITC        = 30_000.0
NF_QTY_THRESHOLD_HCLTECH    = 20_000.0
NF_QTY_THRESHOLD_LT         = 15_000.0
NF_QTY_THRESHOLD_KOTAK      = 43_000.0
NF_QTY_THRESHOLD_AXIS       = 28_000.0
NF_QTY_THRESHOLD_SBI        = 18_000.0
NF_QTY_THRESHOLD_HUL        = 15_000.0
NF_QTY_INTERVAL_MULTIPLIER  = 1.0

# IV-estimate lookback window sizing — see BN_INDICATOR_LOOKBACK_BARS's
# comment above (repurposed the same way, not dead).
NF_INDICATOR_LOOKBACK_BARS = 200

# No NF_STARTING_FUNDS — BN and NF share one paper account balance
# (st.funds), seeded once from BN_STARTING_FUNDS; see scheduler._load_funds.

# NF Options Pricing — synthetic Black-Scholes premium, no real option data
NF_RISK_FREE_RATE      = 0.065
NF_IV_MIN               = 0.20
NF_IV_MAX               = 0.70
NF_IV_DEFAULT            = 0.28
NF_IV_LOOKBACK_BARS      = 50
NF_IV_MANUAL_ENABLED     = False
NF_IV_MANUAL_VALUE       = 0.30

# NF Options Costs — same placeholder rates as BN (confirm current India
# options STT/exchange-txn figures before trusting absolute ₹ P&L).
NF_COST_BROKERAGE_FLAT = 20.0
NF_COST_STT_SELL_PCT   = 0.001
NF_COST_TXN_PCT        = 0.0005
NF_COST_GST_PCT        = 0.18
NF_COST_SEBI_PCT       = 0.000001

# ── Static: Scalping strategy — Top-8 weighted-basket VWAP momentum + deep-
# ITM strike + weighted-order-book-imbalance (W-OBI) execution filter + a
# 12-second target/stop/time-scratch lifecycle. REPLACES the leader-vote/
# composite-indicator entry condition and the index-points target/stop
# above for BOTH instruments (2026-09-21, explicit user decision) — see
# app/engine/bn_entry_exit.py / nf_entry_exit.py's rewritten evaluate_entry/
# evaluate_exit. bn_signals.py/nf_signals.py and the BN_SIDEWAYS_RANGE_MIN-
# style constants above are left in place, unused, per this repo's existing
# revert-safety convention (see the 2026-09-19 BN rewrite's own comments).
#
# Fully simulated, live AND backtest — no real broker/order-routing or
# option order-book connection exists anywhere in this repo (see
# app/engine/wobi.py's module docstring for why W-OBI is a documented
# synthetic proxy, not real market depth, and CLAUDE.md's "Options pricing"
# note for why strike/premium always were synthetic Black-Scholes too).
#
# Following the 2026-09-09 "remove all except threshold" precedent (see
# this file's own module docstring + settings.py's), every value below is a
# plain STATIC attribute, not a Settings-page dynamic tunable — restart-only
# to change, same as everything else in this file (e.g. BN_STARTING_FUNDS
# above).

def _top_n_basket(weights: Dict[str, float], n: int) -> Dict[str, float]:
    """Top `n` tokens by index weight, renormalized to sum to 1.0."""
    top = sorted(weights.items(), key=lambda kv: -kv[1])[:n]
    total = sum(w for _, w in top)
    return {tok: w / total for tok, w in top}


SCALP_BASKET_SIZE = 8

# Top 8 of each index's own already-researched weight table (BN_INDEX_WEIGHTS/
# NF_INDEX_WEIGHTS above — see their own "real weights" provenance comments),
# renormalized to sum to 1.0 over just these 8. Restricted to
# BN_INDEX_WEIGHTS_CONFIRMED/NF_INDEX_WEIGHTS_CONFIRMED FIRST — ranking over
# the raw weight dicts directly would let an unconfirmed stock's flat
# equal-weight PLACEHOLDER (e.g. Union Bank of India/Yes Bank at BankNifty's
# 100/14=7.14%, since neither has ever had a real weight supplied — see
# BN_INDEX_WEIGHTS_CONFIRMED's own comment above) outrank a real, smaller,
# CONFIRMED weight (IndusInd Bank 6.28%, AU Small Finance Bank 5.55%) —
# caught by inspection when this ranked Union Bank/Yes Bank into BN's top 8
# ahead of both. For Nifty 50 this reproduces the user-supplied basket
# almost exactly (HDFC Bank/Reliance/ICICI/Bharti Airtel/TCS/ITC/L&T/SBI all
# rank in the real top 8); computed programmatically (not hand-copied) so a
# future weight-table correction flows through automatically instead of
# silently drifting out of sync.
BN_SCALP_BASKET: Dict[str, float] = _top_n_basket(
    {tok: w for tok, w in BN_INDEX_WEIGHTS.items() if tok in BN_INDEX_WEIGHTS_CONFIRMED},
    SCALP_BASKET_SIZE)
NF_SCALP_BASKET: Dict[str, float] = _top_n_basket(
    {tok: w for tok, w in NF_INDEX_WEIGHTS.items() if tok in NF_INDEX_WEIGHTS_CONFIRMED},
    SCALP_BASKET_SIZE)

# Composite momentum score threshold (%, weighted sum of each basket leg's
# (ltp-vwap)/vwap*100 — see app/engine/scalp_signals.compute_basket_reading).
# Score is a small percentage (basket legs rarely deviate >0.5% from session
# VWAP intraday), so this is deliberately a small number, not points.
BN_SCALP_SCORE_THRESHOLD = 0.08
NF_SCALP_SCORE_THRESHOLD = 0.08

# Deep-ITM offset (points) from spot — CE: spot - offset, PE: spot + offset
# (app/engine/bn_pricing.get_itm_strike / nf_pricing.get_itm_strike). NF's
# 150 is the user-specified value (~3 strikes ITM at Nifty 50's real
# 50-point strike step); BN's 300 is this repo's own equivalent for
# BankNifty's real 100-point strike step (~3 strikes ITM the same way) —
# BankNifty has no source-of-truth value for this the way NF's does.
#
# TESTED 2026-09-21 against this repo's own black_scholes (IV floor 0.20,
# realistic T out to BankNifty's real ~30-day monthly cycle / NF's ~7-day
# weekly one): NEITHER offset reliably clears delta > 0.75 except very
# close to expiry — typical delta at these offsets runs ~0.58-0.72
# depending on days-to-expiry, only crossing 0.75 within a day or so of
# expiry. This is an explicit, confirmed user decision to keep both
# offsets as-is anyway (treating "delta > 0.75" as an aspirational target
# consistent with "a few strikes ITM," not a hard runtime guarantee) —
# see the code-review conversation this was confirmed in. To actually
# force delta > 0.75 across the full cycle would need a MUCH deeper
# offset (roughly BN~1200+, NF~500+ per that same test), which was
# considered too far from real strike liquidity to be worth it.
BN_ITM_OFFSET_POINTS = 300.0
NF_ITM_OFFSET_POINTS = 150.0

# W-OBI execution filter minimum ratio — see app/engine/wobi.py. Same
# formula and threshold for BOTH CE and PE entries: W-OBI is computed on
# the CHOSEN option's OWN synthetic depth (not the underlying's), and for
# either option type a bid-heavy top-2 book is what "path of least
# resistance" means for someone about to go long that specific contract.
BN_WOBI_MIN_RATIO = 2.5
NF_WOBI_MIN_RATIO = 2.5

# 12-second execution lifecycle — target/stop are on the OPTION PREMIUM
# (₹), not the underlying index, and apply identically regardless of
# CE/PE: either way this strategy is LONG one option leg, so "premium up
# ₹target = win, premium down ₹stop = loss" needs no direction branching
# (unlike the old index-points target/stop, which did). See
# bn_entry_exit.evaluate_exit / nf_entry_exit.evaluate_exit.
BN_SCALP_TARGET_RS   = 2.75   # user spec: ₹2.50–3.00 — midpoint
BN_SCALP_STOP_RS     = 2.00
NF_SCALP_TARGET_RS   = 2.75
NF_SCALP_STOP_RS     = 2.00

# Hard time-stop (seconds) — if neither target nor stop is touched first,
# force a scratch exit the instant this elapses. Checked every
# TICK_EVAL_INTERVAL_MS (100ms) tick against (now - trade.entry_time), NOT
# a separate asyncio.sleep(12)-based task — see the handover note on why:
# short version, the existing 100ms tick loop already re-evaluates every
# open trade ~120x within a 12s window, so a second concurrent timer task
# racing to mutate the SAME st.active_trade/st.active_trade_nf (this
# engine's hard "at most one active trade" invariant, see CLAUDE.md) would
# only add risk, not precision.
BN_SCALP_TIME_STOP_S = 12.0
NF_SCALP_TIME_STOP_S = 12.0

# Marketable-exit slippage (₹) modeled on a forced time-scratch — "cancel
# the resting passive target order and fire a marketable limit exit (Bid-1
# tick)" crosses the spread, so the fill is a little worse than the
# last-marked premium. Same idea as the existing (index-side) SLIPPAGE_BPS
# backtest convention, just premium-denominated here since that's what this
# strategy's exit is denominated in.
BN_SCALP_SCRATCH_SLIPPAGE_RS = 0.10
NF_SCALP_SCRATCH_SLIPPAGE_RS = 0.10

# Cooldown between scalp trades — much shorter than the old (now-removed)
# 60s BN_ENTRY_COOLDOWN_S/NF_ENTRY_COOLDOWN_S, since a trade now resolves
# in ~12s, not minutes.
BN_SCALP_COOLDOWN_S = 15.0
NF_SCALP_COOLDOWN_S = 15.0

# ── Risk guardrails (shared by BOTH instruments — one set of limits, not a
# separate pair per instrument): trading windows + a hard cap on executed
# trades/day, enforced inside evaluate_entry for the algo path and again
# inside bn_trade.place_paper_order/nf_trade.place_paper_order (covers the
# manual-order path too — see there) so nothing can exceed it either way.
SCALP_WINDOW1_START_HOUR, SCALP_WINDOW1_START_MIN = 9, 45
SCALP_WINDOW1_END_HOUR,   SCALP_WINDOW1_END_MIN   = 11, 15
SCALP_WINDOW2_START_HOUR, SCALP_WINDOW2_START_MIN = 13, 45
SCALP_WINDOW2_END_HOUR,   SCALP_WINDOW2_END_MIN   = 14, 45
SCALP_MAX_TRADES_PER_DAY = 5

# ── Dynamic tunables — hard defaults. Only BN/NF Alerts remain here (2026-
# 09-09, explicit user decision "remove all except threshold") — everything
# else above is now a plain static attribute. ─────────────────────────────
_DEFAULTS: Dict[str, Any] = {
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
    "BN_PRICE_ALERT_PTS_HDFC":     0.4,
    "BN_PRICE_ALERT_PTS_ICICI":    0.4,
    "BN_PRICE_ALERT_PTS_SBI":      0.4,
    "BN_PRICE_ALERT_PTS_AXIS":     0.4,
    "BN_PRICE_ALERT_PTS_KOTAK":    0.4,
    "BN_PRICE_ALERT_PTS_INDUSIND": 0.4,

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
