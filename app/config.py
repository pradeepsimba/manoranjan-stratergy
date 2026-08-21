from __future__ import annotations

"""
Configuration — static system settings plus the DYNAMIC tunables layer.

Static values (endpoints, credentials, structural pool/buffer sizes, the seed
stock catalog) are plain module attributes and require a restart to change.

Everything else lives in _DEFAULTS and is resolved through the module-level
__getattr__ (PEP 562) with this precedence:

    1. runtime overrides — the Settings page, persisted in the app_settings
                            table and applied at startup
    2. the hard default below

`import app.config as cfg; cfg.STARTING_FUNDS` therefore always returns the
CURRENT value. Code must read cfg attributes at call time — never copy them
into module-level constants or default-argument values, or they freeze at
import and stop being dynamic.

The editable registry (labels, types, bounds, grouping) lives in
app/services/settings.py — add new tunables in BOTH places.
"""

import os
from typing import Any, Dict, List, Optional

# ── Static: custom market data server ────────────────────────────────────────
API_HOST          = "algo.vaangamart.com"
API_URL_TEMPLATE  = "https://{}:8000/api/historical-data/?from_date={}&to_date={}"
CLIENTSTATUS_URL  = f"https://{API_HOST}:8000/api/clientstatus/"
WS_URL            = f"ws://{API_HOST}:8083/historical-data"

# ── Static: credentials / DSN ─────────────────────────────────────────────────
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://postgres:password@localhost/trading_db",
)
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-only-insecure-secret-change-me")

# ── Static: data intervals ────────────────────────────────────────────────────
INTERVAL_5M = "5m"

# ── Static: structural sizes (pools/buffers built once — restart to change) ──
HIST_BATCH_SIZE     = 50    # max stocks per historical API request (server rejects >50 with 400)
MAX_CANDLE_BUFFER   = 300   # per-symbol in-memory candle buffer (deque maxlen)
WS_FILTER_BATCH_SIZE = 40   # max (symbol, interval) pairs per single WS connection

