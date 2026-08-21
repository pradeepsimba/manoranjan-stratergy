from __future__ import annotations

"""
Bank Nifty breakout / support-resistance / weighted global-signal — ported
from c.html's detectSwings/detectSwingBreakouts/detectSRBreakouts/
calculatePivot/detectPivotBreakouts/analyzeContributions/detectBreakouts,
detectSupportResistance/clusterLevels, and updateGlobalSignal.

This is entirely separate from the BN options trading strategy
(bn_signals.py/bn_entry_exit.py) — c.html renders it as an informational
"next candle prediction" banner and a weighted global-signal badge; it never
feeds the actual entry/exit decision. Ported here purely for UI parity.

Pure/stateless, like bn_pricing.py/bn_signals.py — takes candle lists in,
returns plain dicts, no AppState access.

Indexing note: c.html's `lastNCandles[symbol]` is ordered NEWEST-FIRST
(`arr[0]` = latest bar). This repo's candle lists are strictly chronological,
oldest-to-newest (see CLAUDE.md), so every "arr[0]" in the source becomes
"candles[-1]" here, "arr[1]" becomes "candles[-2]", etc.
"""

from typing import Dict, List, Optional

from app.models import Candle


# ── Swing / S-R / pivot breakout detection (operates on ONE candle series) ───

def detect_swings(candles: List[Candle], lookback: int = 2) -> Dict[str, List[Dict]]:
    """Port of c.html's detectSwings — local swing highs/lows over a symmetric window."""
    highs: List[Dict] = []
    lows:  List[Dict] = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        current_high = candles[i].high
        current_low  = candles[i].low
        window = [candles[i - lookback + offset] for offset in range(lookback * 2 + 1) if offset != lookback]
        is_swing_high = all(current_high > c.high for c in window)
        is_swing_low  = all(current_low  < c.low  for c in window)
        if is_swing_high:
            highs.append({"index": i, "price": current_high, "time": candles[i].start_time})
        if is_swing_low:
            lows.append({"index": i, "price": current_low, "time": candles[i].start_time})
    return {"highs": highs, "lows": lows}


def detect_swing_breakouts(candles: List[Candle], swings: Dict[str, List[Dict]]) -> Dict:
    """Port of c.html's detectSwingBreakouts."""
    if not swings["highs"] or not swings["lows"] or not candles:
        return {"type": None, "direction": None, "details": {}}
    latest = candles[-1]
    recent_high = swings["highs"][-1]["price"]
    recent_low  = swings["lows"][-1]["price"]
    if latest.close > recent_high and latest.high > recent_high:
        return {"type": "swing", "direction": "bullish", "details": {"level": recent_high}}
    if latest.close < recent_low and latest.low < recent_low:
        return {"type": "swing", "direction": "bearish", "details": {"level": recent_low}}
    return {"type": None, "direction": None, "details": {}}


def detect_sr_breakouts(candles: List[Candle], sr_levels: Dict[str, List[float]]) -> Dict:
    """Port of c.html's detectSRBreakouts — uses the nearest (last) support/resistance."""
    if not candles or not sr_levels["supports"] or not sr_levels["resistances"]:
        return {"type": None, "direction": None, "details": {}}
    latest = candles[-1]
    support    = sr_levels["supports"][0]
    resistance = sr_levels["resistances"][0]
    if latest.close < support and latest.low < support:
        return {"type": "support", "direction": "bearish", "details": {"level": support}}
    if latest.close > resistance and latest.high > resistance:
        return {"type": "resistance", "direction": "bullish", "details": {"level": resistance}}
    return {"type": None, "direction": None, "details": {}}


def calculate_pivot(candles: List[Candle]) -> Optional[float]:
    """Port of c.html's calculatePivot — classic (H+L+C)/3 on the PREVIOUS bar."""
    if len(candles) < 1:
        return None
    prev = candles[-2] if len(candles) >= 2 else candles[-1]
    return (prev.high + prev.low + prev.close) / 3.0


