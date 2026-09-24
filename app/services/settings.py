from __future__ import annotations

"""
Dynamic settings registry + persistence.

As of 2026-09-09 (explicit user decision, "remove all except threshold"),
most former tunables (strategy gates, risk, options pricing/costs, qty-surge
thresholds, engine tick interval) became plain static attributes in
app.config, no longer editable from Settings or per backtest run. BN/NF
Alerts (client-side price-move notification thresholds) remained the only
survivors — until 2026-09-23 (explicit user decision, "time management" —
see the "Scalp Timing" group below), which made the two trading windows and
the per-instrument scalp time-stop dynamic again. The "time" coercion
machinery, previously unused generic infrastructure, is now actually
exercised by that group's 4 virtual "HH:MM" keys.

SPEC declares every runtime-editable tunable: display metadata, type, bounds,
and whether it may be overridden per-backtest-run ("bt"). Values themselves
live in app.config (defaults + runtime overrides); this module validates user
input and persists overrides to the app_settings table so they survive
restarts.

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
    # ── Dashboard price-move alerts — client-side only (browser Notification
    # API), never read by the trading engine itself; see static/js/alerts.js.
    # Per leader stock (BN_PRICE_ALERT_ATTR/NF_PRICE_ALERT_ATTR wiring), in
    # raw POINTS — matches the Stock Candles table's own cell numbers
    # directly (an explicit user decision over %, which obscures that
    # relationship and was a recurring source of confusion). Split into
    # "BN Alerts"/"NF Alerts" (not one "Alerts" group) so the Settings
    # page's BankNifty/Nifty 50 instrument filter can tell them apart —
    # that filter keys off the "BN "/"NF " group-name prefix, same as every
    # other group.
    _s("BN_PRICE_ALERT_PTS_HDFC", "HDFC BANK move alert (pts)", "float", "BN Alerts",
       min_=0.1, max_=500, step=0.1, bt=False,
       help_="Browser notification when this leader's latest bar |close-open| move exceeds this many points."),
    _s("BN_PRICE_ALERT_PTS_ICICI", "ICICI BANK move alert (pts)", "float", "BN Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("BN_PRICE_ALERT_PTS_SBI", "STATE BANK OF INDIA move alert (pts)", "float", "BN Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("BN_PRICE_ALERT_PTS_AXIS", "AXIS BANK move alert (pts)", "float", "BN Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("BN_PRICE_ALERT_PTS_KOTAK", "KOTAK BANK move alert (pts)", "float", "BN Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("BN_PRICE_ALERT_PTS_INDUSIND", "INDUSIND BANK move alert (pts)", "float", "BN Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("BN_ALERT_CONSENSUS_REQUIRED", "Leaders required for consensus alert", "int", "BN Alerts",
       min_=1, max_=6, bt=False,
       help_="Separate alert when at least this many of the 6 leaders cross their own move-alert threshold in the SAME direction."),

    _s("NF_PRICE_ALERT_PTS_HDFC", "HDFC BANK move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False,
       help_="Browser notification when this leader's latest bar |close-open| move exceeds this many points."),
    _s("NF_PRICE_ALERT_PTS_RELIANCE", "RELIANCE INDUSTRIES move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_ICICI", "ICICI BANK move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_INFY", "INFOSYS move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_BHARTIARTL", "BHARTI AIRTEL move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_ITC", "ITC move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_HCLTECH", "HCL TECHNOLOGIES move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_LT", "LARSEN & TOUBRO move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_KOTAK", "KOTAK BANK move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_AXIS", "AXIS BANK move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_SBI", "STATE BANK OF INDIA move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_PRICE_ALERT_PTS_HUL", "HINDUSTAN UNILEVER move alert (pts)", "float", "NF Alerts",
       min_=0.1, max_=500, step=0.1, bt=False),
    _s("NF_ALERT_CONSENSUS_REQUIRED", "Leaders required for consensus alert", "int", "NF Alerts",
       min_=1, max_=12, bt=False,
       help_="Separate alert when at least this many of the 12 leaders cross their own move-alert threshold in the SAME direction."),

    # ── Scalp timing (2026-09-23, explicit user decision) — no "BN "/"NF "
    # prefix on this group's name, so it always shows regardless of the
    # Settings page's instrument filter (see settings.js's applyFilter —
    # only a prefixed group name is ever hidden by that filter). Windows are
    # genuinely shared by both instruments; the two time-stop entries below
    # are per-instrument but small enough to keep in the same group rather
    # than splitting it in two for just one setting each.
    _s("SCALP_WINDOW1_START", "Trading window 1 — start", "time", "Scalp Timing",
       parts=("SCALP_WINDOW1_START_HOUR", "SCALP_WINDOW1_START_MIN"),
       help_="No new entries (either instrument) before this time. Checked live every tick."),
    _s("SCALP_WINDOW1_END", "Trading window 1 — end", "time", "Scalp Timing",
       parts=("SCALP_WINDOW1_END_HOUR", "SCALP_WINDOW1_END_MIN"),
       help_="No new entries after this time until window 2 opens."),
    _s("SCALP_WINDOW2_START", "Trading window 2 — start", "time", "Scalp Timing",
       parts=("SCALP_WINDOW2_START_HOUR", "SCALP_WINDOW2_START_MIN")),
    _s("SCALP_WINDOW2_END", "Trading window 2 — end", "time", "Scalp Timing",
       parts=("SCALP_WINDOW2_END_HOUR", "SCALP_WINDOW2_END_MIN"),
       help_="No new entries after this time for the rest of the day."),
    # bt=False (2026-09-24, found in review): app/backtest/engine.py's
    # _try_exit never reads pos.time_stop_s — per CLAUDE.md's documented
    # backtest-fidelity limitation, backtest has no sub-5-minute data, so it
    # always resolves on the entry bar's immediate next bar (open/high/low
    # touch, else a TIME_SCRATCH at that bar's close) regardless of this
    # value. Offering it as a per-run bt override silently misled whoever
    # used it into thinking a longer/shorter time-stop would change the
    # simulated outcome — it never did. Still a real, dynamic LIVE-only
    # tunable; only the backtest per-run override is disabled.
    _s("BN_SCALP_TIME_STOP_S", "BankNifty time-stop (s)", "float", "Scalp Timing",
       min_=1, max_=60, step=0.5, bt=False,
       help_="Force a scratch exit if neither target nor stop is touched within this many seconds of entry. Frozen at entry — a live edit never affects an already-open trade. Live-only: has no effect on backtest, which always resolves on the next 5m bar regardless of this value (no sub-5-minute historical data exists to simulate the real time-stop)."),
    _s("NF_SCALP_TIME_STOP_S", "Nifty 50 time-stop (s)", "float", "Scalp Timing",
       min_=1, max_=60, step=0.5, bt=False,
       help_="Force a scratch exit if neither target nor stop is touched within this many seconds of entry. Frozen at entry — a live edit never affects an already-open trade. Live-only: has no effect on backtest, which always resolves on the next 5m bar regardless of this value (no sub-5-minute historical data exists to simulate the real time-stop)."),

    # ── Execution simulation (2026-09-24, explicit user decision) — see
    # config.py's own comment on these same keys in _DEFAULTS. No "BN "/"NF "
    # prefix on the group name (mixed per-instrument settings, same
    # convention as "Scalp Timing" above), so it always shows regardless of
    # the Settings page's instrument filter. All bt=False: backtest has no
    # tick stream to simulate a delayed/slipped fill against, same fidelity
    # gap as BN_SCALP_TIME_STOP_S/NF_SCALP_TIME_STOP_S above.
    _s("BN_ENTRY_FILL_DELAY_MS", "BankNifty entry fill delay (ms)", "float", "Execution Delay",
       min_=0, max_=5000, step=10, bt=False,
       help_="Simulated order-placement lag: an algo entry signal waits this long, then fills at the next live tick's price (not the signal's own price) — realistic slippage. Does not apply to a manual order. Live-only."),
    _s("NF_ENTRY_FILL_DELAY_MS", "Nifty 50 entry fill delay (ms)", "float", "Execution Delay",
       min_=0, max_=5000, step=10, bt=False,
       help_="Simulated order-placement lag: an algo entry signal waits this long, then fills at the next live tick's price (not the signal's own price) — realistic slippage. Does not apply to a manual order. Live-only."),
    _s("BN_EXIT_FILL_DELAY_MS", "BankNifty exit fill delay (ms)", "float", "Execution Delay",
       min_=0, max_=5000, step=10, bt=False,
       help_="Simulated close-order lag: an automatic target/stop/time-scratch exit waits this long, then fills at the next live tick's price — realistic slippage, symmetric with the entry delay above. Does not apply to a manual Exit click or the 15:30 EOD square-off, both immediate. Live-only."),
    _s("NF_EXIT_FILL_DELAY_MS", "Nifty 50 exit fill delay (ms)", "float", "Execution Delay",
       min_=0, max_=5000, step=10, bt=False,
       help_="Simulated close-order lag: an automatic target/stop/time-scratch exit waits this long, then fills at the next live tick's price — realistic slippage, symmetric with the entry delay above. Does not apply to a manual Exit click or the 15:30 EOD square-off, both immediate. Live-only."),
    _s("BN_FILL_MAX_WAIT_MS", "BankNifty fill max wait (ms)", "float", "Execution Delay",
       min_=0, max_=20000, step=100, bt=False,
       help_="If no genuinely new live tick arrives this long after a delayed entry/exit's fill time elapses, fill at whatever price is current instead of waiting indefinitely (feed momentarily idle)."),
    _s("NF_FILL_MAX_WAIT_MS", "Nifty 50 fill max wait (ms)", "float", "Execution Delay",
       min_=0, max_=20000, step=100, bt=False,
       help_="If no genuinely new live tick arrives this long after a delayed entry/exit's fill time elapses, fill at whatever price is current instead of waiting indefinitely (feed momentarily idle)."),
]

_BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SPEC}
GROUP_ORDER = ["Scalp Timing", "Execution Delay", "BN Alerts", "NF Alerts"]

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


def _attr_keys(spec: Dict[str, Any]) -> List[str]:
    return list(spec["parts"]) if spec["type"] == "time" else [spec["key"]]


# ── Cross-field validation ────────────────────────────────────────────────────
# Trading-window start/end are two independent virtual SPEC keys (each
# validated individually as a plain "HH:MM" by _coerce), so an inverted pair
# (start >= end) would pass per-key validation yet silently make
# _in_trading_window(now) always False for that window for the rest of the
# day — checked wherever a change to either window could take effect: on
# save, on partial reset, and at startup load (self-heals instead of crash-
# looping, same convention this repo used for the now-removed
# validate_time_order/validate_bn_indicator_periods before 2026-09-09).
_WINDOWS = [
    ("SCALP_WINDOW1_START_HOUR", "SCALP_WINDOW1_START_MIN",
     "SCALP_WINDOW1_END_HOUR",   "SCALP_WINDOW1_END_MIN",   "Trading window 1"),
    ("SCALP_WINDOW2_START_HOUR", "SCALP_WINDOW2_START_MIN",
     "SCALP_WINDOW2_END_HOUR",   "SCALP_WINDOW2_END_MIN",   "Trading window 2"),
]
_SCALP_TIMING_ATTRS = [k for s in SPEC if s["group"] == "Scalp Timing" for k in _attr_keys(s)]


def validate_scalp_windows(effective: Dict[str, Any]) -> None:
    """`effective` must have all 8 SCALP_WINDOW*_HOUR/MIN attrs (already-coerced ints)."""
    for sh, sm, eh, em, label in _WINDOWS:
        if effective[sh] * 60 + effective[sm] >= effective[eh] * 60 + effective[em]:
            raise ValueError(f"{label}: start time must be before end time")


def effective_state(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """
    Current live value of every dynamic key, with `overrides` layered on
    top — the merged state any cross-field validator (currently just
    validate_scalp_windows) should check against. One shared builder for
    load_and_apply/apply_and_persist/reset below (2026-09-23, found in
    review — each used to hand-roll this same merge slightly differently).
    At startup (load_and_apply, before any override is applied yet) current
    cfg values already equal the defaults, so this is equivalent to merging
    onto defaults there too — no special-casing needed.
    """
    return {**{k: getattr(cfg, k) for k in cfg.dynamic_defaults()}, **overrides}


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

    if valid:
        try:
            validate_scalp_windows(effective_state(valid))
        except ValueError as e:
            print(f"Settings: stored Scalp Timing overrides invalid ({e}) — reverting that group to defaults")
            for k in _SCALP_TIMING_ATTRS:
                valid.pop(k, None)
        cfg.set_runtime_overrides(valid)
        print(f"Settings: applied {len(valid)} stored overrides")


async def apply_and_persist(db, changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate + apply {spec_key: value} changes, persist overrides, and drop
    stored rows for values set back to their default (so future default
    changes in code flow through). Returns the fresh describe() payload.
    """
    attr_changes = expand_changes(changes)
    validate_scalp_windows(effective_state(attr_changes))

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

    # A partial reset can only ever move a key TOWARD its (sane) default, but
    # a window attr NOT included in this reset could still be left overridden
    # in a now-invalid combination with the other window attr's fresh
    # default — check the resulting effective state before committing.
    defaults = cfg.dynamic_defaults()
    validate_scalp_windows(effective_state({k: defaults[k] for k in attr_keys}))

    await db.delete_app_settings(attr_keys)
    cfg.clear_runtime_overrides(attr_keys)
    return describe()
