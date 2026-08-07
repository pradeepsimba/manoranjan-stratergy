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

# Keys in app_settings that are NOT config overrides (day/persistent internal
# state). Prefixed with "_" and skipped by the settings loader.
INTERNAL_PREFIX = "_"
BN_FUNDS_KEY = "_BN_FUNDS"   # persisted running paper-account balance


def _s(key: str, label: str, type_: str, group: str, *,
       min_: Optional[float] = None, max_: Optional[float] = None,
       step: Optional[float] = None, help_: str = "", bt: bool = True,
       parts: Optional[tuple] = None,
       choices: Optional[list] = None,
       cond: Optional[str] = None) -> Dict[str, Any]:
    return {"key": key, "label": label, "type": type_, "group": group,
            "min": min_, "max": max_, "step": step, "help": help_,
            "bt": bt, "parts": parts, "choices": choices, "cond": cond}


SPEC: List[Dict[str, Any]] = [
    # ── Session timings ───────────────────────────────────────────────────────
    _s("PREMARKET_TIME", "Pre-market", "time", "Session Timings",
       parts=("PREMARKET_HOUR", "PREMARKET_MIN"), bt=False,
       help_="No-op placeholder in the BN engine (fixed instrument universe) — kept for phase-driver timing."),
    _s("MARKET_OPEN_TIME", "Market open / data load", "time", "Session Timings",
       parts=("MARKET_OPEN_HOUR", "MARKET_OPEN_MIN"), bt=False,
       help_="Historical load + WebSocket subscribe."),
    _s("SCAN_START_TIME", "Entry scanning starts", "time", "Session Timings",
       parts=("SCAN_START_HOUR", "SCAN_START_MIN"),
       help_="No entries before this time (also used by the backtest)."),
    _s("CUTOFF_TIME", "Entry cutoff", "time", "Session Timings",
       parts=("CUTOFF_HOUR", "CUTOFF_MIN"),
       help_="No new entries after this; exit management keeps running (also used by the backtest)."),
    _s("SESSION_END_TIME", "Session end / square-off", "time", "Session Timings",
       parts=("SESSION_END_HOUR", "SESSION_END_MIN"), bt=False,
       help_="EOD square-off and daily reset."),

    # ── BN Strategy — gates ───────────────────────────────────────────────────
    _s("BN_SIDEWAYS_RANGE_MIN", "Min 5-bar range (pts)", "float", "BN Strategy",
       min_=1, max_=200, step=0.5, help_="Block entries when BankNifty's last 5 closes span less than this."),
    _s("BN_MOMENTUM_THRESHOLD", "Momentum threshold (pts)", "float", "BN Strategy",
       min_=1, max_=200, step=0.5, help_="Fixed 5m move threshold; ATR can only lower it."),
    _s("BN_ATR_PERIOD", "ATR period (bars)", "int", "BN Strategy", min_=3, max_=50),
    _s("BN_SAME_DIRECTION_REQUIRED", "Leaders required to agree", "int", "BN Strategy",
       min_=1, max_=6, help_="Of the 6 leader stocks."),
    _s("BN_ENTRY_COOLDOWN_S", "Post-exit cooldown (s)", "int", "BN Strategy", min_=0, max_=600),

    # ── BN Qty Surge — per-stock bar-volume-surge thresholds (compared
    # against each leader's latest 5m bar volume — the same figure shown in
    # the Entry Loop Monitor's VOLUME column / Big Trades panel).
    _s("BN_QTY_THRESHOLD_HDFC", "HDFC BANK volume threshold", "float", "BN Qty Surge",
       min_=100, max_=1_000_000, step=100, help_="Bar volume (pre-multiplier) that counts as a surge."),
    _s("BN_QTY_THRESHOLD_ICICI", "ICICI BANK volume threshold", "float", "BN Qty Surge",
       min_=100, max_=1_000_000, step=100),
    _s("BN_QTY_THRESHOLD_SBI", "STATE BANK OF INDIA volume threshold", "float", "BN Qty Surge",
       min_=100, max_=1_000_000, step=100),
    _s("BN_QTY_THRESHOLD_AXIS", "AXIS BANK volume threshold", "float", "BN Qty Surge",
       min_=100, max_=1_000_000, step=100),
    _s("BN_QTY_THRESHOLD_KOTAK", "KOTAK BANK volume threshold", "float", "BN Qty Surge",
       min_=100, max_=1_000_000, step=100),
    _s("BN_QTY_THRESHOLD_INDUSIND", "INDUSIND BANK volume threshold", "float", "BN Qty Surge",
       min_=100, max_=1_000_000, step=100),
    _s("BN_QTY_INTERVAL_MULTIPLIER", "Volume threshold multiplier", "float", "BN Qty Surge",
       min_=0.1, max_=20, step=0.1, help_="Applied on top of each per-stock threshold above."),

    # ── BN Strategy — composite indicator gate ───────────────────────────────
    _s("BN_INDICATOR_LOOKBACK_BARS", "Indicator lookback bars", "int", "BN Strategy",
       min_=60, max_=290, help_="Tail fed to RSI/MACD/EMA; must stay under the 300-bar candle buffer."),
    _s("BN_RSI_PERIOD", "RSI period", "int", "BN Strategy", min_=5, max_=50),
    _s("BN_EMA_FAST", "EMA fast period", "int", "BN Strategy", min_=2, max_=100),
    _s("BN_EMA_SLOW", "EMA slow period", "int", "BN Strategy", min_=3, max_=200),
    _s("BN_MACD_FAST", "MACD fast period (EMA, no signal line)", "int", "BN Strategy", min_=2, max_=100),
    _s("BN_MACD_SLOW", "MACD slow period (EMA, no signal line)", "int", "BN Strategy", min_=3, max_=200),
    _s("BN_RSI_BULL_LEVEL", "RSI bullish level", "int", "BN Strategy", min_=50, max_=90),
    _s("BN_RSI_BEAR_LEVEL", "RSI bearish level", "int", "BN Strategy", min_=10, max_=50),
    _s("BN_RSI_OVERBOUGHT", "RSI overbought penalty level", "int", "BN Strategy", min_=50, max_=95),
    _s("BN_RSI_OVERSOLD", "RSI oversold bonus level", "int", "BN Strategy", min_=5, max_=50),
    _s("BN_EMA_EXTENSION_PCT", "EMA extension penalty (%)", "float", "BN Strategy", min_=0.1, max_=10, step=0.1),
    _s("BN_SCORE_MIN", "Min bull/bear score to fire", "float", "BN Strategy", min_=0.5, max_=10, step=0.1),
    _s("BN_SCORE_MARGIN", "Score margin over the other side", "float", "BN Strategy", min_=0, max_=5, step=0.1),

    # ── BN Risk ────────────────────────────────────────────────────────────────
    _s("BN_TARGET_POINTS", "Target (BankNifty pts)", "float", "BN Risk", min_=5, max_=500, step=1),
    _s("BN_STOPLOSS_POINTS", "Initial stop (BankNifty pts)", "float", "BN Risk", min_=5, max_=500, step=1),
    _s("BN_BREAKEVEN_TRIGGER", "Breakeven trigger (pts)", "float", "BN Risk", min_=1, max_=500, step=1),
    _s("BN_TRAIL_TRIGGER", "Trailing-stop trigger (pts)", "float", "BN Risk", min_=1, max_=500, step=1),
    _s("BN_TRAIL_DISTANCE", "Trailing-stop distance (pts)", "float", "BN Risk", min_=1, max_=500, step=1),
    _s("BN_STARTING_FUNDS", "Starting funds ₹", "float", "BN Risk", min_=1_000, max_=100_000_000, step=1000,
       help_="Only seeds the persisted balance the first time / on explicit reset."),

    # ── BN Options Pricing (synthetic Black-Scholes — no real option data) ──
    _s("BN_RISK_FREE_RATE", "Risk-free rate", "float", "BN Options Pricing", min_=0, max_=0.2, step=0.005),
    _s("BN_IV_MIN", "IV floor", "float", "BN Options Pricing", min_=0.05, max_=1.0, step=0.01),
    _s("BN_IV_MAX", "IV ceiling", "float", "BN Options Pricing", min_=0.05, max_=2.0, step=0.01),
    _s("BN_IV_DEFAULT", "IV default (insufficient data)", "float", "BN Options Pricing", min_=0.05, max_=2.0, step=0.01),
    _s("BN_IV_LOOKBACK_BARS", "IV lookback bars", "int", "BN Options Pricing", min_=5, max_=290),
    _s("BN_IV_MANUAL_ENABLED", "Manual IV override", "bool", "BN Options Pricing", bt=False),
    _s("BN_IV_MANUAL_VALUE", "Manual IV value", "float", "BN Options Pricing",
       min_=0.05, max_=2.0, step=0.01, cond="BN_IV_MANUAL_ENABLED", bt=False),

    # ── BN Options Costs (placeholder rates — confirm current India options
    # STT/exchange-txn figures before trusting absolute backtest ₹ P&L) ─────
    _s("BN_COST_BROKERAGE_FLAT", "Brokerage ₹/order (flat)", "float", "BN Options Costs", min_=0, max_=100),
    _s("BN_COST_STT_SELL_PCT", "STT sell-side (fraction)", "float", "BN Options Costs", min_=0, max_=0.01, step=0.0001),
    _s("BN_COST_TXN_PCT", "Exchange txn (fraction)", "float", "BN Options Costs", min_=0, max_=0.01, step=0.00001),
    _s("BN_COST_GST_PCT", "GST (fraction)", "float", "BN Options Costs", min_=0, max_=1, step=0.01),
    _s("BN_COST_SEBI_PCT", "SEBI fee (fraction)", "float", "BN Options Costs", min_=0, max_=0.001, step=0.000001),

    # ── Engine (live only) ───────────────────────────────────────────────────
    _s("TICK_EVAL_INTERVAL_MS", "Tick evaluation interval ms", "int", "Engine",
       min_=0, max_=5000, bt=False, help_="0 = run as fast as the loop allows."),

    # ── Backtest ───────────────────────────────────────────────────────────────
    _s("BACKTEST_WARMUP_DAYS", "Backtest warmup days", "int", "Backtest", min_=3, max_=30),
    _s("SLIPPAGE_BPS", "Slippage (bps)", "float", "Backtest", min_=0, max_=100, step=0.5,
       help_="Applied to the option premium fill."),
]

_BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SPEC}
GROUP_ORDER = ["Session Timings", "BN Strategy", "BN Qty Surge", "BN Risk", "BN Options Pricing",
               "BN Options Costs", "Engine", "Backtest"]

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


# cfg attrs whose value affects an indicator's minimum-bar requirement — the
# self-heal and reset guards drop/validate this whole set together.
BN_INDICATOR_PERIOD_KEYS = ("BN_MACD_FAST", "BN_MACD_SLOW", "BN_EMA_FAST",
                           "BN_EMA_SLOW", "BN_RSI_PERIOD", "BN_INDICATOR_LOOKBACK_BARS")


def validate_bn_indicator_periods(attr_changes: Dict[str, Any]) -> None:
    """
    Cross-field guards so a period/lookback combo can't leave an indicator
    all-NaN — which silently blocks the BN composite gate (never bullish/
    bearish) in both live and backtest, with no error. No-op unless a
    relevant key changed.
    """
    if not any(k in attr_changes for k in BN_INDICATOR_PERIOD_KEYS):
        return

    def eff(k: str) -> int:
        return attr_changes.get(k, getattr(cfg, k))

    fast, slow = eff("BN_MACD_FAST"), eff("BN_MACD_SLOW")
    if fast >= slow:
        raise ValueError(
            f"BN MACD fast period ({fast}) must be less than the slow period ({slow})")

    lookback = eff("BN_INDICATOR_LOOKBACK_BARS")
    need = max(eff("BN_EMA_SLOW"), slow, eff("BN_RSI_PERIOD")) + 1
    if lookback < need:
        raise ValueError(
            f"BN indicator lookback ({lookback}) is too small — needs "
            f"≥ {need} bars; raise BN_INDICATOR_LOOKBACK_BARS or lower the period(s)")


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
    the only times a replay uses. No-op when attr_changes touches none of the
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
        entry = {
            "key":        spec["key"],
            "label":      spec["label"],
            "type":       spec["type"],
            "help":       spec["help"],
            "min":        spec["min"],
            "max":        spec["max"],
            "step":       spec["step"],
            "choices":    spec["choices"],
            "cond":       spec["cond"],
            "bt":         spec["bt"],
            "value":      value,
            "default":    default,
            "overridden": value != default,
        }
        groups.setdefault(spec["group"], []).append(entry)
    return {"groups": [{"name": g, "settings": groups[g]}
                       for g in GROUP_ORDER if groups.get(g)]}


