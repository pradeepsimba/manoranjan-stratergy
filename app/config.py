from __future__ import annotations

import os

# ── Custom Market Data Server ─────────────────────────────────────────────────
API_HOST          = "35.234.219.141"
API_URL_TEMPLATE  = "https://{}:8000/api/historical-data/?from_date={}&to_date={}"
WS_URL            = f"ws://{API_HOST}:8083/historical-data"
CLIENT_STATUS_URL = f"https://{API_HOST}:8000/api/clientstatus/"

# ── Gemini AI pre-market filter ───────────────────────────────────────────────
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = "gemini-2.5-flash"
GEMINI_MAX_STOCKS = 40   # cap on the bullish shortlist returned by the screen

# ── PostgreSQL ────────────────────────────────────────────────────────────────
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "postgresql://postgres:password@localhost/trading_db",
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
INTRADAY_LEVERAGE        = 5         # Standard NSE intraday equity leverage
MAX_CONCURRENT_POSITIONS = 3         # Hard cap on simultaneous open positions
DAILY_LOSS_LIMIT         = 2_000.0   # ₹2,000 daily drawdown ceiling

# ── Strategy parameters ───────────────────────────────────────────────────────
ADX_PERIOD         = 14
ADX_THRESHOLD      = 20.0
RSI_PERIOD         = 14
RSI_OVERSOLD       = 30
RSI_RISING_BARS    = 3      # RSI must rise for this many consecutive bars
SWING_LOW_BARS     = 10     # Lookback bars for structural support floor
SUPPORT_TOUCH_PCT  = 0.015  # Price within 1.5% of support = "at support"
MIN_SL_OFFSET      = 5.0    # Minimum SL distance in ₹ (prevents oversized qty on tiny stops)
VOLUME_MA_PERIOD   = 20
VOLUME_MULTIPLIER  = 1.5    # Bar volume must exceed 1.5× 20-bar avg
RR_RATIO           = 1.5    # target_offset = sl_offset × 1.5
MACD_CROSS_BARS    = 3      # Allow entry up to N bars after a bullish MACD cross

# Tail length fed to TA-Lib per scan. 120 bars lets RSI(14)/ADX(14)/MACD(26,9)
# fully converge (Wilder smoothing) while skipping the multi-day warmup history.
TALIB_LOOKBACK     = 120

# ── Data intervals supported by custom server ─────────────────────────────────
INTERVAL_5M  = "5m"
INTERVAL_1H  = "1h"
INTERVAL_1D  = "1d"

# ── NIFTY 50 token on NSE ─────────────────────────────────────────────────────
NIFTY50_TOKEN  = "99926000"
NIFTY50_NAME   = "NIFTY 50"

# ── Performance ────────────────────────────────────────────────────────────────
HIST_BATCH_SIZE = 100   # max stocks per single historical API request
SCAN_WORKERS    = 16    # ThreadPoolExecutor size for the parallel scan

# ── Tick-wise engine ─────────────────────────────────────────────────────────
# Cadence of the tick-driven evaluation loop in ACTIVE. Signals are recomputed
# for every stock that ticked since the previous cycle, on the forming bar; SL/
# target are checked against the live price. 0 = run as fast as the loop allows.
TICK_EVAL_INTERVAL_MS = 100

# ── Backtest ──────────────────────────────────────────────────────────────────
BACKTEST_WARMUP_DAYS = 7      # extra calendar days fetched before the range for indicator warmup
SLIPPAGE_BPS         = 2.0    # 0.02% slippage applied to entry and exit fills

# Realistic intraday-equity round-trip cost model (all rates as fractions of turnover)
COST_BROKERAGE_PCT = 0.0003     # 0.03% per executed order
COST_BROKERAGE_CAP = 20.0       # ₹20 cap per order
COST_STT_SELL      = 0.00025    # 0.025% securities txn tax, sell side only
COST_TXN_CHARGE    = 0.0000297  # NSE exchange transaction charge
COST_GST           = 0.18       # 18% GST on (brokerage + txn charge)
COST_STAMP_BUY     = 0.00003    # 0.003% stamp duty, buy side only
COST_SEBI          = 0.000001   # SEBI turnover fee
