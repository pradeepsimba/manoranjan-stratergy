from __future__ import annotations

"""
Dynamic settings registry + persistence.

SPEC declares every runtime-editable tunable: display metadata, type, and
bounds. Values themselves live in app.config (defaults + runtime overrides);
this module validates user input, expands virtual "HH:MM" time settings into
their HOUR/MIN config pairs, and persists overrides to the app_settings
table so they survive restarts.

Add a new tunable by adding its default to app.config._DEFAULTS AND an entry
here — nothing else is required for it to appear on the Settings page.
"""

import re
from typing import Any, Dict, List, Optional

import app.config as cfg

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Keys in app_settings that are NOT config overrides (reserved for future
# internal state) — prefixed with "_" and skipped by the settings loader.
INTERNAL_PREFIX = "_"


def _s(key: str, label: str, type_: str, group: str, *,
       min_: Optional[float] = None, max_: Optional[float] = None,
       step: Optional[float] = None, help_: str = "",
       parts: Optional[tuple] = None,
       choices: Optional[list] = None,
       cond: Optional[str] = None) -> Dict[str, Any]:
    return {"key": key, "label": label, "type": type_, "group": group,
            "min": min_, "max": max_, "step": step, "help": help_,
            "parts": parts, "choices": choices, "cond": cond}


SPEC: List[Dict[str, Any]] = [
    # ── Session timings ───────────────────────────────────────────────────────
    _s("MARKET_OPEN_TIME", "Market open", "time", "Session Timings",
       parts=("MARKET_OPEN_HOUR", "MARKET_OPEN_MIN"),
       help_="Historical load + live feed subscribe + orders accepted from here."),
    _s("MIS_SQUAREOFF_TIME", "Intraday (MIS) square-off", "time", "Session Timings",
       parts=("MIS_SQUAREOFF_HOUR", "MIS_SQUAREOFF_MIN"),
       help_="All open intraday positions are auto-closed at this time."),
    _s("MARKET_CLOSE_TIME", "Market close", "time", "Session Timings",
       parts=("MARKET_CLOSE_HOUR", "MARKET_CLOSE_MIN"),
       help_="Live feed stops for the day."),

    # ── Accounts ───────────────────────────────────────────────────────────────
    _s("STARTING_FUNDS", "Starting funds ₹", "float", "Accounts",
       min_=1_000, max_=100_000_000, step=1000,
       help_="Seeded onto a new user's account at registration."),

    # ── Engine ─────────────────────────────────────────────────────────────────
    _s("TICK_EVAL_INTERVAL_MS", "Tick evaluation interval ms", "int", "Engine",
       min_=10, max_=5000, help_="How often resting limit orders are checked against live prices."),
]

_BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SPEC}
GROUP_ORDER = ["Session Timings", "Accounts", "Engine"]

# cfg-attr key -> (spec, role) where role is "value" | "hour" | "min" — lets the
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

# Session times must stay ordered or the phase driver breaks.
_TIME_ORDER = ("MARKET_OPEN", "MIS_SQUAREOFF", "MARKET_CLOSE")
_TIME_LABEL = {"MARKET_OPEN": "market open", "MIS_SQUAREOFF": "MIS square-off",
               "MARKET_CLOSE": "market close"}


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
        raise ValueError(f"{key}: must be >= {spec['min']}")
    if spec["max"] is not None and val > spec["max"]:
        raise ValueError(f"{key}: must be <= {spec['max']}")
    return val


def expand_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
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
    config, the session times in `points` (order matters) must be ordered:
        market open <= MIS square-off <= market close.
    No-op when attr_changes touches none of the points. Raises ValueError
    naming the violated pair.
    """
    if not any(k in attr_changes for p in points
               for k in (f"{p}_HOUR", f"{p}_MIN")):
        return

    def eff(attr: str) -> int:
        return attr_changes.get(attr, getattr(cfg, attr))

    minutes = [eff(f"{p}_HOUR") * 60 + eff(f"{p}_MIN") for p in points]
    for i in range(len(points) - 1):
        if minutes[i] > minutes[i + 1]:
            raise ValueError(
                f"session times out of order: {_TIME_LABEL[points[i]]} must be "
                f"at or before {_TIME_LABEL[points[i + 1]]}"
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
    # would restore: DB failure -> nothing persisted, nothing applied.
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

    await db.delete_app_settings(attr_keys)
    cfg.clear_runtime_overrides(attr_keys)
    return describe()
