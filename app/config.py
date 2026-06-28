from __future__ import annotations

import os

# ── Custom Market Data Server ─────────────────────────────────────────────────
API_HOST          = "35.234.219.141"
API_URL_TEMPLATE  = "https://{}:8000/api/historical-data/?from_date={}&to_date={}"
WS_URL            = f"ws://{API_HOST}:8083/historical-data"
CLIENT_STATUS_URL = f"https://{API_HOST}:8000/api/clientstatus/"

# ── Gemini AI pre-market filter ───────────────────────────────────────────────
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = "gemini-3.5-flash"
GEMINI_MIN_STOCKS = 15
GEMINI_MAX_STOCKS = 40

# ── PostgreSQL ────────────────────────────────────────────────────────────────
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://postgres:password@localhost/trading_db",
)

# ── NSE Universe filters ──────────────────────────────────────────────────────
MIN_ADV   = 1_000_000   # Average Daily Volume (shares)
MIN_PRICE = 100.0       # Minimum stock price ₹100

# ── Instrument master (public — no auth required) ─────────────────────────────
INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelbroking.com"
    "/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# ── Timing (IST) ──────────────────────────────────────────────────────────────
PREMARKET_HOUR   = 9;  PREMARKET_MIN   = 0    # Gemini filter runs here
MARKET_OPEN_HOUR = 9;  MARKET_OPEN_MIN = 15   # Wait zone start
SCAN_START_HOUR  = 9;  SCAN_START_MIN  = 45   # Active scanning starts
CUTOFF_HOUR      = 14; CUTOFF_MIN      = 30   # No new entries after this
SESSION_END_HOUR = 15; SESSION_END_MIN = 30   # Terminate session

# ── Risk & Capital ────────────────────────────────────────────────────────────
RISK_PER_TRADE           = 500.0     # ₹500 fixed risk capital per setup
ACCOUNT_BALANCE          = 40_000.0  # ₹40,000 base capital
INTRADAY_LEVERAGE        = 5         # Standard Angel One intraday equity leverage
MAX_CONCURRENT_POSITIONS = 3         # Hard cap on simultaneous open positions
DAILY_LOSS_LIMIT         = 2_000.0   # ₹2,000 daily drawdown ceiling

# ── Strategy parameters ───────────────────────────────────────────────────────
ADX_PERIOD         = 14
ADX_THRESHOLD      = 20.0
RSI_PERIOD         = 14
RSI_OVERSOLD       = 30
RSI_RISING_BARS    = 3      # RSI must rise for this many consecutive bars
SWING_LOW_BARS     = 10     # Lookback bars for structural support floor
SUPPORT_TOUCH_PCT  = 0.005  # Price within 0.5% of support = "at support"
VOLUME_MA_PERIOD   = 20
VOLUME_MULTIPLIER  = 1.5    # Bar volume must exceed 1.5× 20-bar avg
RR_RATIO           = 1.5    # target_offset = sl_offset × 1.5

# ── Data intervals supported by custom server ─────────────────────────────────
INTERVAL_5M  = "5m"
INTERVAL_1H  = "1h"
INTERVAL_1D  = "1d"

# ── NIFTY 50 token on NSE ─────────────────────────────────────────────────────
NIFTY50_TOKEN  = "26000"
NIFTY50_NAME   = "NIFTY 50"
NSE_EXCHANGE   = "NSE"

# ── Performance ────────────────────────────────────────────────────────────────
HIST_BATCH_SIZE = 100   # max stocks per single historical API request
SCAN_WORKERS    = 16    # ThreadPoolExecutor size for parallel bar-close scan
