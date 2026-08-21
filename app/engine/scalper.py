from __future__ import annotations

"""
Intraday order-book scalper — signal engine.

Three concerns, deliberately separated (data parsing lives one module over in
app/engine/orderbook.py; order placement and bracket management live in
app/services/scalp_engine.py):

  1. session_profile  — the time-of-day adaptive filter: which window the
                        exchange clock is in, whether execution is allowed there,
                        and the imbalance ratio that window demands.
  2. evaluate         — the signal conjunction: W-OBI ratio, order-count and
                        anti-spoofing filters, then tape/traded-volume
                        confirmation. Cheapest checks first; the first veto wins.
  3. plan_entry       — sizing, stop/target placement, slippage tolerance and
                        the transaction-cost buffer.

Everything here is a pure function of its arguments plus dynamic config — no
AppState, no I/O, no TA-Lib — so the whole decision path is unit-testable and
runs on the event loop in microseconds (unlike the core strategy's TA-Lib scan,
which needs the thread pool).

Config is read at CALL time throughout (never captured in a default argument or
module constant) so every knob stays live-editable from /settings, exactly like
the rest of the engine.
"""

from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple

import app.config as cfg
from app.backtest.fills import round_trip_costs
from app.engine.orderbook import (
    obi,
    parse_weights,
    single_ticket_level,
    tape_stats,
    total_orders,
)
from app.models import (
    STRATEGY_SCALP,
    EntrySignal,
    OrderBook,
    ScalpDecision,
    ScalpSession,
    TapeEvent,
)

# Window order matters: the profile picks the LAST window whose start time the
# clock has passed. (window key, start-time config prefix, ratio config attr)
_WINDOWS: Tuple[Tuple[str, str, Optional[str]], ...] = (
    ("warmup",    "SCALP_WARMUP",    None),
    ("morning",   "SCALP_MORNING",   "SCALP_RATIO_MORNING"),
    ("midday",    "SCALP_MIDDAY",    "SCALP_RATIO_MIDDAY"),
    ("afternoon", "SCALP_AFTERNOON", "SCALP_RATIO_AFTERNOON"),
    ("squareoff", "SCALP_SQUAREOFF", None),
)


def _minutes(prefix: str) -> int:
    return getattr(cfg, f"{prefix}_HOUR") * 60 + getattr(cfg, f"{prefix}_MIN")


# ── 1. Time-of-day adaptive filter ────────────────────────────────────────────

def session_profile(now: datetime) -> ScalpSession:
    """
    Resolve the scalping session window for an IST wall clock.

      before warmup   closed     — no scanning, no execution
      warmup          warmup     — scan + diagnose only (avoids the opening
                                   auction's noise; no orders)
      morning         morning    — execute at SCALP_RATIO_MORNING
      midday          midday     — execute at SCALP_RATIO_MIDDAY (a STRICTER
                                   ratio for the chop), or scan-only when
                                   SCALP_MIDDAY_ENABLED is off
      afternoon       afternoon  — execute at SCALP_RATIO_AFTERNOON
      squareoff       squareoff  — stop scanning; the engine cancels pending
                                   intents and flattens every scalp position

    `execute` is the ONLY thing callers should test before placing an order —
    it already folds in the midday pause toggle and the master SCALP_ENABLED
    switch, so no caller has to re-derive the policy.
    """
    mins    = now.hour * 60 + now.minute
    enabled = bool(cfg.SCALP_ENABLED)

    window: str = "closed"
    ratio_attr: Optional[str] = None
    for key, prefix, attr in _WINDOWS:
        if mins >= _minutes(prefix):
            window, ratio_attr = key, attr
        else:
            break

    # `required_ratio` ALWAYS means "the ratio an evaluation is scored against",
    # never a sentinel. The non-executing windows report the morning ratio rather
    # than 0.0 because 0.0 would pass every symbol trivially, so the warm-up
    # scanner (whose entire purpose is to show what WOULD have fired before
    # capital is committed) and the /api/scalp/scan diagnostic would label a flat
    # book a signal. `execute` is what says whether an order may actually be
    # placed — callers must test that, never the ratio.
    if window == "closed":
        return ScalpSession(window=window, execute=False,
                            required_ratio=float(cfg.SCALP_RATIO_MORNING),
                            note="before the scalper's warm-up window")
    if window == "warmup":
        return ScalpSession(window=window, execute=False,
                            required_ratio=float(cfg.SCALP_RATIO_MORNING),
                            note="warm-up: scanner only, no execution")
    if window == "squareoff":
        return ScalpSession(window=window, execute=False,
                            required_ratio=float(cfg.SCALP_RATIO_MORNING),
                            note="past square-off: flattening only")

    required = float(getattr(cfg, ratio_attr)) if ratio_attr else 0.0
    if window == "midday" and not cfg.SCALP_MIDDAY_ENABLED:
        return ScalpSession(window=window, execute=False, required_ratio=required,
                            note="midday chop: scanner paused by config")
    if not enabled:
        return ScalpSession(window=window, execute=False, required_ratio=required,
                            note="scalper disabled (SCALP_ENABLED off)")
    return ScalpSession(window=window, execute=True, required_ratio=required,
                        note=f"{window}: required W-OBI ratio {required:g}")


