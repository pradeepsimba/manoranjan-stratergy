from dataclasses import dataclass
from typing import Dict, List

API_HOST         = "35.234.219.141"
API_URL_TEMPLATE = "https://{}:8000/api/historical-data/?from_date={}&to_date={}"
WS_URL           = f"ws://{API_HOST}:8083/historical-data"

INDEX_NAME   = "BANKNIFTY"
INDEX_SYMBOL = "26009"

TARGET            = 35.0
STOPLOSS          = 18.0
BREAKEVEN_TRIGGER = 12.0
TRAIL_TRIGGER     = 18.0
TRAIL_DISTANCE    = 12.0
LOT_SIZE          = 30
SAME_DIRECTION_REQUIRED = 3

DEFAULT_FUNDS  = 100_000.0
RISK_FREE_RATE = 0.065

@dataclass(frozen=True)
class Stock:
    name: str
    symbol: str

STOCKS: List[Stock] = [
    Stock("BANKNIFTY",            "26009"),
    Stock("HDFC BANK",            "1333"),
    Stock("ICICI BANK",           "4963"),
    Stock("AXIS BANK",            "5900"),
    Stock("STATE BANK OF INDIA",  "3045"),
    Stock("KOTAK MAHINDRA BANK",  "1922"),
    Stock("INDUSIND BANK",        "5258"),
    Stock("AU SMALL FINANCE BANK","21238"),
    Stock("FEDERAL BANK",         "1023"),
    Stock("IDFC FIRST BANK",      "11184"),
    Stock("PUNJAB NATIONAL BANK", "10666"),
    Stock("CANARA BANK",          "10794"),
]

LEADER_STOCKS: List[str] = [
    "HDFC BANK", "ICICI BANK", "AXIS BANK",
    "STATE BANK OF INDIA", "KOTAK MAHINDRA BANK", "INDUSIND BANK",
]

INDEX_WEIGHTS: Dict[str, float] = {
    "1333":  31.86,
    "4963":  20.14,
    "3045":  17.83,
    "1922":   8.79,
    "5900":   7.96,
    "5258":   2.92,
    "10666":  2.86,
    "10794":  2.40,
    "11184":  1.40,
    "21238":  1.35,
    "1023":   1.19,
}

STOCK_QTY_THRESHOLD: Dict[str, int] = {
    "HDFC BANK":           2000,
    "ICICI BANK":          2000,
    "AXIS BANK":            900,
    "STATE BANK OF INDIA": 1200,
    "KOTAK MAHINDRA BANK": 1500,
    "INDUSIND BANK":        600,
}

MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 15
MARKET_CLOSE_HOUR  = 15
MARKET_CLOSE_MIN   = 30
ENTRY_START_HOUR   = 9
ENTRY_START_MIN    = 30
ENTRY_END_HOUR     = 15
ENTRY_END_MIN      = 0