def detect_pivot_breakouts(candles: List[Candle]) -> Dict:
    """Port of c.html's detectPivotBreakouts."""
    if len(candles) < 2:
        return {"type": None, "direction": None, "details": {}}
    pivot = calculate_pivot(candles)
    latest = candles[-1]
    if latest.close > pivot and latest.high > pivot:
        return {"type": "pivot", "direction": "bullish", "details": {"level": pivot}}
    if latest.close < pivot and latest.low < pivot:
        return {"type": "pivot", "direction": "bearish", "details": {"level": pivot}}
    return {"type": None, "direction": None, "details": {}}


# ── Support/resistance clustering (used both for the S-R table and detectSRBreakouts) ─

def average(values: List[float]) -> float:
    return sum(values) / len(values)


def cluster_levels(levels: List[float], threshold_pct: float = 0.2) -> List[float]:
    """Port of c.html's clusterLevels — merges levels within threshold_pct% of each other."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters: List[float] = []
    group = [levels[0]]
    for lvl in levels[1:]:
        if abs(lvl - group[-1]) / group[-1] < threshold_pct / 100.0:
            group.append(lvl)
        else:
            clusters.append(average(group))
            group = [lvl]
    clusters.append(average(group))
    return clusters


def detect_support_resistance(candles: List[Candle]) -> Dict[str, List[float]]:
    """Port of c.html's detectSupportResistance — local close-price minima/maxima, clustered."""
    supports: List[float] = []
    resistances: List[float] = []
    n = len(candles)
    for i in range(2, n - 2):
        p2, p1 = candles[i - 2].close, candles[i - 1].close
        c = candles[i].close
        n1, n2 = candles[i + 1].close, candles[i + 2].close
        if c < p1 and c < p2 and c < n1 and c < n2:
            supports.append(c)
        if c > p1 and c > p2 and c > n1 and c > n2:
            resistances.append(c)
    clustered_supports    = cluster_levels(supports, 0.25)
    clustered_resistances = cluster_levels(resistances, 0.25)
    return {
        "supports":    clustered_supports[-3:],
        "resistances": clustered_resistances[-3:],
    }


# ── Contribution analysis + the combined breakout-prediction banner ─────────

def analyze_contributions(breakout_direction: str, breakout_candle: Candle,
                          leader_candles: Dict[str, List[Candle]],
                          weights: Dict[str, float]) -> Dict:
    """
    Port of c.html's analyzeContributions — top-5-by-weight stocks (from
    `weights`, keyed by token — cfg.BN_INDEX_WEIGHTS or cfg.NF_INDEX_WEIGHTS)
    checked for a same-direction, >0.1% move on the SAME bar as the breakout
    candle. `leader_candles` is keyed by token, matching `weights`' keys.
    """
    top5 = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]
    contributors: List[Dict] = []
    contributing_count = 0
    for token, weight in top5:
        candles = leader_candles.get(token, [])
        stock_candle = next((c for c in candles if c.start_time == breakout_candle.start_time), None)
        if stock_candle is None:
            continue
        change = ((stock_candle.close - stock_candle.open) / stock_candle.open) * 100.0 if stock_candle.open else 0.0
        points = stock_candle.close - stock_candle.open
        significant = (
            (breakout_direction == "bullish" and change > 0.1) or
            (breakout_direction == "bearish" and change < -0.1)
        )
        if significant:
            contributing_count += 1
        contributors.append({
            "token": token, "weight": weight,
            "change": round(change, 2), "points": round(points, 2),
            "significant": significant,
        })
    return {"contributors": contributors, "valid": contributing_count >= 3}


