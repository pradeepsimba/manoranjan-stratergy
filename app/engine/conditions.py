from __future__ import annotations

"""
Shared entry-condition evaluation — the single place where the 8 entry checks
and their runtime enable/disable toggles are defined. Used by BOTH the live
entry engine and the backtest so the two can never drift.

A disabled condition auto-passes: it is excluded from the failure list but
still reported in the checks dict for the dashboard.
"""

import threading
from typing import Dict, List, Optional

import app.config as cfg
from app.models import IndicatorResult

def _rsi_ok(ind, r) -> bool:
    """
    RSI entry rule, direction controlled by cfg.RSI_MODE against RSI_OVERSOLD:
      above_or_rising (default) — RSI > level OR rose for RSI_RISING_BARS
      above                     — RSI > level
      below                     — RSI < level (oversold-bounce entry)
    ind.rsi_above_30 already means "RSI > RSI_OVERSOLD" (name is historical).
    """
    mode = cfg.RSI_MODE
    if mode == "below":
        return ind.rsi is not None and ind.rsi < cfg.RSI_OVERSOLD
    if mode == "above":
        return ind.rsi_above_30
    return ind.rsi_above_30 or ind.rsi_rising


# The single source of truth: check key → (config toggle attr, evaluator).
# depth_ratio=None (no snap data yet / backtest) defaults depth to PASS —
# it only vetoes a clearly sell-skewed book.
_CONDITIONS: Dict[str, tuple] = {
    "near_support":    ("COND_NEAR_SUPPORT",
                        lambda ind, r: ind.near_support),
    "bullish_pattern": ("COND_BULLISH_PATTERN",
                        lambda ind, r: ind.bullish_pattern),
    "adx_ok":          ("COND_ADX",
                        lambda ind, r: ind.adx_ok),
    "rsi_ok":          ("COND_RSI", _rsi_ok),
    "macd_cross":      ("COND_MACD_CROSS",
                        lambda ind, r: ind.macd_bullish_cross),
    "volume_surge":    ("COND_VOLUME_SURGE",
                        lambda ind, r: ind.volume_surge),
    "above_vwap":      ("COND_ABOVE_VWAP",
                        lambda ind, r: ind.price_above_vwap),
    "depth_bullish":   ("COND_DEPTH",
                        lambda ind, r: (r >= cfg.DEPTH_MIN_RATIO) if r is not None else True),
}

CONDITION_TOGGLES: Dict[str, str] = {k: t for k, (t, _) in _CONDITIONS.items()}


# ── Custom entry rules (the "ultimate dynamic" layer) ─────────────────────────
# A rule set is OR-of-ANDs: groups are OR'd, clauses within a group are AND'd.
#   {"enabled": bool, "mode": "and"|"replace", "groups": [[clause, ...], ...]}
#   clause = {"field": <RULE_FIELDS key>, "op": <RULE_OPS key>, "value": num|bool
#             [, "value2": num]}         (value2 only for op "between")
# mode "and"     → rules are an EXTRA condition on top of the fixed 8.
# mode "replace" → rules REPLACE the fixed 8 (trend gates still apply).
# Numeric clause on missing data (None) fails — unknown data can't satisfy an
# assertion — EXCEPT depth_ratio, which keeps its documented None→pass semantics.

# field key → (label, kind, extractor(ind, depth_ratio))
RULE_FIELDS: Dict[str, tuple] = {
    "rsi":              ("RSI",                    "num",  lambda ind, r: ind.rsi),
    "adx":              ("ADX",                    "num",  lambda ind, r: ind.adx),
    "plus_di":          ("+DI",                    "num",  lambda ind, r: ind.plus_di),
    "minus_di":         ("−DI",                    "num",  lambda ind, r: ind.minus_di),
    "macd_hist":        ("MACD histogram",         "num",  lambda ind, r: ind.macd_histogram),
    "volume_ratio":     ("Volume ÷ average",       "num",  lambda ind, r: ind.volume_ratio),
    "ltp":              ("Price ₹",                "num",  lambda ind, r: ind.ltp or None),
    "vwap_dist_pct":    ("% above VWAP",           "num",
                         lambda ind, r: ((ind.ltp / ind.vwap) - 1.0) * 100.0
                                        if ind.vwap > 0 and ind.ltp > 0 else None),
    "support_dist_pct": ("% above support",        "num",
                         lambda ind, r: ((ind.ltp / ind.support_level) - 1.0) * 100.0
                                        if ind.support_level > 0 and ind.ltp > 0 else None),
    "depth_ratio":      ("Order-book buy ratio",   "num",  lambda ind, r: r),
    "near_support":     ("Near support",           "bool", lambda ind, r: ind.near_support),
    "bullish_pattern":  ("Bullish pattern",        "bool", lambda ind, r: ind.bullish_pattern),
    "macd_cross":       ("MACD bullish cross",     "bool", lambda ind, r: ind.macd_bullish_cross),
    "volume_surge":     ("Volume surge",           "bool", lambda ind, r: ind.volume_surge),
    "above_vwap":       ("Price above VWAP",       "bool", lambda ind, r: ind.price_above_vwap),
    "rsi_rising":       ("RSI rising",             "bool", lambda ind, r: ind.rsi_rising),
    "adx_trending":     ("ADX trend ok",           "bool", lambda ind, r: ind.adx_ok),
}