# ── Static: instrument discovery seed ─────────────────────────────────────────
# Fallback candidate catalog (name -> token), used ONLY if the live
# `/api/clientstatus/` call fails at discovery time. This is a snapshot of
# that same endpoint's real response (fetched 2026-07-13) — every name here is
# the server's own canonical stockname text, so there's no "wrong name"
# guesswork (the Kotak-naming trap from the old BN engine doesn't apply: these
# names come straight from the source of truth). Discovery still verifies
# each one actually has historical candle data before marking it tradable —
# being listed here doesn't guarantee OHLC history exists (e.g. a very
# recent IPO). Equities only — see SEED_INDEX_CANDIDATES below for the
# separate index fallback.
SEED_STOCK_CANDIDATES: Dict[str, str] = {
    "360 One WAM": "13061", "ABB": "13", "Adani Energy Solutions": "10217",
    "Adani Enterprises": "25", "Adani Green Energy": "3563",
    "Adani Ports & SEZ": "15083", "Aditya Birla Capital": "21614",
    "Alkem Laboratories": "11703", "Amber Enterprises": "1185",
    "Ambuja Cements": "1270", "Angel One": "324", "APL Apollo Tubes": "25780",
    "Apollo Hospitals": "157", "Ashok Leyland": "212", "Asian Paints": "236",
    "Astral": "14418", "AU Small Finance Bank": "21238",
    "Aurobindo Pharma": "275", "Avenue Supermarts DMart": "19913",
    "AXIS BANK": "5900", "Bajaj Auto": "16669", "Bajaj Finance": "317",
    "Bajaj Finserv": "16675", "Bajaj Holdings & Investments": "305",
    "Bandhan Bank": "2263", "Bank of Baroda": "4668", "Bank of India": "4745",
    "Bharat Dynamics": "2144", "Bharat Electronics": "383",
    "Bharat Forge": "422", "Bharat Heavy Electricals": "438",
    "Bharat Petroleum": "526", "Bharti Airtel": "10604", "Biocon": "11373",
    "Blue Star": "8311", "Bosch": "2181", "Britannia Industries": "547",
    "BSE": "19585", "CAMS": "342", "Canara Bank": "10794", "CDSL": "21174",
    "CENTRAL BANK OF INDIA": "14894", "CG Power & Industrial Solutions": "760",
    "Cholamandalam Investment": "685", "Cipla": "694", "Coal India": "20374",
    "Cochin Shipyard": "21508", "Coforge": "11543", "Colgate Palmolive": "15141",
    "Container Corporation of India": "4749", "Crompton Greaves": "17094",
    "Cummins": "1901", "Dabur India": "772", "Dalmia Bharat": "8075",
    "Delhivery": "9599", "Divis Laboratories": "10940", "DLF": "14732",
    "Dr Reddys Laboratories": "881", "Eicher Motors": "910",
    "Exide Industries": "676", "Federal Bank": "1023", "Force Motors": "11573",
    "Fortis Healthcare": "14592", "GAIL": "4717",
    "Glenmark Pharmaceuticals": "7406", "GMR Airports": "13528",
    "Godfrey Phillips": "1181", "Godrej Consumer Products": "10099",
    "Godrej Properties": "17875", "Grasim Industries": "1232",
    "Havells": "9819", "HCL Technologies": "7229", "HDFC AMC": "4244",
    "HDFC Bank": "1333", "HDFC Life Insurance": "467", "Hero Motocorp": "1348",
    "Hindalco Industries": "1363", "Hindustan Aeronautics": "2303",
    "Hindustan Petroleum": "1406", "Hindustan Unilever": "1394",
    "Hindustan Zinc": "1424", "Hitachi Energy": "18457",
    "Hyundai Motor India": "25844", "ICICI BANK": "4963",
    "ICICI Lombard General Insurance": "21770",
    "ICICI Prudential Life Insurance": "18652", "IDFC First Bank": "11184",
    "Indian Bank": "14309", "Indian Energy Exchange": "220",
    "Indian Hotels Company": "1512", "Indian Oil Corporation": "1624",
    "INDRAPRASTHA GAS": "11262", "Indus Towers": "29135",
    "Indusind Bank": "5258", "Info Edge": "13751", "INFOSYS": "1594",
    "Inox Wind": "7852", "Interglobe Aviation": "11195", "IREDA": "20261",
    "IRFC": "2029", "ITC": "1660", "Jindal Steel": "6733",
    "Jio Financial Services": "18143", "JSW Energy": "17869",
    "JSW Steel": "11723", "Jubilant FoodWorks": "18096",
    "Kalyan Jewellers": "2955", "Kaynes Technology India": "12092",
    "KEI Industries": "13310", "KFin Technologies": "13359",
    "Kotak Bank": "1922", "KPIT Technologies": "9683",
    "Larsen & Toubro": "11483", "Laurus Labs": "19234",
    "LIC Housing Finance": "1997", "LIC of India": "9480", "Lupin": "10440",
    "Mahindra & Mahindra": "2031", "Manappuram Finance": "19061",
    "Marico": "4067", "Maruti Suzuki": "10999",
    "Max Financial Services": "2142", "Max Healthcare Institute": "22377",
    "Mazagon Dock Shipbuilders": "509", "MCX": "31181",
    "Motilal Oswal Financial Services": "14947", "Mphasis": "4503",
    "Muthoot Finance": "23650", "NALCO": "6364", "NBCC": "31415",
    "Nestle": "17963", "NHPC": "17400", "Nippon Life India AMC": "357",
    "NMDC": "15332", "NTPC": "11630", "Nuvama Wealth Management": "18721",
    "Nykaa": "6545", "Oberoi Realty": "20242",
    "Oil & Natural Gas Corporation": "2475", "Oil India": "17438",
    "One 97 Communications": "6705",
    "Oracle Financial Services Software": "10738", "Page Industries": "14413",
    "Patanjali Foods": "17029", "PB FinTech": "6656",
    "Persistent Systems": "18365", "PG Electroplast": "25358",
    "Phoenix Mills": "14552", "PI Industries": "24184",
    "Pidilite Industries": "2664", "PNB Housing Finance": "18908",
    "Polycab": "9590", "Power Finance Corporation": "14299",
    "Power Grid Corporation of India": "14977",
    "Prestige Estates Projects": "20302", "Punjab National Bank": "10666",
    "Radico Khaitan": "10990", "Rail Vikas Nigam": "9552", "RBL Bank": "18391",
    "REC": "15355", "Reliance Industries": "2885",
    "Samvardhana Motherson International": "4204", "SBI Cards": "17971",
    "SBI Life Insurance": "21808", "Shree Cement": "3103",
    "Shriram Finance": "4306", "Siemens": "3150", "Solar Industries": "13332",
    "Sona BLW Precision Forgings": "4684", "SRF": "3273",
    "State Bank of India": "3045", "Steel Authority of India": "2963",
    "Sun Pharmaceutical": "3351", "Supreme Industries": "3363",
    "Suzlon Energy": "12018", "Swiggy": "27066",
    "Tata Consultancy Services": "11536", "Tata Consumer Products": "3432",
    "Tata Elxsi": "3411", "TATA MOTORS": "3456", "Tata Power": "3426",
    "Tata Steel": "3499", "TATA TECHNOLOGIES": "20293", "Tech Mahindra": "13538",
    "Titan": "3506", "Torrent Pharmaceuticals": "3518", "Trent": "1964",
    "Tube Investment": "312", "TVS Motors": "8479", "UltraTech Cement": "11532",
    "Union Bank of India": "10753", "UNO Minda": "14154", "UPL": "11287",
    "Varun Beverages": "18921", "VEDANTA": "3063", "Vodafone Idea": "14366",
    "Voltas": "3718", "Wipro": "3787", "Yes Bank": "11915",
    "Zydus Life Science": "7929",
}

# Index fallback candidates (name -> token) — same snapshot-of-clientstatus
# principle as SEED_STOCK_CANDIDATES, just kept separate since these carry
# asset_type='INDEX' rather than 'EQUITY' (see instrument_discovery.py). An
# index has no delivery mechanism, so it's MIS-only — order placement blocks
# CNC for these (see app/engine/orders.py's place_order).
SEED_INDEX_CANDIDATES: Dict[str, str] = {
    "NIFTY 50": "99926000",
    "BANKNIFTY": "26009",
}

# ── Dynamic tunables — hard defaults ──────────────────────────────────────────
_DEFAULTS: Dict[str, Any] = {
    # Session timings (IST)
    "MARKET_OPEN_HOUR":  9,  "MARKET_OPEN_MIN":  15,   # historical load + WS subscribe + orders open
    "MARKET_CLOSE_HOUR": 15, "MARKET_CLOSE_MIN": 30,   # session end / daily reset
    "MIS_SQUAREOFF_HOUR": 15, "MIS_SQUAREOFF_MIN": 20, # auto square-off all open MIS positions

    # Accounts
    "STARTING_FUNDS": 500_000.0,   # ₹ — seeds a new user's virtual funds balance

    # Engine cadence
    "TICK_EVAL_INTERVAL_MS": 100,   # limit-order matching / mark-to-market loop

    # Leverage — MIS (intraday) orders only; CNC is always unleveraged/cash-only.
    "MIS_LEVERAGE": 5.0,   # e.g. 5.0 => an MIS order only blocks qty*price/5 as margin
}

_runtime_overrides: Dict[str, Any] = {}

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