# ── Persistence glue ──────────────────────────────────────────────────────────

async def load_and_apply(db) -> None:
    """
    Startup: apply stored overrides from the app_settings table. Every value
    is re-validated against SPEC — a corrupt/out-of-range row (manual edit,
    schema drift) is skipped with a warning instead of poisoning the engine.
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

    # Same self-heal for a stored indicator period/lookback combo that would
    # leave the BN composite gate all-NaN. Drop ALL indicator-period
    # overrides back to defaults (which are internally consistent).
    try:
        validate_bn_indicator_periods(valid)
    except ValueError as e:
        dropped = [k for k in BN_INDICATOR_PERIOD_KEYS if k in valid]
        for k in dropped:
            valid.pop(k, None)
        print(f"Settings: stored indicator periods invalid ({e}) — "
              f"dropped {dropped}, using defaults")

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
    validate_bn_indicator_periods(attr_changes)

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

    # A PARTIAL reset must honor the same cross-field guards as a save.
    # (A full reset is always valid — defaults are internally consistent.)
    defaults = cfg.dynamic_defaults()
    post_reset = {k: defaults[k] for k in attr_keys}
    validate_time_order(post_reset)
    validate_bn_indicator_periods(post_reset)

    await db.delete_app_settings(attr_keys)
    cfg.clear_runtime_overrides(attr_keys)
    return describe()