def describe_windows() -> list:
    """
    The configured window boundaries, for the dashboard / API.

    `requiredRatio` is the ratio CONFIGURED for that window (None for the two
    scan-only/flatten windows, which have none of their own) — distinct from
    session_profile's `required_ratio`, which is the ratio the CURRENT evaluation
    is scored against and therefore never None.

    `execute` folds in the midday pause toggle: reporting midday as executable
    while SCALP_MIDDAY_ENABLED is off would contradict the engine.
    """
    out = []
    for key, prefix, attr in _WINDOWS:
        h, m = getattr(cfg, f"{prefix}_HOUR"), getattr(cfg, f"{prefix}_MIN")
        execute = key in ("morning", "midday", "afternoon")
        if key == "midday" and not cfg.SCALP_MIDDAY_ENABLED:
            execute = False
        out.append({
            "window":         key,
            "start":          f"{h:02d}:{m:02d}",
            "requiredRatio":  float(getattr(cfg, attr)) if attr else None,
            "execute":        execute,
        })
    return out


# ── 2. Signal engine ──────────────────────────────────────────────────────────

def evaluate(book: Optional[OrderBook],
             tape: Sequence[TapeEvent],
             now_mono: float,
             required_ratio: float,
             ltp: float = 0.0) -> ScalpDecision:
    """
    Order-book + tape conjunction for ONE symbol.

    Checks run cheapest-first and the first failure short-circuits, so a symbol
    whose book is stale or thin costs almost nothing to reject. `metrics` always
    carries whatever was computed before the veto — that is what the dashboard
    shows, and it is the difference between "no signals today" and knowing the
    ratio sat at 1.8 against a required 3.0 all morning.

    now_mono is time.monotonic() (never wall clock): book age and the tape
    window are elapsed-time measurements, which a clock adjustment must not warp.
    """
    m: Dict[str, object] = {}

    if book is None or not book.bids or not book.asks:
        return ScalpDecision(ok=False, reason="no order book", metrics=m)

    # (a) Freshness — a scalp signal off a stale book is the single most
    # dangerous failure mode here: the WS can stay "connected" while the depth
    # feed goes quiet, and an imbalance from 30s ago is fiction.
    age = now_mono - book.ts
    m["bookAgeS"] = round(age, 2)
    if age > cfg.SCALP_MAX_BOOK_AGE_S:
        return ScalpDecision(ok=False, reason=f"stale book ({age:.1f}s)", metrics=m)

    min_levels = int(cfg.SCALP_MIN_LEVELS)
    m["bidLevels"] = len(book.bids)
    m["askLevels"] = len(book.asks)
    if len(book.bids) < min_levels or len(book.asks) < min_levels:
        return ScalpDecision(
            ok=False,
            reason=f"thin book ({len(book.bids)}×{len(book.asks)} levels, need {min_levels})",
            metrics=m)

    # (b) Spread — the round trip pays it twice. A wide spread also means the
    # displayed imbalance is not actionable at anything like the current price.
    ref   = ltp if ltp > 0 else book.best_ask()
    spread = book.spread()
    m["bid"], m["ask"], m["spread"] = book.best_bid(), book.best_ask(), spread
    if spread is None or spread < 0:
        return ScalpDecision(ok=False, reason="crossed/invalid book", metrics=m)
    spread_pct = (spread / ref * 100.0) if ref > 0 else None
    m["spreadPct"] = round(spread_pct, 4) if spread_pct is not None else None
    if spread_pct is None:
        return ScalpDecision(ok=False, reason="no reference price", metrics=m)
    if spread_pct > cfg.SCALP_MAX_SPREAD_PCT:
        return ScalpDecision(
            ok=False,
            reason=f"spread {spread_pct:.3f}% > {cfg.SCALP_MAX_SPREAD_PCT:g}%",
            metrics=m)

    # (c) Weighted Order Book Imbalance.
    weights = parse_weights(cfg.SCALP_OBI_WEIGHTS)
    wb, wa, ratio = obi(book, weights)
    m["weightedBids"] = round(wb, 1)
    m["weightedAsks"] = round(wa, 1)
    m["obiRatio"]     = round(ratio, 3) if ratio is not None else None
    m["requiredRatio"] = required_ratio
    if ratio is None:
        return ScalpDecision(ok=False, reason="no ask-side depth", metrics=m)
    if ratio < required_ratio:
        return ScalpDecision(
            ok=False, reason=f"W-OBI {ratio:.2f} < {required_ratio:g}", metrics=m)

    # (d) Order-count + anti-spoofing. Both need per-level order counts; when the
    # feed doesn't publish them SCALP_REQUIRE_ORDER_DATA decides between
    # fail-closed (block) and fail-open (trade without the filter, the default —
    # consistent with depth_bullish's "absent data auto-passes" convention).
    bid_orders = total_orders(book.bids, int(cfg.SCALP_ORDER_COUNT_DEPTH))
    m["bidOrders"]   = bid_orders
    m["ordersSeen"]  = book.orders_seen
    if bid_orders is None:
        if cfg.SCALP_REQUIRE_ORDER_DATA:
            return ScalpDecision(
                ok=False,
                reason="feed publishes no per-level order counts "
                       "(SCALP_REQUIRE_ORDER_DATA is on)",
                metrics=m)
    else:
        if bid_orders < cfg.SCALP_MIN_ORDER_COUNT:
            return ScalpDecision(
                ok=False,
                reason=f"only {bid_orders} bid orders < {cfg.SCALP_MIN_ORDER_COUNT:g}",
                metrics=m)
        spoof_lv = single_ticket_level(book.bids,
                                      int(cfg.SCALP_SPOOF_DEPTH),
                                      float(cfg.SCALP_SPOOF_MIN_SHARE))
        m["spoofLevel"] = spoof_lv
        if spoof_lv is not None:
            return ScalpDecision(
                ok=False,
                reason=f"single-ticket wall at level {spoof_lv} (possible spoof)",
                metrics=m)

    # (e) Tape confirmation — displayed depth is an intention; the tape is what
    # actually traded. Require real aggressive buying INTO the ask, not just a
    # bid-heavy book (which is exactly what a spoofer shows you).
    ts = tape_stats(tape, now_mono, float(cfg.SCALP_TAPE_WINDOW_S))
    m["tapeBuyQty"]     = round(ts["buy_qty"], 1)
    m["tapeSellQty"]    = round(ts["sell_qty"], 1)
    m["tapeTrades"]     = int(ts["trades"])
    m["tapeBuyRatio"]   = round(ts["buy_ratio"], 3)
    m["tapeVelocity"]   = round(ts["buy_velocity"], 1)
    if ts["trades"] < cfg.SCALP_MIN_TAPE_TRADES:
        return ScalpDecision(
            ok=False,
            reason=f"tape too quiet ({int(ts['trades'])} prints "
                   f"< {cfg.SCALP_MIN_TAPE_TRADES:g})",
            metrics=m)
    if ts["buy_qty"] < cfg.SCALP_MIN_TAPE_QTY:
        return ScalpDecision(
            ok=False,
            reason=f"aggressive buy volume {ts['buy_qty']:.0f} "
                   f"< {cfg.SCALP_MIN_TAPE_QTY:g}",
            metrics=m)
    if ts["buy_ratio"] < cfg.SCALP_MIN_TAPE_BUY_RATIO:
        return ScalpDecision(
            ok=False,
            reason=f"tape buy ratio {ts['buy_ratio']:.2f} "
                   f"< {cfg.SCALP_MIN_TAPE_BUY_RATIO:g}",
            metrics=m)
    if cfg.SCALP_REQUIRE_ASK_HIT and not _recent_ask_hit(tape, now_mono):
        return ScalpDecision(
            ok=False, reason="no recent print at the ask", metrics=m)

    return ScalpDecision(ok=True, reason="", metrics=m)


