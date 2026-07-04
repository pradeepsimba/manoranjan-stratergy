from __future__ import annotations

"""
Dynamic settings registry + persistence.

SPEC declares every runtime-editable tunable: display metadata, type, bounds,
and whether it may be overridden per-backtest-run ("bt"). Values themselves
live in app.config (defaults + runtime overrides); this module validates user
input, expands virtual "HH:MM" time settings into their HOUR/MIN config pairs,
and persists overrides to the app_settings table so they survive restarts.

Add a new tunable by adding its default to app.config._DEFAULTS AND an entry
here — nothing else is required for it to appear on the Settings page.
"""

import re
from typing import Any, Dict, List, Optional

import app.config as cfg

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Keys in app_settings that are NOT config overrides (e.g. day-scoped watchlist
# edits). Prefixed with "_" and skipped by the settings loader.
INTERNAL_PREFIX = "_"
WATCHLIST_OVERRIDES_KEY = "_WATCHLIST_OVERRIDES"


def _s(key: str, label: str, type_: str, group: str, *,
       min_: Optional[float] = None, max_: Optional[float] = None,
       step: Optional[float] = None, help_: str = "", bt: bool = True,
       parts: Optional[tuple] = None,
       choices: Optional[list] = None) -> Dict[str, Any]:
    return {"key": key, "label": label, "type": type_, "group": group,
            "min": min_, "max": max_, "step": step, "help": help_,
            "bt": bt, "parts": parts, "choices": choices}


SPEC: List[Dict[str, Any]] = [
    # ── AI pre-market screen (live only) ─────────────────────────────────────
    _s("GEMINI_ENABLED", "Gemini screen enabled", "bool", "AI Pre-market Screen",
       help_="Off = skip the AI screen and trade the capped full high-volume list.", bt=False),
    _s("GEMINI_MODEL", "Gemini model id", "str", "AI Pre-market Screen",
       help_="Must be a real google-genai model id; a bad id silently disables the screen.", bt=False),
    _s("GEMINI_MAX_STOCKS", "Max tradeable stocks", "int", "AI Pre-market Screen",
       min_=1, max_=100, help_="Cap on the shortlist / fallback list (WS buffer limit).", bt=False),

    # ── Session timings ───────────────────────────────────────────────────────
    _s("PREMARKET_TIME", "Pre-market screen", "time", "Session Timings",
       parts=("PREMARKET_HOUR", "PREMARKET_MIN"), bt=False,
       help_="When the watchlist fetch + Gemini screen run."),
    _s("MARKET_OPEN_TIME", "Market open / data load", "time", "Session Timings",
       parts=("MARKET_OPEN_HOUR", "MARKET_OPEN_MIN"), bt=False,
       help_="Historical load + WebSocket subscribe."),
    _s("SCAN_START_TIME", "Entry scanning starts", "time", "Session Timings",
       parts=("SCAN_START_HOUR", "SCAN_START_MIN"),
       help_="No entries before this time (also used by the backtest)."),
    _s("CUTOFF_TIME", "Entry cutoff", "time", "Session Timings",
       parts=("CUTOFF_HOUR", "CUTOFF_MIN"),
       help_="No new entries after this; exits keep running (also used by the backtest)."),
    _s("SESSION_END_TIME", "Session end / square-off", "time", "Session Timings",
       parts=("SESSION_END_HOUR", "SESSION_END_MIN"), bt=False,
       help_="EOD square-off and daily reset."),

    # ── Risk & capital ────────────────────────────────────────────────────────
    _s("RISK_PER_TRADE", "Risk per trade ₹", "float", "Risk & Capital",
       min_=50, max_=100_000, step=50, help_="Qty = risk ÷ stop distance."),
    _s("ACCOUNT_BALANCE", "Account capital ₹", "float", "Risk & Capital",
       min_=1_000, max_=100_000_000, step=1000),
    _s("INTRADAY_LEVERAGE", "Intraday leverage ×", "int", "Risk & Capital",
       min_=1, max_=10),
    _s("MAX_CONCURRENT_POSITIONS", "Max open positions", "int", "Risk & Capital",
       min_=1, max_=20),
    _s("DAILY_LOSS_LIMIT", "Daily loss limit ₹", "float", "Risk & Capital",
       min_=100, max_=10_000_000, step=100, help_="No new entries once daily P&L breaches −limit."),

    # ── Strategy parameters ───────────────────────────────────────────────────
    _s("ADX_PERIOD", "ADX period", "int", "Strategy", min_=5, max_=50),
    _s("ADX_THRESHOLD", "ADX threshold", "float", "Strategy", min_=5, max_=50, step=0.5),
    _s("RSI_PERIOD", "RSI period", "int", "Strategy", min_=5, max_=50),
    _s("RSI_OVERSOLD", "RSI floor", "int", "Strategy", min_=10, max_=50,
       help_="rsi_ok passes when RSI > floor OR RSI rose N bars."),
    _s("RSI_RISING_BARS", "RSI rising bars", "int", "Strategy", min_=1, max_=10),
    _s("SWING_LOW_BARS", "Support lookback bars", "int", "Strategy", min_=3, max_=50),
    _s("SUPPORT_TOUCH_PCT", "Support proximity (fraction)", "float", "Strategy",
       min_=0.001, max_=0.10, step=0.001, help_="0.015 = within 1.5% above the swing low."),
    _s("MIN_SL_OFFSET", "Min stop distance ₹", "float", "Strategy", min_=0.5, max_=100, step=0.5),
    _s("VOLUME_MA_PERIOD", "Volume MA period", "int", "Strategy", min_=5, max_=100),
    _s("VOLUME_MULTIPLIER", "Volume surge ×", "float", "Strategy", min_=1.0, max_=10, step=0.1),
    _s("RR_RATIO", "Reward : risk ratio", "float", "Strategy", min_=0.5, max_=10, step=0.1),
    _s("MACD_CROSS_BARS", "MACD cross window (bars)", "int", "Strategy", min_=1, max_=10),
    _s("DEPTH_MIN_RATIO", "Min order-book buy ratio", "float", "Strategy",
       min_=0.0, max_=1.0, step=0.05, help_="Live only — backtests have no order book."),
    _s("TALIB_LOOKBACK", "Indicator lookback bars", "int", "Strategy", min_=60, max_=290,
       help_="Tail fed to TA-Lib; must stay under the 300-bar candle buffer."),

    # ── Entry-condition toggles ───────────────────────────────────────────────
    _s("COND_NEAR_SUPPORT", "Near support", "bool", "Entry Conditions"),
    _s("COND_BULLISH_PATTERN", "Bullish candle pattern", "bool", "Entry Conditions"),
    _s("COND_ADX", "ADX trend strength", "bool", "Entry Conditions"),
    _s("COND_RSI", "RSI ok", "bool", "Entry Conditions"),
    _s("COND_MACD_CROSS", "MACD bullish cross", "bool", "Entry Conditions"),
    _s("COND_VOLUME_SURGE", "Volume surge", "bool", "Entry Conditions"),
    _s("COND_ABOVE_VWAP", "Price above VWAP", "bool", "Entry Conditions"),
    _s("COND_DEPTH", "Order-book depth bullish", "bool", "Entry Conditions",
       help_="Live only — the backtest always passes this."),

    # ── Trend-gate toggles ────────────────────────────────────────────────────
    _s("GATE_STOCK_DAILY", "Stock daily green", "bool", "Trend Gates"),
    _s("GATE_STOCK_HOURLY", "Stock hourly green", "bool", "Trend Gates"),
    _s("GATE_NIFTY_DAILY", "NIFTY daily green", "bool", "Trend Gates"),
    _s("GATE_NIFTY_VWAP", "NIFTY above VWAP", "bool", "Trend Gates"),

    # ── Engine (live only) ───────────────────────────────────────────────────
    _s("TICK_EVAL_INTERVAL_MS", "Tick evaluation interval ms", "int", "Engine",
       min_=0, max_=5000, bt=False, help_="0 = run as fast as the loop allows."),
    _s("FULL_SCAN_INTERVAL_S", "Full-watchlist scan interval s", "int", "Engine",
       min_=30, max_=3600, bt=False),

    # ── Backtest & costs ──────────────────────────────────────────────────────
    _s("BACKTEST_TIMEFRAME", "Backtest timeframe", "choice", "Backtest & Costs",
       choices=cfg.TIMEFRAMES, help_="Bar interval a backtest replays (per-run overridable on the form)."),
    _s("BACKTEST_WARMUP_DAYS", "Backtest warmup days", "int", "Backtest & Costs",
       min_=3, max_=30),
    _s("SLIPPAGE_BPS", "Slippage (bps)", "float", "Backtest & Costs", min_=0, max_=100, step=0.5),
    _s("COST_BROKERAGE_PCT", "Brokerage % (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.0001),
    _s("COST_BROKERAGE_CAP", "Brokerage cap ₹/order", "float", "Backtest & Costs",
       min_=0, max_=100),
    _s("COST_STT_SELL", "STT sell-side (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00005),
    _s("COST_TXN_CHARGE", "Exchange txn (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00001),
    _s("COST_GST", "GST (fraction)", "float", "Backtest & Costs", min_=0, max_=1, step=0.01),
    _s("COST_STAMP_BUY", "Stamp duty buy-side (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00001),
    _s("COST_SEBI", "SEBI fee (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.001, step=0.000001),
]

_BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SPEC}
GROUP_ORDER = ["AI Pre-market Screen", "Session Timings", "Risk & Capital",
               "Strategy", "Entry Conditions", "Trend Gates", "Engine",
               "Backtest & Costs"]

# cfg-attr key → (spec, role) where role is "value" | "hour" | "min" — lets the
# loader validate raw stored attrs (incl. expanded time parts) one by one.
_ATTR_SPEC: Dict[str, tuple] = {}
for _spec in SPEC:
    if _spec["type"] == "time":
        _ATTR_SPEC[_spec["parts"][0]] = (_spec, "hour")
        _ATTR_SPEC[_spec["parts"][1]] = (_spec, "min")
    else:
        _ATTR_SPEC[_spec["key"]] = (_spec, "value")

# Import-time consistency check: every SPEC entry must map to a real dynamic
# config default and every default must be editable — catches the "added a
# tunable in only one place" drift at startup instead of as a silent bug.
_defaults_keys = set(cfg.dynamic_defaults())
_spec_attr_keys = set(_ATTR_SPEC)
if _spec_attr_keys != _defaults_keys:
    raise RuntimeError(
        "settings SPEC / config._DEFAULTS drift — "
        f"missing from SPEC: {sorted(_defaults_keys - _spec_attr_keys)}, "
        f"unknown in SPEC: {sorted(_spec_attr_keys - _defaults_keys)}"
    )

# Session times must stay ordered or the phase driver / backtest window breaks.
_TIME_ORDER = ("PREMARKET", "MARKET_OPEN", "SCAN_START", "CUTOFF", "SESSION_END")
_TIME_LABEL = {"PREMARKET": "pre-market", "MARKET_OPEN": "market open",
               "SCAN_START": "scan start", "CUTOFF": "entry cutoff",
               "SESSION_END": "session end"}


# ── Value coercion / validation ───────────────────────────────────────────────

def _coerce(spec: Dict[str, Any], raw: Any) -> Any:
    key, typ = spec["key"], spec["type"]
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        if raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise ValueError(f"{key}: expected true/false")

    if typ == "str":
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{key}: expected a non-empty string")
        return raw.strip()

    if typ == "choice":
        val = raw.strip() if isinstance(raw, str) else raw
        if val not in (spec["choices"] or []):
            raise ValueError(f"{key}: must be one of {spec['choices']}")
        return val

    if typ == "time":
        if not isinstance(raw, str) or not _TIME_RE.match(raw.strip()):
            raise ValueError(f"{key}: expected \"HH:MM\" (24h)")
        h, m = raw.strip().split(":")
        return int(h), int(m)

    # int / float
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: expected a number") from None
    if typ == "int":
        if val != int(val):
            raise ValueError(f"{key}: expected an integer")
        val = int(val)
    if spec["min"] is not None and val < spec["min"]:
        raise ValueError(f"{key}: must be ≥ {spec['min']}")
    if spec["max"] is not None and val > spec["max"]:
        raise ValueError(f"{key}: must be ≤ {spec['max']}")
    return val


def expand_changes(changes: Dict[str, Any], *, bt_only: bool = False) -> Dict[str, Any]:
    """
    Validate {spec_key: value} user input and return {cfg_attr: value},
    expanding virtual time settings into their HOUR/MIN pairs.
    Raises ValueError with a user-facing message on any bad key/value.
    """
    out: Dict[str, Any] = {}
    for key, raw in changes.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"unknown setting: {key}")
        if bt_only and not spec["bt"]:
            raise ValueError(f"{key} cannot be overridden per backtest run")
        val = _coerce(spec, raw)
        if spec["type"] == "time":
            out[spec["parts"][0]], out[spec["parts"][1]] = val
        else:
            out[key] = val
    return out


def _coerce_attr(key: str, raw: Any) -> Any:
    """
    Validate one raw cfg-attr value (as stored in the DB) against its SPEC.
    Time settings are stored expanded as *_HOUR/*_MIN ints, so they are
    validated through _coerce with a synthetic int spec (one validation path,
    consistent error messages) instead of the "HH:MM" string coercion.
    """
    spec, role = _ATTR_SPEC[key]
    if role == "value":
        return _coerce(spec, raw)
    hi = 23 if role == "hour" else 59
    return _coerce({"key": key, "type": "int", "min": 0, "max": hi}, raw)


def validate_time_order(attr_changes: Dict[str, Any],
                        points: tuple = _TIME_ORDER) -> None:
    """
    Cross-field guard: with `attr_changes` applied on top of the current
    config, the session times in `points` (order matters) must be ordered —
    the full live chain enforces
        premarket ≤ market open ≤ scan start < cutoff ≤ session end,
    while the backtest passes points=("SCAN_START","CUTOFF") since those are
    the only times a replay uses (comparing against live-only settings would
    falsely reject valid runs). No-op when attr_changes touches none of the
    points. Raises ValueError naming the violated pair.
    """
    if not any(k in attr_changes for p in points
               for k in (f"{p}_HOUR", f"{p}_MIN")):
        return

    def eff(attr: str) -> int:
        return attr_changes.get(attr, getattr(cfg, attr))

    minutes = [eff(f"{p}_HOUR") * 60 + eff(f"{p}_MIN") for p in points]
    for i in range(len(points) - 1):
        strict = points[i] == "SCAN_START"   # zero-width scan window is useless
        if minutes[i] > minutes[i + 1] or (strict and minutes[i] == minutes[i + 1]):
            raise ValueError(
                f"session times out of order: {_TIME_LABEL[points[i]]} must be "
                f"{'before' if strict else 'at or before'} {_TIME_LABEL[points[i + 1]]}"
            )


def _attr_keys(spec: Dict[str, Any]) -> List[str]:
    return list(spec["parts"]) if spec["type"] == "time" else [spec["key"]]


def _read_value(spec: Dict[str, Any], source: Dict[str, Any]) -> Any:
    if spec["type"] == "time":
        h, m = spec["parts"]
        return f"{source[h]:02d}:{source[m]:02d}"
    return source[spec["key"]]


# ── Introspection for GET /api/settings ───────────────────────────────────────

def describe() -> Dict[str, Any]:
    defaults = cfg.dynamic_defaults()
    current = {k: getattr(cfg, k) for k in defaults}
    groups: Dict[str, list] = {g: [] for g in GROUP_ORDER}
    for spec in SPEC:
        value   = _read_value(spec, current)
        default = _read_value(spec, defaults)
        groups.setdefault(spec["group"], []).append({
            "key":        spec["key"],
            "label":      spec["label"],
            "type":       spec["type"],
            "help":       spec["help"],
            "min":        spec["min"],
            "max":        spec["max"],
            "step":       spec["step"],
            "choices":    spec["choices"],
            "bt":         spec["bt"],
            "value":      value,
            "default":    default,
            "overridden": value != default,
        })
    return {"groups": [{"name": g, "settings": groups[g]}
                       for g in GROUP_ORDER if groups.get(g)]}


# ── Persistence glue ──────────────────────────────────────────────────────────

async def load_and_apply(db) -> None:
    """
    Startup: apply stored overrides from the app_settings table. Every value
    is re-validated against SPEC — a corrupt/out-of-range row (manual edit,
    schema drift) is skipped with a warning instead of poisoning the engine
    (e.g. PREMARKET_HOUR=99 would crash the phase driver's time math).
    """
    try:
        stored = await db.get_app_settings()
    except Exception as e:
        print(f"Settings load failed (using defaults): {e}")
        return
    valid: Dict[str, Any] = {}
    for k, v in stored.items():
        if k.startswith(INTERNAL_PREFIX) or k not in _ATTR_SPEC:
            continue
        try:
            valid[k] = _coerce_attr(k, v)
        except (ValueError, TypeError) as e:
            print(f"Settings: ignoring invalid stored override {k}={v!r} ({e})")

    # Cross-field self-heal: individually-valid rows can still form an
    # inverted session-time chain (partial manual edit / historical bug).
    # Fall back to the DEFAULT times rather than brick the trading day.
    try:
        validate_time_order(valid)
    except ValueError as e:
        time_attrs = [k for k in valid
                      if k.endswith("_HOUR") or k.endswith("_MIN")]
        for k in time_attrs:
            valid.pop(k, None)
        print(f"Settings: stored session times invalid ({e}) — "
              f"dropped {time_attrs}, using default timings")

    if valid:
        cfg.set_runtime_overrides(valid)
        print(f"Settings: applied {len(valid)} stored overrides")


async def apply_and_persist(db, changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate + apply {spec_key: value} changes, persist overrides, and drop
    stored rows for values set back to their default (so future default
    changes in code flow through). Returns the fresh describe() payload.
    """
    attr_changes = expand_changes(changes)
    validate_time_order(attr_changes)

    defaults   = cfg.dynamic_defaults()
    store      = {k: v for k, v in attr_changes.items() if v != defaults[k]}
    at_default = [k for k, v in attr_changes.items() if v == defaults[k]]

    # Persist FIRST (atomically — upsert + delete in one transaction), then
    # apply. Whatever the outcome, live behavior matches what a restart
    # would restore: DB failure → nothing persisted, nothing applied.
    await db.replace_app_settings(store, at_default)
    cfg.set_runtime_overrides(store)
    cfg.clear_runtime_overrides(at_default)
    return describe()


async def reset(db, keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Reset the given spec keys (or ALL settings) to defaults."""
    if keys is None:
        attr_keys = [k for s in SPEC for k in _attr_keys(s)]
    else:
        attr_keys = []
        for key in keys:
            spec = _BY_KEY.get(key)
            if spec is None:
                raise ValueError(f"unknown setting: {key}")
            attr_keys.extend(_attr_keys(spec))

    # A PARTIAL reset of a session time must honor the same ordering guard as
    # a save: resetting only CUTOFF back to 14:30 while SCAN_START is
    # overridden to 14:45 would invert the window and block all entries.
    # (A full reset is always valid — defaults are ordered.)
    defaults = cfg.dynamic_defaults()
    validate_time_order({k: defaults[k] for k in attr_keys
                         if k.endswith("_HOUR") or k.endswith("_MIN")})

    await db.delete_app_settings(attr_keys)
    cfg.clear_runtime_overrides(attr_keys)
    return describe()