def compute_breakout_prediction(bn_candles: List[Candle], leader_candles: Dict[str, List[Candle]],
                                weights: Dict[str, float]) -> Dict:
    """
    Port of c.html's detectBreakouts — tries swing, then S/R, then pivot
    breakouts (first hit wins) on the index candles, and — if one fires —
    validates it against the top-5-weighted-stock contribution check.
    `weights` is cfg.BN_INDEX_WEIGHTS or cfg.NF_INDEX_WEIGHTS depending on
    which instrument's candles are passed in.
    """
    if len(bn_candles) < 3:
        return {"type": None, "direction": None, "level": None, "contributors": [], "valid": False}

    latest = bn_candles[-1]
    swings = detect_swings(bn_candles, 2)
    sr_levels = detect_support_resistance(bn_candles)

    breakout = detect_swing_breakouts(bn_candles, swings)
    if not breakout["type"]:
        breakout = detect_sr_breakouts(bn_candles, sr_levels)
    if not breakout["type"]:
        breakout = detect_pivot_breakouts(bn_candles)

    if not breakout["type"]:
        return {"type": None, "direction": None, "level": None, "contributors": [], "valid": False}

    contributions = analyze_contributions(breakout["direction"], latest, leader_candles, weights)
    return {
        "type":         breakout["type"],
        "direction":    breakout["direction"],
        "level":        breakout["details"].get("level"),
        "contributors": contributions["contributors"],
        "valid":        contributions["valid"],
    }


# ── Weighted global signal (green/red column tally + INDEX_WEIGHTS %) ───────

def compute_column_counts(all_candles: Dict[str, List[Candle]], num_candles: int) -> List[Dict[str, int]]:
    """
    Port of c.html's updateColumnCounts' tally step: for each of the last
    `num_candles` bars (column 0 = most recent), count green/red/neutral
    across every stock in `all_candles` (keyed by token, all 12 incl. index).
    """
    counts = [{"g": 0, "r": 0, "n": 0} for _ in range(num_candles)]
    for candles in all_candles.values():
        for i in range(num_candles):
            candle = candles[-(i + 1)] if len(candles) > i else None
            if candle is None or not candle.open or not candle.close:
                counts[i]["n"] += 1
            elif candle.close > candle.open:
                counts[i]["g"] += 1
            elif candle.close < candle.open:
                counts[i]["r"] += 1
            else:
                counts[i]["n"] += 1
    return counts


def compute_global_signal(counts: List[Dict[str, int]], latest_by_token: Dict[str, Candle],
                          bn_token: str, weights: Dict[str, float]) -> Dict:
    """
    Port of c.html's updateGlobalSignal. `counts` from compute_column_counts;
    `latest_by_token` is each stock's single most-recent candle (index/leader
    stocks alike, keyed by token) — c.html's asymmetric allGreen(>=4)/
    allRed(>=5) thresholds are intentional quirks of the source, kept as-is.
    `weights` is cfg.BN_INDEX_WEIGHTS or cfg.NF_INDEX_WEIGHTS.
    """
    num_candles = len(counts)
    all_green = num_candles > 0 and all(c["g"] >= 4 for c in counts)
    all_red   = num_candles > 0 and all(c["r"] >= 5 for c in counts)

    count_signal, count_color = "NEUTRAL", "#777"
    if all_green:
        count_signal, count_color = "BUY", "green"
    elif all_red:
        count_signal, count_color = "SELL", "red"

    weighted_pct = 0.0
    total_weight = 0.0
    for token, weight_pct in weights.items():
        w = weight_pct / 100.0
        candle = latest_by_token.get(token)
        pct = ((candle.close - candle.open) / candle.open) * 100.0 if candle and candle.open and candle.close else 0.0
        weighted_pct += w * pct
        total_weight += w
    weighted_pct = weighted_pct / total_weight if total_weight else 0.0

    bn_candle = latest_by_token.get(bn_token)
    bn_level = bn_candle.close if bn_candle else None
    points = round(weighted_pct / 100.0 * bn_level) if bn_level else None

    final_signal, final_color = count_signal, count_color
    if abs(weighted_pct) > 0.08:
        final_signal = "STRONG BUY" if weighted_pct > 0 else "STRONG SELL"
        final_color  = "#00ff00" if weighted_pct > 0 else "#ff0000"

    return {
        "signal": final_signal, "color": final_color,
        "countSignal": count_signal, "countColor": count_color,
        "weightedPct": round(weighted_pct, 3), "points": points,
    }