def _recent_ask_hit(tape: Sequence[TapeEvent], now_mono: float) -> bool:
    """
    True when a print inside SCALP_ASK_HIT_WINDOW_S traded AT or ABOVE the
    prevailing ask — i.e. a buyer is currently paying up, which is the
    "high-velocity active buying hitting the ask" the blueprint asks to confirm
    before firing.

    Prints whose book was unknown (bid/ask 0.0) can't be classified this way, so
    they neither confirm nor deny; the surrounding tape thresholds still apply.
    """
    cutoff = now_mono - float(cfg.SCALP_ASK_HIT_WINDOW_S)
    for ev in reversed(tape):
        if ev.ts < cutoff:
            return False
        if ev.qty > 0 and ev.ask > 0 and ev.price >= ev.ask:
            return True
    return False


# ── 3. Sizing, brackets, cost buffer ──────────────────────────────────────────

def plan_entry(symbol: str, token: str, book: OrderBook, ltp: float,
               available: float, total_capital: Optional[float] = None,
               metrics: Optional[dict] = None
               ) -> Tuple[Optional[EntrySignal], str]:
    """
    Turn a passing signal into a sized, bracketed order intent.

    Returns (signal, "") or (None, rejection reason). Rejections here are
    ECONOMIC (unaffordable, stop too tight to clear costs, slippage beyond
    tolerance) rather than about the setup itself, and are reported separately on
    the dashboard so a config problem doesn't look like an absent market.

    Pricing: `EntrySignal.ltp` is the UNSLIPPED reference the order is sent at —
    the ask when SCALP_ENTRY_AT_ASK is on (a market buy crosses the spread), else
    the last price. paper_trade.place_paper_order applies SLIPPAGE_BPS on top,
    so sizing/target math here uses the same PROJECTED fill it will produce; if
    it used the raw reference, every scalp's cost buffer would be understated by
    exactly the slippage.
    """
    if total_capital is None:
        total_capital = cfg.ACCOUNT_BALANCE

    ask = book.best_ask()
    ref = ask if (cfg.SCALP_ENTRY_AT_ASK and ask > 0) else (ltp or ask)
    if ref <= 0:
        return None, "no entry reference price"

    # Projected fill — mirrors paper_trade._slip_buy exactly.
    fill = ref * (1 + cfg.SLIPPAGE_BPS / 10_000.0)

    # Slippage tolerance: how far the projected fill may sit above the price the
    # signal was measured at. Catches the case where the ask has already run away
    # from the LTP that triggered us (chasing) as well as a fat spread.
    if ltp > 0:
        slip_pct = (fill / ltp - 1.0) * 100.0
        if slip_pct > cfg.SCALP_MAX_SLIPPAGE_PCT:
            return None, (f"projected slippage {slip_pct:.3f}% > "
                          f"{cfg.SCALP_MAX_SLIPPAGE_PCT:g}%")

    # Stop distance: a true percentage of the fill (scalps need a price-
    # proportional stop, not a ₹ constant), floored at SCALP_MIN_SL_OFFSET so a
    # low-priced stock can't get a sub-tick stop that the first print takes out.
    sl_offset = round(max(fill * cfg.SCALP_SL_PCT / 100.0,
                          cfg.SCALP_MIN_SL_OFFSET), 2)
    if sl_offset <= 0:
        return None, "invalid stop distance"
    target_offset = round(sl_offset * cfg.SCALP_RR_RATIO, 2)

    lev = max(1, int(cfg.INTRADAY_LEVERAGE))

    # Two independent ceilings — the binding one wins:
    #   allocation — SCALP_ALLOC_PCT of account equity of OWN funds per trade,
    #                capped by what open positions haven't already committed.
    #   risk       — the ₹ (or % of equity) a stop-out may cost.
    alloc = min(total_capital * cfg.SCALP_ALLOC_PCT / 100.0, available)
    if alloc <= 0:
        return None, "no free capital"
    qty_alloc = int((alloc * lev) / fill)

    if cfg.SCALP_RISK_MODE == "capital_pct":
        risk = total_capital * cfg.SCALP_RISK_CAPITAL_PERCENT / 100.0
    else:
        risk = cfg.SCALP_RISK_PER_TRADE
    qty_risk = int(risk / sl_offset)

    qty = min(qty_alloc, qty_risk)
    if qty < 1:
        return None, (f"size 0 (alloc allows {qty_alloc}, risk allows {qty_risk})")

    # Transaction-cost buffer: a scalp whose whole target is eaten by brokerage +
    # STT + GST + exchange charges is a guaranteed loser even when it WINS. Deny
    # it here rather than discovering it in the P&L. (Deliberately NOT floored at
    # 1 share like the core calc_quantity: for a scalp, "always tradeable" would
    # just mean "always cost-dominated".)
    buy_value  = qty * fill
    sell_value = qty * (fill + target_offset)
    costs      = round_trip_costs(buy_value, sell_value)
    gross_at_target = target_offset * qty
    if gross_at_target < costs * cfg.SCALP_COST_BUFFER_MULT:
        return None, (f"target ₹{gross_at_target:.2f} does not clear costs "
                      f"₹{costs:.2f} × {cfg.SCALP_COST_BUFFER_MULT:g}")

    scalp_meta = dict(metrics or {})
    scalp_meta.update({
        "entryRef":      round(ref, 2),
        "projectedFill": round(fill, 2),
        "slOffset":      sl_offset,
        "targetOffset":  target_offset,
        "costs":         round(costs, 2),
        "grossAtTarget": round(gross_at_target, 2),
        "qtyAlloc":      qty_alloc,
        "qtyRisk":       qty_risk,
    })

    return EntrySignal(
        symbol         = symbol,
        token          = token,
        ltp            = round(ref, 2),
        support        = 0.0,          # no structural support in a scalp entry
        sl_offset      = sl_offset,
        target_offset  = target_offset,
        quantity       = qty,
        capital_needed = (fill * qty) / lev,
        bar_time       = "",
        strategy       = STRATEGY_SCALP,
        scalp          = scalp_meta,
    ), ""