RULE_OPS = ("lt", "lte", "gt", "gte", "eq", "neq", "between")
_BOOL_OPS = ("eq", "neq")

MAX_RULE_GROUPS  = 8
MAX_RULE_CLAUSES = 8


def validate_rules(raw) -> Dict:
    """
    Structurally validate a rule-set value (settings 'rules' type). Returns a
    normalized copy or raises ValueError with a user-facing message. Whitelists
    live HERE so the validator and evaluator can't drift.
    """
    if not isinstance(raw, dict):
        raise ValueError("custom rules: expected an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("custom rules: 'enabled' must be true/false")
    mode = raw.get("mode", "and")
    if mode not in ("and", "replace"):
        raise ValueError("custom rules: 'mode' must be \"and\" or \"replace\"")
    groups = raw.get("groups", [])
    if not isinstance(groups, list) or len(groups) > MAX_RULE_GROUPS:
        raise ValueError(f"custom rules: 'groups' must be a list of ≤{MAX_RULE_GROUPS} groups")
    out_groups = []
    for gi, group in enumerate(groups, 1):
        if not isinstance(group, list) or not group or len(group) > MAX_RULE_CLAUSES:
            raise ValueError(f"custom rules: group {gi} must hold 1–{MAX_RULE_CLAUSES} conditions")
        out_clauses = []
        for ci, cl in enumerate(group, 1):
            where = f"group {gi} condition {ci}"
            if not isinstance(cl, dict):
                raise ValueError(f"custom rules: {where} must be an object")
            fld = cl.get("field")
            if fld not in RULE_FIELDS:
                raise ValueError(f"custom rules: {where}: unknown field {fld!r}")
            op = cl.get("op")
            if op not in RULE_OPS:
                raise ValueError(f"custom rules: {where}: unknown op {op!r}")
            kind = RULE_FIELDS[fld][1]
            val = cl.get("value")
            if kind == "bool":
                if op not in _BOOL_OPS or not isinstance(val, bool):
                    raise ValueError(f"custom rules: {where}: boolean field "
                                     f"'{fld}' needs op eq/neq and a true/false value")
                out_clauses.append({"field": fld, "op": op, "value": val})
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"custom rules: {where}: numeric value required")
            norm = {"field": fld, "op": op, "value": float(val)}
            if op == "between":
                v2 = cl.get("value2")
                if isinstance(v2, bool) or not isinstance(v2, (int, float)) or float(v2) <= float(val):
                    raise ValueError(f"custom rules: {where}: 'between' needs value2 > value")
                norm["value2"] = float(v2)
            out_clauses.append(norm)
        out_groups.append(out_clauses)
    if enabled and not out_groups:
        raise ValueError("custom rules: enable requires at least one group")
    return {"enabled": enabled, "mode": mode, "groups": out_groups}


def _clause_ok(cl: Dict, ind: IndicatorResult, depth_ratio: Optional[float]) -> bool:
    fld = cl["field"]
    kind, fn = RULE_FIELDS[fld][1], RULE_FIELDS[fld][2]
    v = fn(ind, depth_ratio)
    op, ref = cl["op"], cl["value"]
    if kind == "bool":
        v = bool(v)
        return (v == ref) if op == "eq" else (v != ref)
    if v is None:
        # Missing numeric data can't satisfy an assertion — except the
        # order-book ratio, which is live-only and documented as pass-when-absent.
        return fld == "depth_ratio"
    if op == "lt":  return v <  ref
    if op == "lte": return v <= ref
    if op == "gt":  return v >  ref
    if op == "gte": return v >= ref
    if op == "eq":  return v == ref
    if op == "neq": return v != ref
    return ref <= v <= cl["value2"]        # between (validated to exist)


def custom_rules_ok(ind: IndicatorResult,
                    depth_ratio: Optional[float] = None) -> bool:
    """OR over groups, AND within a group. Neutral (True) when disabled/empty."""
    rules = cfg.CUSTOM_ENTRY_RULES
    if not rules.get("enabled") or not rules.get("groups"):
        return True
    for group in rules["groups"]:
        if all(_clause_ok(cl, ind, depth_ratio) for cl in group):
            return True
    return False


