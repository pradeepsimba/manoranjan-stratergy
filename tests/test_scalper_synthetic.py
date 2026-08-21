"""
Synthetic conformance test for the order-book scalper.

Exercises the REAL decision path (parse → W-OBI → filters → tape → sizing →
risk gates) on fabricated books and tapes with hand-computed expected results:

  * snap parsing across every plausible upstream text layout, plus the
    structured (JSON) SnapQuote shape and the index's -0.01 sentinel rows
  * W-OBI weighting math and the undefined-ratio (empty ask side) case
  * order-count and single-ticket (anti-spoofing) filters, including the
    "feed publishes no order counts" fail-open / fail-closed switch
  * tape classification (ask-hit vs bid-hit vs inside-spread), windowing and
    the bounded-tape append
  * the time-of-day session controller across a whole trading day
  * evaluate()'s veto ORDER — each filter is shown to be the one that fires
  * sizing by allocation vs risk ceiling, the transaction-cost buffer, and the
    slippage tolerance
  * the scalp risk gates (concurrency, per-symbol/day churn caps, cooldown,
    both loss limits)
  * settings validation: level weights and the scalper's time-order chain

Pure python — no numpy, no TA-Lib, no database, no market feed:

    python3 tests/test_scalper_synthetic.py
"""
from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Test labels (and the engine's own messages) contain ₹, →, ×. A Windows console
# defaults to cp1252 and raises UnicodeEncodeError on those, which would fail the
# suite for a printing problem rather than a logic one.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import app.config as cfg
from app.engine.orderbook import (
    DEFAULT_WEIGHTS,
    append_tape,
    obi,
    parse_snap,
    parse_weights,
    single_ticket_level,
    tape_stats,
    total_orders,
    weighted_depth,
)
from app.engine.position_manager import can_enter_scalp
from app.engine.scalper import evaluate, plan_entry, session_profile
from app.models import BookLevel, OrderBook, TapeEvent

PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL {name}: {detail}"
    PASS += 1
    print(f"  ok  {name}")


def section(title):
    print(f"\n=== {title} ===")


@contextmanager
def over(**changes):
    """Scope runtime config overrides (restoring previous values on exit).

    set_runtime_overrides, NOT thread_overrides: the scalper runs on the event
    loop, where thread-local overrides are forbidden — so the tests must exercise
    the same resolution path production uses."""
    prev = {k: getattr(cfg, k) for k in changes}
    cfg.set_runtime_overrides(changes)
    try:
        yield
    finally:
        cfg.set_runtime_overrides(prev)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def book(bids, asks, ts=None, ltp=100.0, orders_seen=None):
    """Build an OrderBook from [(price, qty[, orders]), …] tuples."""
    def lv(rows):
        return tuple(BookLevel(price=r[0], qty=r[1],
                               orders=(r[2] if len(r) > 2 else None))
                     for r in rows)
    b, a = lv(bids), lv(asks)
    seen = (any(l.orders is not None for l in b + a)
            if orders_seen is None else orders_seen)
    return OrderBook(bids=b, asks=a, ltp=ltp, orders_seen=seen,
                     ts=time.monotonic() if ts is None else ts)


def bullish_book(ts=None):
    """A book that clears the default filters: 5 levels each side, deep bid
    stack, many orders per level, 1-paisa spread."""
    return book(
        bids=[(100.00, 2000, 40), (99.95, 1500, 30), (99.90, 1200, 25),
              (99.85, 800, 20), (99.80, 600, 15)],
        asks=[(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8),
              (100.20, 150, 6), (100.25, 100, 5)],
        ts=ts, ltp=100.05)


def buy_tape(now, n=5, qty=200.0, ask=100.05, bid=100.00, price=None):
    """n aggressive-buy prints (at the ask), most recent last."""
    px = ask if price is None else price
    return tuple(TapeEvent(ts=now - (n - i) * 0.2, price=px, qty=qty,
                           bid=bid, ask=ask)
                 for i in range(n))


# ── 1. Snap parsing ───────────────────────────────────────────────────────────

def s1():
    section("1. snap parsing — every upstream layout")

    # The layout the pre-existing L1 parser was written against (no order counts)
    legacy = ("LTP 100.05 BuyQty 12000 SellQty 4000  "
              "Bids: 1) 100.00 x 2000  2) 99.95 x 1500  3) 99.90 x 1200 "
              "4) 99.85 x 800  5) 99.80 x 600  "
              "Asks: 1) 100.05 x 300  2) 100.10 x 250  3) 100.15 x 200 "
              "4) 100.20 x 150  5) 100.25 x 100")
    b = parse_snap(legacy, ts=1.0)
    check("5 bid levels parsed", len(b.bids) == 5, len(b.bids))
    check("5 ask levels parsed", len(b.asks) == 5, len(b.asks))
    check("L1 bid price/qty", (b.bids[0].price, b.bids[0].qty) == (100.00, 2000),
          b.bids[0])
    check("L5 ask price/qty", (b.asks[4].price, b.asks[4].qty) == (100.25, 100),
          b.asks[4])
    check("aggregate buy/sell qty", (b.buy_qty, b.sell_qty) == (12000, 4000),
          (b.buy_qty, b.sell_qty))
    check("LTP from snap", b.ltp == 100.05, b.ltp)
    check("no order counts → orders_seen False", b.orders_seen is False)
    check("total_orders None when unpublished", total_orders(b.bids) is None)
    # The critical no-swallow property: level 2's "2)" index must NOT be read as
    # level 1's order count.
    check("index not mistaken for order count",
          all(l.orders is None for l in b.bids), [l.orders for l in b.bids])

    for label, fmt in (
        ("parenthesised", "Bids: 1) 100.00 x 2000 (40)  2) 99.95 x 1500 (30)"),
        ("bracketed",     "Bids: 1) 100.00 x 2000 [40]  2) 99.95 x 1500 [30]"),
        ("slash",         "Bids: 1) 100.00 x 2000 / 40  2) 99.95 x 1500 / 30"),
        ("at-ord",        "Bids: 1) 100.00 x 2000 @ 40 ord  2) 99.95 x 1500 @ 30 ord"),
    ):
        p = parse_snap(fmt + "  Asks: 1) 100.05 x 300 (12)", ts=1.0)
        check(f"order count parsed — {label}",
              [l.orders for l in p.bids] == [40, 30] and p.orders_seen,
              [l.orders for l in p.bids])

    unindexed = "Bids: 100.00 x 2000, 99.95 x 1500  Asks: 100.05 x 300, 100.10 x 250"
    p = parse_snap(unindexed, ts=1.0)
    check("unindexed levels, in order",
          [(l.price, l.qty) for l in p.bids] == [(100.00, 2000), (99.95, 1500)],
          p.bids)

    structured = ('{"ltp": 100.05, "ltq": 75, "total_buy_quantity": 12000, '
                  '"best_5_buy_data": [{"price": 100.0, "quantity": 2000, "orders": 40},'
                  '{"price": 99.95, "quantity": 1500, "orders": 30}], '
                  '"best_5_sell_data": [{"price": 100.05, "quantity": 300, "orders": 12}]}')
    p = parse_snap(structured, ts=1.0)
    check("structured SnapQuote JSON", len(p.bids) == 2 and p.bids[0].orders == 40,
          p.bids)
    check("LTQ from structured snap", p.ltq == 75, p.ltq)

    # The index's snap carries -0.01 sentinels rather than a real book — these
    # must be DROPPED, not treated as a 0.01 bid (which would look infinitely
    # bullish against an empty ask side).
    idx = "LTP 24500.5 Bids: 1) -0.01 x 0  2) -0.01 x 0  Asks: 1) -0.01 x 0"
    p = parse_snap(idx, ts=1.0)
    check("sentinel book rejected", p.bids == () and p.asks == (), (p.bids, p.asks))

    check("garbage yields empty book", parse_snap("nonsense", ts=1.0).bids == ())
    check("None yields empty book", parse_snap(None).bids == ())
    check("LTQ from text snap",
          parse_snap("LTP 100 LTQ 250 Bids: 1) 99 x 10", ts=1.0).ltq == 250)

    # Partial annotation: only some levels carry counts. The total must be
    # CONSERVATIVE (sum of what is known), never invented.
    part = parse_snap("Bids: 1) 100.00 x 2000 (40)  2) 99.95 x 1500  Asks: 1) 100.05 x 300",
                      ts=1.0)
    check("partial order counts sum conservatively", total_orders(part.bids) == 40,
          total_orders(part.bids))


# ── 2. W-OBI math ─────────────────────────────────────────────────────────────

def s2():
    section("2. weighted order book imbalance")
    b = bullish_book()
    w = DEFAULT_WEIGHTS
    exp_bids = 2000*1.0 + 1500*0.8 + 1200*0.6 + 800*0.4 + 600*0.2
    exp_asks = 300*1.0 + 250*0.8 + 200*0.6 + 150*0.4 + 100*0.2
    check("weighted bids = Σ qty×weight", weighted_depth(b.bids, w) == exp_bids,
          weighted_depth(b.bids, w))
    check("weighted asks = Σ qty×weight", weighted_depth(b.asks, w) == exp_asks,
          weighted_depth(b.asks, w))
    wb, wa, ratio = obi(b, w)
    check("ratio = bids ÷ asks", abs(ratio - exp_bids / exp_asks) < 1e-9, ratio)

    # Levels beyond the weight vector are IGNORED, not weighted 1.0 — shortening
    # the vector really does narrow the depth considered.
    short = (1.0, 0.5)
    check("weights shorter than book truncates",
          weighted_depth(b.bids, short) == 2000*1.0 + 1500*0.5,
          weighted_depth(b.bids, short))

    # An empty/zero ask side is an UNDEFINED ratio, not an infinite one: firing
    # into a book with no offers at all would be the worst possible fill.
    _, _, r_none = obi(book(bids=[(100, 500)], asks=[]), w)
    check("no ask depth → ratio None", r_none is None, r_none)

    check("weights parse", parse_weights("1.0,0.8,0.6") == (1.0, 0.8, 0.6))
    check("bad weights fall back to defaults",
          parse_weights("1.0,abc") == DEFAULT_WEIGHTS, parse_weights("1.0,abc"))
    check("weights list accepted", parse_weights([1.0, 0.5]) == (1.0, 0.5))


# ── 3. Order-count + anti-spoofing filters ────────────────────────────────────

def s3():
    section("3. order count + anti-spoofing")
    b = bullish_book()
    check("aggregate bid orders", total_orders(b.bids) == 40+30+25+20+15,
          total_orders(b.bids))
    check("depth-limited count", total_orders(b.bids, 2) == 70,
          total_orders(b.bids, 2))
    check("clean book has no single-ticket wall",
          single_ticket_level(b.bids, 2, 0.5) is None)

    # One ticket holding most of the displayed bid quantity = the classic pulled
    # wall. Level 1 and level 2 are both checked (the blueprint's requirement).
    spoof1 = book(bids=[(100.00, 9000, 1), (99.95, 500, 20)],
                  asks=[(100.05, 300, 12)])
    check("spoof at level 1 detected", single_ticket_level(spoof1.bids, 2, 0.5) == 1)
    spoof2 = book(bids=[(100.00, 500, 20), (99.95, 9000, 1)],
                  asks=[(100.05, 300, 12)])
    check("spoof at level 2 detected", single_ticket_level(spoof2.bids, 2, 0.5) == 2)
    check("depth 1 ignores a level-2 wall",
          single_ticket_level(spoof2.bids, 1, 0.5) is None)
    # orders == 1 but a SMALL slice of the book is just a small order, not a wall.
    small = book(bids=[(100.00, 100, 1), (99.95, 9000, 30)],
                 asks=[(100.05, 300, 12)])
    check("single small order is not a spoof",
          single_ticket_level(small.bids, 2, 0.5) is None)


# ── 4. Tape reading ───────────────────────────────────────────────────────────

def s4():
    section("4. tape classification and windowing")
    now = 1000.0
    tape = (
        TapeEvent(ts=now-1.0, price=100.05, qty=300, bid=100.00, ask=100.05),  # buy
        TapeEvent(ts=now-0.8, price=100.00, qty=100, bid=100.00, ask=100.05),  # sell
        TapeEvent(ts=now-0.6, price=100.03, qty=50,  bid=100.00, ask=100.05),  # inside
        TapeEvent(ts=now-0.4, price=100.06, qty=200, bid=100.00, ask=100.05),  # buy
    )
    ts = tape_stats(tape, now, 5.0)
    check("ask-hits counted as aggressive buys", ts["buy_qty"] == 500, ts["buy_qty"])
    check("bid-hits counted as sells", ts["sell_qty"] == 100, ts["sell_qty"])
    check("inside-spread prints are neutral", ts["total_qty"] == 650, ts["total_qty"])
    check("print count", ts["trades"] == 4, ts["trades"])
    check("buy ratio over directional volume only",
          abs(ts["buy_ratio"] - 500/600) < 1e-9, ts["buy_ratio"])
    check("velocity = buy qty ÷ window", ts["buy_velocity"] == 100.0,
          ts["buy_velocity"])

    old = tape_stats(tape, now + 10.0, 5.0)
    check("prints outside the window are excluded",
          old["trades"] == 0 and old["buy_qty"] == 0, old)

    # With no book recorded at print time, direction falls back to up/down ticks.
    nb = (TapeEvent(ts=now-1.0, price=100.00, qty=100, bid=0.0, ask=0.0),
          TapeEvent(ts=now-0.5, price=100.10, qty=200, bid=0.0, ask=0.0),
          TapeEvent(ts=now-0.2, price=100.05, qty=150, bid=0.0, ask=0.0))
    nbs = tape_stats(nb, now, 5.0)
    check("uptick/downtick fallback without a book",
          nbs["buy_qty"] == 200 and nbs["sell_qty"] == 150, nbs)

    t = ()
    for i in range(10):
        t = append_tape(t, TapeEvent(ts=float(i), price=100.0, qty=1, bid=0, ask=0), 4)
    check("tape is bounded by maxlen", len(t) == 4, len(t))
    check("tape keeps the NEWEST prints", [e.ts for e in t] == [6.0, 7.0, 8.0, 9.0],
          [e.ts for e in t])


# ── 5. Session controller ─────────────────────────────────────────────────────

def s5():
    section("5. time-of-day session controller")

    def prof_at(h, m):
        return session_profile(datetime(2026, 8, 17, h, m))

    with over(SCALP_ENABLED=True, SCALP_MIDDAY_ENABLED=True):
        cases = [
            (9, 0,  "closed",    False),
            (9, 14, "closed",    False),
            (9, 15, "warmup",    False),   # scanner only — no execution
            (9, 44, "warmup",    False),
            (9, 45, "morning",   True),
            (11, 29, "morning",  True),
            (11, 30, "midday",   True),
            (13, 29, "midday",   True),
            (13, 30, "afternoon", True),
            (14, 44, "afternoon", True),
            (14, 45, "squareoff", False),
            (15, 45, "squareoff", False),
        ]
        for h, m, window, execute in cases:
            p = prof_at(h, m)
            check(f"{h:02d}:{m:02d} → {window} (execute={execute})",
                  p.window == window and p.execute is execute,
                  f"{p.window}/{p.execute}")

        check("morning ratio 3.0", prof_at(10, 0).required_ratio == 3.0)
        check("midday ratio 5.0 (stricter)", prof_at(12, 0).required_ratio == 5.0)
        check("afternoon ratio 3.0", prof_at(14, 0).required_ratio == 3.0)
        # The non-executing windows score against the ratio the next executing
        # window uses, so their diagnostics show what would actually have fired.
        # A 0.0 threshold would pass every symbol and label a flat book a signal.
        check("warm-up scores at the morning ratio",
              prof_at(9, 30).required_ratio == 3.0, prof_at(9, 30).required_ratio)
        check("closed/square-off never report a 0.0 threshold",
              prof_at(8, 0).required_ratio == 3.0
              and prof_at(15, 0).required_ratio == 3.0,
              (prof_at(8, 0).required_ratio, prof_at(15, 0).required_ratio))

    with over(SCALP_ENABLED=True, SCALP_MIDDAY_ENABLED=False):
        p = prof_at(12, 0)
        check("midday pause blocks execution, keeps the window",
              p.window == "midday" and p.execute is False, f"{p.window}/{p.execute}")

    with over(SCALP_ENABLED=False):
        check("master switch off → never executes",
              prof_at(10, 0).execute is False)

    # Windows are dynamic settings, not constants.
    with over(SCALP_ENABLED=True, SCALP_MORNING_HOUR=10, SCALP_MORNING_MIN=0):
        check("window boundaries are runtime-editable",
              prof_at(9, 50).window == "warmup" and prof_at(10, 0).window == "morning",
              f"{prof_at(9, 50).window}/{prof_at(10, 0).window}")


# ── 6. evaluate() — the veto order ────────────────────────────────────────────

def s6():
    section("6. signal engine vetoes")
    now = time.monotonic()

    with over(SCALP_ENABLED=True, SCALP_MIN_LEVELS=3, SCALP_MIN_ORDER_COUNT=50,
              SCALP_MIN_TAPE_QTY=500.0, SCALP_MIN_TAPE_TRADES=3,
              SCALP_MIN_TAPE_BUY_RATIO=0.6, SCALP_REQUIRE_ASK_HIT=True):
        b, tape = bullish_book(ts=now), buy_tape(now, n=5, qty=200.0)
        d = evaluate(b, tape, now, 3.0, ltp=100.05)
        check("clean bullish book + tape passes", d.ok, d.reason)
        check("metrics carry the ratio", d.metrics["obiRatio"] > 3.0, d.metrics)

        check("no book → veto", not evaluate(None, tape, now, 3.0).ok)
        check("empty book → veto",
              not evaluate(book([], []), tape, now, 3.0).ok)

        stale = evaluate(bullish_book(ts=now - 30.0), tape, now, 3.0, 100.05)
        check("stale book → veto", not stale.ok and "stale" in stale.reason,
              stale.reason)

        thin = evaluate(book(bids=[(100.0, 5000, 40)], asks=[(100.05, 100, 10)],
                             ts=now), tape, now, 3.0, 100.05)
        check("too few levels → veto", not thin.ok and "thin book" in thin.reason,
              thin.reason)

        wide = book(bids=[(100.00, 2000, 40), (99.0, 1500, 30), (98.0, 1200, 25)],
                    asks=[(101.00, 300, 12), (102.0, 250, 10), (103.0, 200, 8)],
                    ts=now)
        dw = evaluate(wide, buy_tape(now, ask=101.0, bid=100.0), now, 3.0, 100.05)
        check("spread too wide → veto", not dw.ok and "spread" in dw.reason, dw.reason)

        # Balanced book: passes every other filter, fails only the ratio.
        bal = book(bids=[(100.00, 300, 40), (99.95, 250, 30), (99.90, 200, 25)],
                   asks=[(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8)],
                   ts=now)
        dr = evaluate(bal, tape, now, 3.0, 100.05)
        check("W-OBI below required → veto", not dr.ok and "W-OBI" in dr.reason,
              dr.reason)
        check("…and it reports both sides of the comparison",
              dr.metrics["obiRatio"] is not None
              and dr.metrics["requiredRatio"] == 3.0, dr.metrics)
        # The SAME book passes at a lower threshold — the ratio is the only reason.
        check("same book passes a lower threshold",
              evaluate(bal, tape, now, 0.9, 100.05).ok)

        few_orders = book(
            bids=[(100.00, 2000, 3), (99.95, 1500, 2), (99.90, 1200, 2)],
            asks=[(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8)], ts=now)
        do = evaluate(few_orders, tape, now, 3.0, 100.05)
        check("too few bid orders → veto", not do.ok and "bid orders" in do.reason,
              do.reason)

        spoof = book(bids=[(100.00, 9000, 1), (99.95, 1500, 30), (99.90, 1200, 25)],
                     asks=[(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8)],
                     ts=now)
        ds = evaluate(spoof, tape, now, 3.0, 100.05)
        check("single-ticket wall → veto", not ds.ok and "spoof" in ds.reason,
              ds.reason)

        # Feed publishes no order counts at all: fail-open by default, and
        # fail-closed when SCALP_REQUIRE_ORDER_DATA is on.
        no_ord = book(bids=[(100.00, 2000), (99.95, 1500), (99.90, 1200)],
                      asks=[(100.05, 300), (100.10, 250), (100.15, 200)], ts=now)
        check("missing order data tolerated by default",
              evaluate(no_ord, tape, now, 3.0, 100.05).ok)
        with over(SCALP_REQUIRE_ORDER_DATA=True):
            dn = evaluate(no_ord, tape, now, 3.0, 100.05)
            check("missing order data blocks when required",
                  not dn.ok and "order counts" in dn.reason, dn.reason)

        quiet = evaluate(b, buy_tape(now, n=1, qty=100.0), now, 3.0, 100.05)
        check("too few prints → veto", not quiet.ok and "quiet" in quiet.reason,
              quiet.reason)
        small = evaluate(b, buy_tape(now, n=5, qty=10.0), now, 3.0, 100.05)
        check("insufficient aggressive buy volume → veto",
              not small.ok and "buy volume" in small.reason, small.reason)

        # Heavy volume, but it is SELLERS hitting the bid — the exact case a
        # bid-heavy (spoofed) book would otherwise wave through.
        sell_tape = tuple(TapeEvent(ts=now - 0.1*i, price=100.00, qty=500,
                                    bid=100.00, ask=100.05) for i in range(5))
        dsell = evaluate(b, sell_tape, now, 3.0, 100.05)
        check("sell-side tape → veto", not dsell.ok, dsell.reason)

        # Nothing is currently paying the ask. The volume and buy-ratio floors are
        # relaxed to 0 so the ASK-HIT filter is provably the one that vetoes —
        # inside-spread prints count as neither buys nor sells, so those earlier
        # checks (which run first, by design) would otherwise fire instead.
        with over(SCALP_MIN_TAPE_BUY_RATIO=0.0, SCALP_MIN_TAPE_QTY=0.0,
                  SCALP_ASK_HIT_WINDOW_S=1.0):
            inside = tuple(TapeEvent(ts=now - 0.1*i, price=100.03, qty=500,
                                     bid=100.00, ask=100.05) for i in range(5))
            di = evaluate(b, inside, now, 3.0, 100.05)
            check("no recent print at the ask → veto",
                  not di.ok and "at the ask" in di.reason, di.reason)


# ── 7. Sizing, brackets and the cost buffer ───────────────────────────────────

def s7():
    section("7. sizing, brackets, cost buffer, slippage")
    now = time.monotonic()
    b   = bullish_book(ts=now)

    with over(SCALP_ENABLED=True, ACCOUNT_BALANCE=40_000.0, INTRADAY_LEVERAGE=5,
              SCALP_ALLOC_PCT=30.0, SCALP_RISK_MODE="fixed_amount",
              SCALP_RISK_PER_TRADE=200.0, SCALP_SL_PCT=0.25,
              SCALP_MIN_SL_OFFSET=0.10, SCALP_RR_RATIO=1.5,
              SCALP_COST_BUFFER_MULT=1.5, SCALP_ENTRY_AT_ASK=True,
              SLIPPAGE_BPS=2.0, SCALP_MAX_SLIPPAGE_PCT=0.10):
        sig, why = plan_entry("AAA", "AAA", b, ltp=100.05, available=40_000.0,
                              total_capital=40_000.0)
        check("signal produced", sig is not None, why)
        # Entry is priced at the ASK (a market buy crosses the spread), and the
        # signal carries the UNSLIPPED reference — place_paper_order slips it.
        check("entry priced at the ask", sig.ltp == 100.05, sig.ltp)
        fill = 100.05 * (1 + 2.0 / 10_000.0)
        exp_sl = round(max(fill * 0.25 / 100.0, 0.10), 2)
        check("stop = % of the projected fill", sig.sl_offset == exp_sl,
              f"{sig.sl_offset} vs {exp_sl}")
        check("target = stop × R:R", sig.target_offset == round(exp_sl * 1.5, 2),
              sig.target_offset)
        # Risk ceiling binds here: ₹200 ÷ ₹0.25 = 800 shares, while the
        # allocation ceiling would allow 30% × 40k × 5 ÷ 100 ≈ 599.
        qty_alloc = int((40_000.0 * 0.30 * 5) / fill)
        qty_risk  = int(200.0 / exp_sl)
        check("qty = min(allocation, risk) ceiling",
              sig.quantity == min(qty_alloc, qty_risk),
              f"{sig.quantity} vs min({qty_alloc},{qty_risk})")
        check("capital_needed accounts for leverage",
              abs(sig.capital_needed - (fill * sig.quantity) / 5) < 0.01,
              sig.capital_needed)
        check("diagnostics carried on the signal",
              sig.scalp["qtyAlloc"] == qty_alloc and sig.scalp["qtyRisk"] == qty_risk,
              sig.scalp)
        check("strategy tagged scalp", sig.strategy == "scalp", sig.strategy)

        # Allocation ceiling binds when free capital is nearly exhausted.
        sig2, _ = plan_entry("AAA", "AAA", b, ltp=100.05, available=200.0,
                             total_capital=40_000.0)
        check("allocation ceiling binds on low free capital",
              sig2 is not None and sig2.quantity == int((200.0 * 5) / fill),
              sig2.quantity if sig2 else None)
        s_none, why_none = plan_entry("AAA", "AAA", b, ltp=100.05, available=0.0,
                                      total_capital=40_000.0)
        check("no free capital → rejected",
              s_none is None and "free capital" in why_none, why_none)

        # Cost buffer: a target that doesn't clear round-trip costs × the
        # multiplier is refused — a "winner" that loses money after charges.
        with over(SCALP_RR_RATIO=0.1, SCALP_COST_BUFFER_MULT=5.0):
            s3_, why3 = plan_entry("AAA", "AAA", b, ltp=100.05, available=40_000.0,
                                   total_capital=40_000.0)
            check("cost-dominated target → rejected",
                  s3_ is None and "does not clear costs" in why3, why3)

        # Slippage tolerance: the ask has run away from the LTP that triggered us.
        runaway = book(bids=[(100.00, 2000, 40), (99.95, 1500, 30), (99.90, 1200, 25)],
                       asks=[(101.00, 300, 12), (101.05, 250, 10), (101.10, 200, 8)],
                       ts=now)
        s4_, why4 = plan_entry("AAA", "AAA", runaway, ltp=100.00, available=40_000.0,
                               total_capital=40_000.0)
        check("projected slippage beyond tolerance → rejected",
              s4_ is None and "slippage" in why4, why4)

        # capital_pct risk basis
        with over(SCALP_RISK_MODE="capital_pct", SCALP_RISK_CAPITAL_PERCENT=0.5):
            s5_, _ = plan_entry("AAA", "AAA", b, ltp=100.05, available=40_000.0,
                                total_capital=40_000.0)
            check("capital_pct risk basis sizes off equity %",
                  s5_ is not None and s5_.quantity == min(qty_alloc,
                                                          int(200.0 / exp_sl)),
                  s5_.quantity if s5_ else None)

        # Min stop floor protects a cheap stock from a sub-tick stop.
        cheap = book(bids=[(20.00, 5000, 40), (19.95, 4000, 30), (19.90, 3000, 25)],
                     asks=[(20.05, 500, 12), (20.10, 400, 10), (20.15, 300, 8)],
                     ts=now)
        with over(SCALP_SL_PCT=0.01, SCALP_MIN_SL_OFFSET=0.10):
            s6_, _ = plan_entry("BBB", "BBB", cheap, ltp=20.05, available=40_000.0,
                                total_capital=40_000.0)
            check("min stop distance floor applies",
                  s6_ is not None and s6_.sl_offset == 0.10,
                  s6_.sl_offset if s6_ else None)


# ── 8. Scalp risk gates ───────────────────────────────────────────────────────

def s8():
    section("8. scalp risk gates")
    with over(SCALP_MAX_CONCURRENT_POSITIONS=3, SCALP_MAX_TRADES_PER_SYMBOL=3,
              SCALP_MAX_TRADES_PER_DAY=20, SCALP_REENTRY_COOLDOWN_S=60.0,
              SCALP_DAILY_LOSS_LIMIT=1000.0, DAILY_LOSS_LIMIT=2000.0):
        base = dict(symbol="AAA", open_symbols=set(), scalp_open=0,
                    trades_symbol=0, trades_today=0, last_exit_ago=None,
                    daily_pnl=0.0, scalp_pnl=0.0)
        ok, why = can_enter_scalp(**base)
        check("clean state allows entry", ok, why)

        ok, why = can_enter_scalp(**{**base, "open_symbols": {"AAA"}})
        check("already open → blocked", not ok and "already has an open" in why, why)
        ok, why = can_enter_scalp(**{**base, "scalp_open": 3})
        check("concurrency cap → blocked", not ok and "concurrent scalps" in why, why)
        ok, why = can_enter_scalp(**{**base, "trades_symbol": 3})
        check("per-symbol churn cap → blocked", not ok and "daily cap" in why, why)
        ok, why = can_enter_scalp(**{**base, "trades_today": 20})
        check("per-day cap → blocked",
              not ok and "Daily scalp trade cap" in why, why)
        ok, why = can_enter_scalp(**{**base, "last_exit_ago": 10.0})
        check("re-entry cooldown → blocked", not ok and "cooldown" in why, why)
        ok, why = can_enter_scalp(**{**base, "last_exit_ago": 61.0})
        check("re-entry allowed after cooldown", ok, why)
        # The defining difference from the core strategy: re-entry IS permitted.
        ok, why = can_enter_scalp(**{**base, "trades_symbol": 1,
                                     "last_exit_ago": 120.0})
        check("same symbol re-entered later in the day", ok, why)
        ok, why = can_enter_scalp(**{**base, "scalp_pnl": -1000.0})
        check("scalp loss limit → blocked", not ok and "Scalp daily loss" in why, why)
        ok, why = can_enter_scalp(**{**base, "daily_pnl": -2000.0})
        check("ACCOUNT loss limit also stops the scalper",
              not ok and "Account daily loss" in why, why)


# ── 9. Settings validation ────────────────────────────────────────────────────

def s9():
    section("9. settings validation")
    import app.services.settings as S

    check("SPEC covers every scalp default",
          all(k in S._ATTR_SPEC for k in cfg.dynamic_defaults() if k.startswith("SCALP_")))
    check("no scalp key is backtest-overridable",
          all(not S._BY_KEY[k]["bt"] for k in S._BY_KEY if k.startswith("SCALP_")),
          [k for k in S._BY_KEY if k.startswith("SCALP_") and S._BY_KEY[k]["bt"]])

    S.validate_obi_weights({"SCALP_OBI_WEIGHTS": "1.0,0.8,0.6,0.4,0.2"})
    check("valid weight vector accepted", True)
    for bad, label in (("", "empty"), ("1,2,3,4,5,6", "too many"),
                       ("1.0,abc", "non-numeric"), ("0,0,0", "all zero"),
                       ("1.0,-0.5", "negative")):
        try:
            S.validate_obi_weights({"SCALP_OBI_WEIGHTS": bad})
            check(f"weights rejected — {label}", False, f"{bad!r} accepted")
        except ValueError:
            check(f"weights rejected — {label}", True)

    # The scalper's window chain is validated independently of the live session's.
    try:
        S.validate_time_order({"SCALP_MIDDAY_HOUR": 9, "SCALP_MIDDAY_MIN": 0})
        check("inverted scalp windows rejected", False, "accepted")
    except ValueError as e:
        check("inverted scalp windows rejected", "scalp" in str(e), str(e))
    try:
        S.validate_time_order({"SCALP_SQUAREOFF_HOUR": 13, "SCALP_SQUAREOFF_MIN": 30})
        check("zero-width afternoon window rejected", False, "accepted")
    except ValueError as e:
        check("zero-width afternoon window rejected", True, str(e))
    S.validate_time_order({"SCALP_SQUAREOFF_HOUR": 15, "SCALP_SQUAREOFF_MIN": 0})
    check("valid scalp window change accepted", True)
    # Editing a scalp window must not drag the live session chain into the check.
    S.validate_time_order({"SCALP_WARMUP_HOUR": 9, "SCALP_WARMUP_MIN": 20})
    check("scalp edit does not touch the live session chain", True)


# ── 10. End-to-end: the same book with each knob turned ───────────────────────

def s10():
    section("10. end-to-end signal through a whole configured window")
    now = time.monotonic()

    with over(SCALP_ENABLED=True, SCALP_MIDDAY_ENABLED=True,
              ACCOUNT_BALANCE=40_000.0, INTRADAY_LEVERAGE=5,
              SCALP_ALLOC_PCT=30.0, SCALP_RISK_PER_TRADE=200.0,
              SCALP_SL_PCT=0.25, SCALP_RR_RATIO=1.5, SLIPPAGE_BPS=2.0):
        # A MODERATELY imbalanced book — weighted ratio ≈ 4.0, deliberately
        # between the morning (3.0) and midday (5.0) thresholds, so the same book
        # demonstrates both outcomes. (bullish_book's 6.2 would clear both and
        # prove nothing about the time-of-day filter.)
        b = book(bids=[(100.00, 1300, 40), (99.95, 950, 30), (99.90, 750, 25),
                       (99.85, 500, 20), (99.80, 400, 15)],
                 asks=[(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8),
                       (100.20, 150, 6), (100.25, 100, 5)],
                 ts=now, ltp=100.05)
        tape  = buy_tape(now, n=5, qty=200.0)
        ratio = obi(b, parse_weights(cfg.SCALP_OBI_WEIGHTS))[2]
        check(f"test book sits between the two thresholds (ratio {ratio:.2f})",
              3.0 <= ratio < 5.0, ratio)

        # Morning (3.0): this book's ratio clears it → signal + sized order.
        morning = session_profile(datetime(2026, 8, 17, 10, 0))
        d = evaluate(b, tape, now, morning.required_ratio, 100.05)
        sig, why = (plan_entry("AAA", "AAA", b, 100.05, 40_000.0, 40_000.0,
                               d.metrics) if d.ok else (None, d.reason))
        check(f"morning window fires (ratio {ratio:.2f} ≥ 3.0)",
              d.ok and sig is not None and morning.execute, why or "")

        # Midday (5.0): the SAME book is rejected by the stricter threshold —
        # the time-of-day filter working end to end.
        midday = session_profile(datetime(2026, 8, 17, 12, 0))
        dm = evaluate(b, tape, now, midday.required_ratio, 100.05)
        check(f"same book rejected at midday (needs {midday.required_ratio:g})",
              (ratio < 5.0) and not dm.ok and "W-OBI" in dm.reason, dm.reason)

        # Warm-up: the book still evaluates (scanner), but execution is barred.
        warm = session_profile(datetime(2026, 8, 17, 9, 30))
        dw = evaluate(b, tape, now, warm.required_ratio, 100.05)
        check("warm-up scans but cannot execute", dw.ok and not warm.execute)

        # Square-off: no execution at all.
        check("square-off window bars execution",
              not session_profile(datetime(2026, 8, 17, 14, 46)).execute)


# ── 11. ScalpEngine end to end (no DB, no market feed) ────────────────────────

def s11():
    section("11. ScalpEngine cycle: fill → tag → time stop → square-off")
    import asyncio

    from app.models import STRATEGY_CORE, STRATEGY_SCALP
    from app.services.scalp_engine import ScalpEngine
    from app.state import get_state

    st = get_state()

    saved, exited = [], []

    async def fake_save(pos):
        saved.append(pos)

    async def fake_exit(pos):
        exited.append(pos)

    def fresh_state():
        """Minimal AppState for the engine: one tradeable symbol with a live
        book, tape and price. Nothing else the engine reads is left over."""
        st.positions.clear(); st.closed_positions.clear()
        st.traded_today.clear()
        st.reset_scalp_state()
        st.daily_pnl = 0.0
        st.active_watchlist = {"AAA": "AAA"}
        st.full_watchlist   = {"AAA": "AAA"}
        st.token_to_name    = {"AAA": "AAA"}
        st.ltp = {"AAA": 100.05}
        now = time.monotonic()
        st.book = {"AAA": bullish_book(ts=now)}
        st.tape = {"AAA": buy_tape(now, n=5, qty=200.0)}
        st.dirty_ticks_scalp = {"AAA"}
        saved.clear(); exited.clear()
        return ScalpEngine(queue_entry_save=fake_save, write_exit=fake_exit)

    # (a) Armed, inside the morning window → a real (paper) fill, tagged scalp.
    with over(SCALP_ENABLED=True, SCALP_DRY_RUN=False,
              SCALP_MORNING_HOUR=0, SCALP_MORNING_MIN=0,      # window = "now"
              SCALP_WARMUP_HOUR=0, SCALP_WARMUP_MIN=0,
              SCALP_MIDDAY_HOUR=23, SCALP_MIDDAY_MIN=58,
              SCALP_AFTERNOON_HOUR=23, SCALP_AFTERNOON_MIN=58,
              SCALP_SQUAREOFF_HOUR=23, SCALP_SQUAREOFF_MIN=59,
              SCALP_RATIO_MORNING=3.0, ACCOUNT_BALANCE=40_000.0,
              INTRADAY_LEVERAGE=5, SCALP_MAX_HOLD_S=300.0):
        eng = fresh_state()
        asyncio.run(eng.tick())
        check("entry placed", "AAA" in st.positions, list(st.positions))
        pos = st.positions.get("AAA")
        check("position tagged scalp", pos and pos.strategy == STRATEGY_SCALP,
              pos.strategy if pos else None)
        check("bracket set from the signal",
              pos and pos.stop_loss < pos.entry_price < pos.target,
              (pos.stop_loss, pos.entry_price, pos.target) if pos else None)
        check("fill persisted through the shared queue", len(saved) == 1, len(saved))
        check("fill counter advanced", eng.fills == 1, eng.fills)
        check("per-symbol trade count recorded",
              st.scalp_trades_today.get("AAA") == 1, st.scalp_trades_today)

        # Second cycle: the symbol is already open, so no duplicate entry.
        st.dirty_ticks_scalp = {"AAA"}
        asyncio.run(eng.tick())
        check("no duplicate entry while open", len(saved) == 1, len(saved))

        # `signals` must be EDGE-triggered. The book stays imbalanced across
        # cycles, so a level-triggered counter would add one per symbol per cycle
        # (~10/s) and report thousands of "signals" for a single setup.
        eng_s = fresh_state()
        with over(SCALP_DRY_RUN=True):          # dry run: nothing opens, so the
            for _ in range(5):                  # same setup keeps qualifying
                st.dirty_ticks_scalp = {"AAA"}
                asyncio.run(eng_s.tick())
            check("one setup counts as ONE signal across many cycles",
                  eng_s.signals == 1, eng_s.signals)
            check("evaluations still count every cycle",
                  eng_s.evaluated == 5, eng_s.evaluated)
            # Once it stops qualifying and comes back, it is a NEW setup.
            st.tape = {}                        # kills the tape confirmation
            st.dirty_ticks_scalp = {"AAA"}
            asyncio.run(eng_s.tick())
            check("a failing symbol releases the latch", eng_s.signals == 1,
                  eng_s.signals)
            st.tape = {"AAA": buy_tape(time.monotonic(), n=5, qty=200.0)}
            st.book = {"AAA": bullish_book(ts=time.monotonic())}
            st.dirty_ticks_scalp = {"AAA"}
            asyncio.run(eng_s.tick())
            check("re-qualifying counts as a new signal", eng_s.signals == 2,
                  eng_s.signals)

        # (b) Time stop — the position is aged past SCALP_MAX_HOLD_S.
        eng = fresh_state()
        asyncio.run(eng.tick())
        st.positions["AAA"].opened_at = time.monotonic() - 10_000.0
        st.dirty_ticks_scalp = set()
        asyncio.run(eng.tick())
        check("time stop flattens a stale scalp", "AAA" not in st.positions,
              list(st.positions))
        check("time-stop exit persisted", len(exited) == 1, len(exited))
        check("scalp P&L booked separately", st.scalp_pnl != 0.0, st.scalp_pnl)
        check("cooldown timestamp recorded", "AAA" in st.scalp_last_exit,
              st.scalp_last_exit)
        check("closed trade moved to the day log",
              len(st.closed_positions) == 1
              and st.closed_positions[0].strategy == STRATEGY_SCALP)

        # (c) Dry run places nothing, but still logs a signal.
        with over(SCALP_DRY_RUN=True):
            eng2 = fresh_state()
            asyncio.run(eng2.tick())
            check("dry run places no order", not st.positions and not saved,
                  list(st.positions))
            check("dry run still logs the signal",
                  any(e.get("mode") == "dry_run" for e in st.scalp_log),
                  list(st.scalp_log))

        # (d) A CORE position is never touched by the scalper's management.
        eng3 = fresh_state()
        from app.services.paper_trade import place_paper_order
        place_paper_order(symbol="AAA", token="AAA", quantity=10,
                          entry_price=100.0, sl_offset=1.0, target_offset=2.0,
                          strategy=STRATEGY_CORE)
        st.positions["AAA"].opened_at = time.monotonic() - 10_000.0
        st.dirty_ticks_scalp = set()
        asyncio.run(eng3.tick())
        check("core position untouched by the scalp time stop",
              "AAA" in st.positions and not exited, list(st.positions))

    # (e) Square-off window flattens the scalp book (and only it).
    with over(SCALP_ENABLED=True, SCALP_DRY_RUN=False,
              SCALP_WARMUP_HOUR=0, SCALP_WARMUP_MIN=0,
              SCALP_MORNING_HOUR=0, SCALP_MORNING_MIN=1,
              SCALP_MIDDAY_HOUR=0, SCALP_MIDDAY_MIN=2,
              SCALP_AFTERNOON_HOUR=0, SCALP_AFTERNOON_MIN=3,
              SCALP_SQUAREOFF_HOUR=0, SCALP_SQUAREOFF_MIN=4):
        eng4 = fresh_state()
        from app.services.paper_trade import place_paper_order
        place_paper_order(symbol="AAA", token="AAA", quantity=10,
                          entry_price=100.0, sl_offset=1.0, target_offset=2.0,
                          strategy=STRATEGY_SCALP)
        place_paper_order(symbol="BBB", token="BBB", quantity=10,
                          entry_price=50.0, sl_offset=1.0, target_offset=2.0,
                          strategy=STRATEGY_CORE)
        st.ltp["BBB"] = 50.0
        asyncio.run(eng4.tick())
        check("square-off flattens the scalp position", "AAA" not in st.positions,
              list(st.positions))
        check("square-off leaves core positions to the 15:30 EOD flat",
              "BBB" in st.positions, list(st.positions))
        check("square-off exit persisted", len(exited) == 1, len(exited))

    # (f) Master switch off: the dirty set is drained and nothing is evaluated.
    with over(SCALP_ENABLED=False):
        eng5 = fresh_state()
        asyncio.run(eng5.tick())
        check("disabled engine evaluates nothing",
              eng5.evaluated == 0 and not st.dirty_ticks_scalp and not st.positions,
              (eng5.evaluated, st.dirty_ticks_scalp))

    # Leave no test state behind for anything else in the process.
    st.positions.clear(); st.closed_positions.clear(); st.traded_today.clear()
    st.reset_scalp_state()
    st.daily_pnl = 0.0
    st.active_watchlist, st.full_watchlist, st.token_to_name = {}, {}, {}
    st.ltp = {}


# ── 11b. Capital invariants across sequential fills ───────────────────────────

def s11b():
    section("11b. shared-account capital invariants")
    import asyncio

    from app.engine.orderbook import append_tape
    from app.models import STRATEGY_CORE, STRATEGY_SCALP, TapeEvent
    from app.services.paper_trade import place_paper_order
    from app.services.scalp_engine import ScalpEngine
    from app.state import get_state

    st = get_state()

    async def noop(_p):
        pass

    SYMS = ["S1", "S2", "S3", "S4", "S5"]

    def seed():
        # reset FIRST — reset_scalp_state() clears book/tape, so seeding before it
        # silently produces an empty book and zero fills.
        st.positions.clear(); st.closed_positions.clear(); st.traded_today.clear()
        st.reset_scalp_state()
        st.daily_pnl = 0.0
        now = time.monotonic()
        st.active_watchlist = {s: s for s in SYMS}
        st.token_to_name    = {s: s for s in SYMS}
        st.ltp   = {s: 100.05 for s in SYMS}
        # Very deep bid side so the imbalance passes comfortably and ALLOCATION,
        # not the signal, is what limits the number of fills.
        deep = book(bids=[(100.00, 20000, 40), (99.95, 15000, 30), (99.90, 12000, 25)],
                    asks=[(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8)],
                    ts=now, ltp=100.05)
        st.book = {s: deep for s in SYMS}
        tape = ()
        for i in range(5):
            tape = append_tape(tape, TapeEvent(ts=now - 0.1 * i, price=100.05,
                                               qty=400, bid=100.0, ask=100.05), 40)
        st.tape = {s: tape for s in SYMS}
        st.dirty_ticks_scalp = set(SYMS)
        return ScalpEngine(queue_entry_save=noop, write_exit=noop)

    # A huge risk cap makes the ALLOCATION ceiling the binding constraint, which is
    # what exercises the per-fill `available` decrement.
    base = dict(SCALP_ENABLED=True, SCALP_DRY_RUN=False, ACCOUNT_BALANCE=40_000.0,
                INTRADAY_LEVERAGE=5, SCALP_ALLOC_PCT=30.0,
                SCALP_MAX_CONCURRENT_POSITIONS=5, SCALP_RISK_PER_TRADE=100_000.0,
                SCALP_WARMUP_HOUR=0, SCALP_WARMUP_MIN=0,
                SCALP_MORNING_HOUR=0, SCALP_MORNING_MIN=0,
                SCALP_MIDDAY_HOUR=23, SCALP_MIDDAY_MIN=57,
                SCALP_AFTERNOON_HOUR=23, SCALP_AFTERNOON_MIN=58,
                SCALP_SQUAREOFF_HOUR=23, SCALP_SQUAREOFF_MIN=59)

    with over(**base):
        eng = seed()
        asyncio.run(eng.tick())
        lev      = 5
        notional = sum(p.entry_price * p.quantity for p in st.positions.values())
        margin   = notional / lev
        check("multiple candidates fill in one cycle", len(st.positions) >= 2,
              len(st.positions))
        # THE invariant: both strategies draw on one account, so committed margin
        # can never exceed equity however many scalps fire in a single cycle. This
        # is what the per-fill `available -= capital_needed` exists to guarantee —
        # without it every candidate would size against the full balance.
        check(f"committed margin ₹{margin:,.0f} never exceeds equity ₹40,000",
              margin <= 40_000.0 + 1e-6, margin)
        check(f"notional ₹{notional:,.0f} never exceeds equity × leverage",
              notional <= 40_000.0 * lev + 1e-6, notional)
        check("capital is actually used up (not left mostly idle)",
              margin > 40_000.0 * 0.9, margin)
        # The last affordable fill is DOWNSIZED rather than skipped or oversized.
        qtys = sorted(p.quantity for p in st.positions.values())
        check("the final fill is downsized to the remaining capital",
              qtys[0] < qtys[-1], qtys)

        # Now with capital already committed by the CORE strategy: the scalper must
        # size against what's LEFT, not the full balance.
        eng2 = seed()
        place_paper_order(symbol="CORE1", token="CORE1", quantity=1500,
                          entry_price=100.0, sl_offset=1.0, target_offset=2.0,
                          strategy=STRATEGY_CORE)          # ₹150k notional = ₹30k margin
        st.dirty_ticks_scalp = set(SYMS)
        asyncio.run(eng2.tick())
        notional2 = sum(p.entry_price * p.quantity for p in st.positions.values())
        margin2   = notional2 / lev
        scalps    = [p for p in st.positions.values() if p.strategy == STRATEGY_SCALP]
        check(f"scalper respects core-committed capital (total margin ₹{margin2:,.0f})",
              margin2 <= 40_000.0 + 1e-6, margin2)
        check("…and still trades what capital remains",
              len(scalps) >= 1, len(scalps))

    st.positions.clear(); st.closed_positions.clear(); st.traded_today.clear()
    st.reset_scalp_state()
    st.daily_pnl = 0.0
    st.active_watchlist = {}; st.token_to_name = {}; st.ltp = {}


# ── 12. Tape ingestion from raw ticks (regressions) ───────────────────────────

def s12():
    section("12. tape ingestion — volume delta and LTQ de-duplication")
    try:
        from app.services.market_data import MarketDataService
    except ImportError as e:      # websockets/httpx absent — keep the suite portable
        print(f"  -- skipped (market_data unavailable: {e})")
        return

    from app.models import TradingPhase
    from app.state import get_state

    st = get_state()

    def tick(start_time, volume, ltp=100.05, snap=None):
        return {"stock_symbol": "AAA", "stockname": "AAA", "interval": "5m",
                "start_time": start_time, "open": 100.0, "close": ltp,
                "high": 100.2, "low": 99.9, "volume": volume,
                "ltp": str(ltp), "snap": snap if snap is not None else SNAP}

    SNAP = ("LTP 100.05 BuyQty 12000 SellQty 4000 "
            "Bids: 1) 100.00 x 2000 (40) 2) 99.95 x 1500 (30) "
            "Asks: 1) 100.05 x 300 (12) 2) 100.10 x 250 (10)")

    def fresh():
        st.candles_5m.clear(); st.tick_version.clear()
        st.reset_scalp_state()
        st.ltp = {}
        st.active_watchlist = {"AAA": "AAA"}
        st.token_to_name    = {"AAA": "AAA"}
        st.phase = TradingPhase.ACTIVE
        return MarketDataService()

    def qtys():
        return [e.qty for e in st.tape.get("AAA", ())]

    with over(SCALP_ENABLED=True, SCALP_TAPE_MAXLEN=40):
        mkt = fresh()

        # First sighting records a baseline only — emitting the forming bar's
        # whole accumulated volume would fake a startup burst.
        mkt._process_tick(tick("2026-08-17 09:15:00", 1000))
        check("first tick books no tape print", qtys() == [], qtys())
        check("book parsed for a tradeable symbol",
              "AAA" in st.book and len(st.book["AAA"].bids) == 2)
        check("scalp dirty set populated", st.dirty_ticks_scalp == {"AAA"},
              st.dirty_ticks_scalp)

        # Same bar, volume grew → exactly the delta is one print.
        mkt._process_tick(tick("2026-08-17 09:15:00", 1500))
        check("volume delta becomes one print", qtys() == [500.0], qtys())

        # REGRESSION: a stale out-of-order bar (reconnect replay) must NOT emit a
        # print, and must NOT rebase the baseline backwards.
        mkt._process_tick(tick("2026-08-17 09:10:00", 9999))
        check("stale replayed bar emits no print", qtys() == [500.0], qtys())
        mkt._process_tick(tick("2026-08-17 09:15:00", 1600))
        check("baseline survived the stale bar (delta 100, not 1600)",
              qtys() == [500.0, 100.0], qtys())

        # A genuinely new bar contributes its whole volume.
        mkt._process_tick(tick("2026-08-17 09:20:00", 300))
        check("new bar contributes its full volume",
              qtys() == [500.0, 100.0, 300.0], qtys())

        # Identical snap must still refresh the book timestamp (a quiet but live
        # book must not age into the staleness veto).
        ts_before = st.book["AAA"].ts
        mkt._process_tick(tick("2026-08-17 09:20:00", 400))
        check("unchanged snap re-confirms the book timestamp",
              st.book["AAA"].ts >= ts_before)

        # REGRESSION: LTQ is a LEVEL, not a delta. With no bar volume at all, the
        # same LTQ re-read on every tick must be counted ONCE — otherwise a single
        # 500-share print fabricates thousands of shares of "aggressive buying"
        # per second and fires entries on nothing.
        mkt2 = fresh()
        ltq_snap = SNAP + " LTQ 250"
        for _ in range(5):
            mkt2._process_tick(tick("2026-08-17 09:15:00", 0, snap=ltq_snap))
        check("repeated identical LTQ counted once", qtys() == [250.0], qtys())
        mkt2._process_tick(tick("2026-08-17 09:15:00", 0,
                                snap=SNAP + " LTQ 900"))
        check("a CHANGED LTQ is a new print", qtys() == [250.0, 900.0], qtys())

        # Non-tradeable symbols cost nothing: no book, no tape.
        st.active_watchlist = {}
        mkt3 = MarketDataService()
        st.book.clear(); st.tape.clear()
        mkt3._process_tick(tick("2026-08-17 09:25:00", 5000))
        check("untradeable symbol builds no book/tape",
              not st.book and not st.tape, (st.book, st.tape))

    # With the scalper OFF the parse is skipped entirely.
    with over(SCALP_ENABLED=False):
        mkt4 = fresh()
        mkt4._process_tick(tick("2026-08-17 09:15:00", 1000))
        mkt4._process_tick(tick("2026-08-17 09:15:00", 2000))
        check("disabled scalper builds no book/tape",
              not st.book and not st.tape, (st.book, st.tape))
        check("legacy L1 depth still parsed when scalper is off",
              st.depth.get("AAA", {}).get("bid") == 100.00, st.depth.get("AAA"))

    st.candles_5m.clear(); st.tick_version.clear(); st.depth.clear()
    st.reset_scalp_state()
    st.active_watchlist = {}; st.token_to_name = {}; st.ltp = {}


# ── 13. Engine-level regressions ──────────────────────────────────────────────

def s13():
    section("13. engine regressions — disable, unpriced flatten, diagnostics")
    import asyncio

    from app.models import STRATEGY_SCALP
    from app.services.paper_trade import place_paper_order
    from app.services.scalp_engine import ScalpEngine, _bucket
    from app.state import get_state

    st = get_state()
    exited = []

    async def fake_save(pos):
        pass

    async def fake_exit(pos):
        exited.append(pos)

    def fresh():
        st.positions.clear(); st.closed_positions.clear(); st.traded_today.clear()
        st.reset_scalp_state()
        st.daily_pnl = 0.0
        st.active_watchlist = {"AAA": "AAA"}
        st.token_to_name    = {"AAA": "AAA"}
        st.ltp = {"AAA": 100.05}
        exited.clear()
        return ScalpEngine(queue_entry_save=fake_save, write_exit=fake_exit)

    # REGRESSION: turning the master switch OFF must stop new risk without
    # abandoning open risk — the time stop still has to fire.
    with over(SCALP_ENABLED=False, SCALP_MAX_HOLD_S=60.0,
              SCALP_WARMUP_HOUR=0, SCALP_WARMUP_MIN=0,
              SCALP_MORNING_HOUR=0, SCALP_MORNING_MIN=1,
              SCALP_MIDDAY_HOUR=23, SCALP_MIDDAY_MIN=57,
              SCALP_AFTERNOON_HOUR=23, SCALP_AFTERNOON_MIN=58,
              SCALP_SQUAREOFF_HOUR=23, SCALP_SQUAREOFF_MIN=59):
        eng = fresh()
        place_paper_order(symbol="AAA", token="AAA", quantity=10,
                          entry_price=100.0, sl_offset=1.0, target_offset=2.0,
                          strategy=STRATEGY_SCALP)
        st.positions["AAA"].opened_at = time.monotonic() - 10_000.0
        asyncio.run(eng.tick())
        check("disabled engine still time-stops an open scalp",
              "AAA" not in st.positions and len(exited) == 1,
              (list(st.positions), len(exited)))

    # REGRESSION: the square-off must NOT fabricate a fill price when the feed
    # has no live price — EOD closes those at a REST-fetched real price instead.
    with over(SCALP_ENABLED=True, SCALP_WARMUP_HOUR=0, SCALP_WARMUP_MIN=0,
              SCALP_MORNING_HOUR=0, SCALP_MORNING_MIN=1,
              SCALP_MIDDAY_HOUR=0, SCALP_MIDDAY_MIN=2,
              SCALP_AFTERNOON_HOUR=0, SCALP_AFTERNOON_MIN=3,
              SCALP_SQUAREOFF_HOUR=0, SCALP_SQUAREOFF_MIN=4):
        eng2 = fresh()
        place_paper_order(symbol="AAA", token="AAA", quantity=10,
                          entry_price=100.0, sl_offset=1.0, target_offset=2.0,
                          strategy=STRATEGY_SCALP)
        st.ltp.pop("AAA")                      # feed went silent for this symbol
        asyncio.run(eng2.tick())
        check("unpriced scalp is NOT closed at a fabricated price",
              "AAA" in st.positions and not exited, (list(st.positions), exited))
        st.ltp["AAA"] = 100.05                 # price returns → it flattens
        asyncio.run(eng2.tick())
        check("…and flattens as soon as a real price exists",
              "AAA" not in st.positions and len(exited) == 1,
              (list(st.positions), len(exited)))

        # REGRESSION: never enter a position whose stop can't be monitored.
        eng3 = fresh()
        now = time.monotonic()
        st.book = {"AAA": bullish_book(ts=now)}
        st.tape = {"AAA": buy_tape(now, n=5, qty=200.0)}
        st.dirty_ticks_scalp = {"AAA"}
        st.ltp = {}                            # no live price at all
        with over(SCALP_DRY_RUN=False, SCALP_MORNING_HOUR=0, SCALP_MORNING_MIN=0,
                  SCALP_MIDDAY_HOUR=23, SCALP_MIDDAY_MIN=57,
                  SCALP_AFTERNOON_HOUR=23, SCALP_AFTERNOON_MIN=58,
                  SCALP_SQUAREOFF_HOUR=23, SCALP_SQUAREOFF_MIN=59):
            asyncio.run(eng3.tick())
        check("no entry without a live price to monitor the stop",
              not st.positions, list(st.positions))

    # REGRESSION: rejection buckets must AGGREGATE — live values and symbol names
    # must not fragment them into one bucket per symbol.
    check("numeric detail collapses",
          _bucket("AAA", "W-OBI 1.80 < 3.0") == _bucket("BBB", "W-OBI 2.40 < 3.0"),
          (_bucket("AAA", "W-OBI 1.80 < 3.0"), _bucket("BBB", "W-OBI 2.40 < 3.0")))
    check("symbol name collapses",
          _bucket("AAA", "AAA in re-entry cooldown (43s left)")
          == _bucket("BBB", "BBB in re-entry cooldown (7s left)"),
          _bucket("AAA", "AAA in re-entry cooldown (43s left)"))
    check("distinct reasons stay distinct",
          _bucket("AAA", "W-OBI N < N") != _bucket("AAA", "stale book (3s)"))

    eng4 = fresh()
    eng4._note_reject("AAA", "W-OBI 1.80 < 3.0")
    eng4._note_reject("BBB", "W-OBI 2.40 < 3.0")
    eng4._note_reject("CCC", "stale book (12.3s)")
    summary = eng4.reject_summary()
    check("summary aggregates across symbols",
          summary[0]["symbols"] == 2 and len(summary) == 2, summary)

    st.positions.clear(); st.closed_positions.clear(); st.traded_today.clear()
    st.reset_scalp_state()
    st.daily_pnl = 0.0
    st.active_watchlist = {}; st.token_to_name = {}; st.ltp = {}


# ── 14. Scanner endpoint + threadpool safety (regressions) ────────────────────

def s14():
    section("14. /api/scalp/scan — ordering, filtering, threadpool safety")
    try:
        import app.api.dashboard as api      # pulls in the backtest engine (numpy/talib)
    except ImportError as e:
        print(f"  -- skipped (dashboard API unavailable: {e})")
        return

    import threading

    from app.engine.orderbook import parse_snap
    from app.models import STRATEGY_SCALP, Position
    from app.services.paper_trade import place_paper_order
    from app.services.scalp_engine import ScalpEngine
    from app.state import get_state

    BULL = ("LTP 100.05 BuyQty 12000 SellQty 4000 "
            "Bids: 1) 100.00 x 2000 (40) 2) 99.95 x 1500 (30) 3) 99.90 x 1200 (25) "
            "Asks: 1) 100.05 x 300 (12) 2) 100.10 x 250 (10) 3) 100.15 x 200 (8)")
    BEAR = ("LTP 50.05 BuyQty 100 SellQty 900 "
            "Bids: 1) 50.00 x 200 (90) 2) 49.95 x 150 (80) 3) 49.90 x 100 (70) "
            "Asks: 1) 50.05 x 900 (30) 2) 50.10 x 800 (25) 3) 50.15 x 700 (20)")

    st  = get_state()
    eng = ScalpEngine(queue_entry_save=None, write_exit=None)

    class FakeMkt:
        _last_snap = {"AAA": BULL, "BBB": BEAR}

    class FakeSched:
        _mkt  = FakeMkt()
        scalp = eng

    api.set_services(None, FakeSched())

    st.positions.clear(); st.traded_today.clear()
    st.reset_scalp_state()
    st.active_watchlist = {"AAA": "AAA", "BBB": "BBB"}
    st.token_to_name    = {"AAA": "AAA", "BBB": "BBB"}
    st.ltp  = {"AAA": 100.05, "BBB": 50.05}
    now = time.monotonic()
    st.book = {"AAA": parse_snap(BULL, ts=now), "BBB": parse_snap(BEAR, ts=now)}
    st.tape = {"AAA": buy_tape(now, n=5, qty=200.0)}

    with over(SCALP_ENABLED=True, SCALP_RATIO_MORNING=3.0):
        d = api.scalp_scan()
        check("every tradeable symbol is scanned", len(d["rows"]) == 2, len(d["rows"]))
        check("strongest imbalance sorts first — same order the engine fills in",
              d["rows"][0]["symbol"] == "AAA", [r["symbol"] for r in d["rows"]])
        check("the bullish book signals", d["rows"][0]["ok"] is True,
              d["rows"][0]["reason"])
        check("the sell-skewed book does not", d["rows"][1]["ok"] is False)
        check("rejection reason is carried per symbol",
              "W-OBI" in d["rows"][1]["reason"] or "bid orders" in d["rows"][1]["reason"],
              d["rows"][1]["reason"])
        check("only_passing filters to signals",
              [r["symbol"] for r in api.scalp_scan(only_passing=True)["rows"]] == ["AAA"])
        check("response reports the arm/window state",
              d["enabled"] is True and "window" in d and d["tradeable"] == 2, d["window"])

        # An open scalp is tagged so the page can shade the row.
        place_paper_order(symbol="AAA", token="AAA", quantity=5, entry_price=100.0,
                          sl_offset=0.5, target_offset=1.0, strategy=STRATEGY_SCALP)
        held = {r["symbol"]: r["held"] for r in api.scalp_scan()["rows"]}
        check("open scalp positions are tagged in the scan",
              held["AAA"] == "scalp" and held["BBB"] is None, held)
        st.positions.clear()

        # REGRESSION: these endpoints are SYNC defs, so FastAPI serves them from a
        # threadpool while the engine mutates the same dicts on the event loop.
        # Iterating them unguarded raises "dictionary changed size during
        # iteration"; every such read loop must snapshot with list() first.
        #
        # Two things make this reliably catch an unguarded loop instead of being a
        # coin flip: a big universe (so each read spends real time inside the
        # Python-level loop) and a 1µs thread-switch interval (so the mutating
        # thread is guaranteed to interleave). Verified to FAIL when any of the
        # list() guards is removed.
        errors = []
        stop   = threading.Event()
        for i in range(300):                     # wide scan + fat reject map
            wide = f"W{i}"
            st.active_watchlist[wide] = wide
            st.token_to_name[wide]    = wide
            st.book[wide] = st.book["BBB"]
            eng._note_reject(wide, f"W-OBI {i / 10:.2f} < 3.0")

        def reader():
            try:
                while not stop.is_set():
                    api.scalp_scan()
                    eng.snapshot()
                    eng.reject_summary()
                    api.scalp_status()
            except Exception as exc:            # noqa: BLE001 — that's the point
                errors.append(exc)

        prev_switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            # Positions are inserted directly rather than through
            # place_paper_order: this loop only needs the dict CHURN, and 400
            # simulated fills would bury the suite's output in log lines.
            for i in range(400):
                sym = f"SYM{i}"
                st.active_watchlist[sym] = sym
                st.token_to_name[sym]    = sym
                st.book[sym] = st.book["BBB"]
                eng._note_reject(sym, f"W-OBI {i / 10:.2f} < 3.0")
                st.positions[sym] = Position(
                    symbol=sym, token=sym, entry_price=10.0, entry_time="",
                    quantity=1, stop_loss=9.5, target=11.0, sl_offset=0.5,
                    target_offset=1.0, order_id=f"T{i}", strategy=STRATEGY_SCALP,
                    opened_at=time.monotonic())
                st.positions.pop(sym, None)
                st.active_watchlist.pop(sym, None)
                st.book.pop(sym, None)
        finally:
            stop.set()
            t.join(timeout=15)
            sys.setswitchinterval(prev_switch)
        check("read paths survive concurrent mutation from the engine",
              not errors, repr(errors[:1]))

    api.set_services(None, None)
    st.positions.clear(); st.closed_positions.clear(); st.traded_today.clear()
    st.reset_scalp_state()
    st.daily_pnl = 0.0
    st.active_watchlist = {}; st.token_to_name = {}; st.ltp = {}


if __name__ == "__main__":
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s11b, s12, s13, s14):
        fn()
    print(f"\nALL GREEN — {PASS} assertions passed")