def _rules_mode() -> Optional[str]:
    """'and' | 'replace' when custom rules are active, else None."""
    rules = cfg.CUSTOM_ENTRY_RULES
    return rules.get("mode", "and") if rules.get("enabled") and rules.get("groups") else None


# ── Resolved-plan cache ────────────────────────────────────────────────────────
# entry_ok / cheap_gates_veto run once per gated symbol-bar — tens of millions
# of times in a long backtest — and each toggle read goes through config's
# module __getattr__ (~20× a plain attribute). Resolve the ENABLED evaluator
# tuples once per cfg.resolution_token() (bumps on any Settings apply/reset or
# thread-override scope change) and reuse them. Thread-local: worker threads
# hold different override scopes. Behavior-identical to resolving per call.
_plan_local = threading.local()


def _resolved_plan() -> tuple:
    """(enabled evaluator fns, enabled CHEAP evaluator fns, rules mode)."""
    tok    = cfg.resolution_token()
    cached = getattr(_plan_local, "plan", None)
    if cached is not None and cached[0] == tok:
        return cached[1]
    enabled = tuple(fn for toggle, fn in _CONDITIONS.values() if getattr(cfg, toggle))
    cheap   = tuple(_CONDITIONS[k][1] for k in _CHEAP_KEYS
                    if getattr(cfg, _CONDITIONS[k][0]))
    plan = (enabled, cheap, _rules_mode())
    _plan_local.plan = (tok, plan)
    return plan


# ── Evaluation entry points (shared live + backtest) ──────────────────────────

def build_entry_checks(ind: IndicatorResult,
                       depth_ratio: Optional[float]) -> Dict[str, bool]:
    """All condition outcomes (toggles NOT applied) — for diagnostics/UI.
    Includes a 'custom_rules' pseudo-check whenever custom rules are active."""
    checks = {k: fn(ind, depth_ratio) for k, (_, fn) in _CONDITIONS.items()}
    if _rules_mode() is not None:
        checks["custom_rules"] = custom_rules_ok(ind, depth_ratio)
    return checks


def failed_entry_checks(checks: Dict[str, bool]) -> List[str]:
    """Failing checks among the ENABLED conditions (disabled = auto-pass).
    In replace mode only the custom rules can block; in and mode they add on."""
    mode = _rules_mode()
    if mode == "replace":
        return [] if checks.get("custom_rules", False) else ["custom_rules"]
    failed = [k for k, v in checks.items()
              if not v and k in CONDITION_TOGGLES and getattr(cfg, CONDITION_TOGGLES[k])]
    if mode == "and" and not checks.get("custom_rules", True):
        failed.append("custom_rules")
    return failed


def entry_ok(ind: IndicatorResult, depth_ratio: Optional[float] = None) -> bool:
    """
    Short-circuit conjunction of the ENABLED conditions — the backtest's hot
    path (no dict build, stops at the first enabled failure). Same tables as
    build_entry_checks/failed_entry_checks, so the two styles cannot drift;
    the toggle resolution is cached per cfg.resolution_token() (see
    _resolved_plan) but semantically identical to per-call getattr.
    """
    enabled, _, mode = _resolved_plan()
    if mode == "replace":
        return custom_rules_ok(ind, depth_ratio)
    if mode == "and" and not custom_rules_ok(ind, depth_ratio):
        return False
    for fn in enabled:
        if not fn(ind, depth_ratio):
            return False
    return True


# The conditions computable WITHOUT TA-Lib — compute_indicators evaluates
# these first and (in entry_short_circuit mode) skips the expensive RSI/MACD/
# ADX calls when one already vetoes the conjunctive entry.
_CHEAP_KEYS = ("near_support", "bullish_pattern", "above_vwap", "volume_surge")


def cheap_gates_veto(ind: IndicatorResult) -> bool:
    """
    True when an ENABLED cheap condition already fails — the RSI/MACD/ADX
    values then can't change the (conjunctive) entry decision. Driven by the
    same _CONDITIONS table so the short-circuit can never disagree with
    entry_ok / failed_entry_checks.

    Custom rules in REPLACE mode disable this veto entirely: the fixed cheap
    conditions no longer gate the entry, so an early reject on them would
    wrongly veto rule sets that don't require them (and rules need the full
    RSI/MACD/ADX values anyway).
    """
    _, cheap_enabled, mode = _resolved_plan()
    if mode == "replace":
        return False
    for fn in cheap_enabled:
        if not fn(ind, None):
            return True
    return False
