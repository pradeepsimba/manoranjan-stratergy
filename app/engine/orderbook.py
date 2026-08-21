from __future__ import annotations

"""
Order-book parsing and Weighted Order Book Imbalance (W-OBI) math.

Pure functions only — no state, no I/O, no TA-Lib — so the whole scalper signal
core is unit-testable without a market feed (see tests/test_scalper_synthetic.py).

WHY THE PARSER IS FORMAT-TOLERANT
---------------------------------
The market-data server publishes the book as a formatted TEXT blob in each
tick's `snap` field, and this repo has never had a captured sample of it: the
pre-existing parser (market_data._parse_depth, still used unchanged for the
legacy display/`depth_bullish` path) only ever needed Level-1 price/qty and the
aggregate BuyQty/SellQty, which it pulls with three narrow regexes.

The scalper needs strictly more than that — five levels, and the per-level
ORDER COUNT that the anti-spoofing filter is built on — so `parse_snap` accepts
every rendering of a depth level the upstream formatter plausibly emits:

    Bids: 1) 100.50 x 500      2) 100.45 x 300        (no order count)
    Bids: 1) 100.50 x 500 (12) 2) 100.45 x 300 (7)    (parenthesised count)
    Bids: 1) 100.50 x 500 [12] …  · 1) 100.50 x 500 / 12 · … x 500 @ 12 ord
    Bids: 100.50 x 500, 100.45 x 300                  (unindexed, in order)
    {"best_5_buy_data": [{"price": 100.5, "quantity": 500, "orders": 12}, …]}

`orders_seen` reports whether a count was actually found. When it is False the
order-count and single-ticket (spoof) filters CANNOT be evaluated, and
SCALP_REQUIRE_ORDER_DATA decides whether that blocks trading (fail-closed) or
is tolerated (fail-open, the default — matching `depth_bullish`'s documented
"absent data can only auto-pass" convention). Use GET /api/scalp/snap to see the
raw blob next to its parse and confirm against the live feed before going live.

Negative prices are dropped: the NIFTY index snap carries -0.01 sentinels rather
than a real book (the same reason the legacy parser guards on `bid_p > 0`).
"""

import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from app.models import BookLevel, OrderBook, TapeEvent

# The blueprint's decaying level weights: nearest-touch liquidity counts fully,
# deeper levels progressively less (they are easier to post and to pull).
DEFAULT_WEIGHTS: Tuple[float, ...] = (1.0, 0.8, 0.6, 0.4, 0.2)

# ── Snap field patterns ───────────────────────────────────────────────────────
# Kept independent of market_data's legacy trio so tightening one can never
# silently change the other (the legacy path drives the existing depth_bullish
# condition and the indicators page).
_LTP_PAT = re.compile(r"\bLTP\s*[:=]?\s*(-?\d+(?:\.\d+)?)", re.I)
_LTQ_PAT = re.compile(
    r"\b(?:LTQ|LastTradedQ(?:ty|uantity)|last_traded_quantity)\s*[:=]?\s*(\d+)", re.I)
_TOTQTY_PAT = re.compile(r"\bBuyQty\s+(\d+)\s+SellQty\s+(\d+)", re.I)

# Section split: everything after "Bids…:" up to "Asks", and vice versa. The
# formatter may label them "Bids (5)", "Best 5 Bids:", "BUY DEPTH:" …
_BID_SEC = re.compile(r"(?:Bids|Buy\s*Depth|best_5_buy_data)[^:\n]*[:\n](.*?)"
                      r"(?=Asks|Sell\s*Depth|best_5_sell_data|$)", re.S | re.I)
_ASK_SEC = re.compile(r"(?:Asks|Sell\s*Depth|best_5_sell_data)[^:\n]*[:\n](.*?)"
                      r"(?=Bids|Buy\s*Depth|best_5_buy_data|$)", re.S | re.I)

# "N) price x qty" with an OPTIONAL trailing order count. The count must be
# introduced by one of ( [ / @ — a bare number cannot be swallowed, so the next
# level's "2)" index is never mistaken for level 1's order count.
_INDEXED_LEVEL = re.compile(
    r"(?<![\d.])([1-9])\)\s*(-?\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+)"
    r"(?:\s*[(\[/@]\s*(\d+)\s*(?:ord(?:er)?s?)?\s*[)\]]?)?"
)
# Fallback for unindexed renderings: "price x qty" pairs in book order.
_BARE_LEVEL = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+)"
    r"(?:\s*[(\[/@]\s*(\d+)\s*(?:ord(?:er)?s?)?\s*[)\]]?)?"
)

_MAX_LEVELS = 5


# ── Weights ───────────────────────────────────────────────────────────────────

# Memo keyed on the raw setting string: parse_weights runs once per symbol per
# evaluation, and the value changes only when someone saves the setting.
_weights_memo: Dict[str, Tuple[float, ...]] = {}


def parse_weights(raw, levels: int = _MAX_LEVELS) -> Tuple[float, ...]:
    """
    Parse the SCALP_OBI_WEIGHTS setting ("1.0,0.8,0.6,0.4,0.2") into a tuple.

    Falls back to DEFAULT_WEIGHTS on anything unparseable so a bad value can
    never crash the tick loop — `validate_obi_weights` (app/services/settings.py)
    is what rejects it at SAVE time, where the user can see the error.
    """
    if isinstance(raw, (tuple, list)):
        try:
            vals = tuple(float(v) for v in raw)
            return vals[:levels] if vals else DEFAULT_WEIGHTS[:levels]
        except (TypeError, ValueError):
            return DEFAULT_WEIGHTS[:levels]
    if not isinstance(raw, str):
        return DEFAULT_WEIGHTS[:levels]
    cached = _weights_memo.get(raw)
    if cached is not None:
        return cached[:levels]
    try:
        vals = tuple(float(p) for p in raw.replace(" ", "").split(",") if p != "")
        if not vals or any(v < 0 for v in vals):
            raise ValueError("weights must be non-negative and non-empty")
    except (TypeError, ValueError):
        vals = DEFAULT_WEIGHTS
    if len(_weights_memo) > 64:       # bounded: only distinct saved strings land here
        _weights_memo.clear()
    _weights_memo[raw] = vals
    return vals[:levels]


# ── Parsing ───────────────────────────────────────────────────────────────────

def _levels_from_text(text: str) -> Tuple[BookLevel, ...]:
    """Extract up to 5 levels from one side's section text, in book order."""
    found: List[Tuple[int, BookLevel]] = []
    for m in _INDEXED_LEVEL.finditer(text):
        idx   = int(m.group(1))
        price = float(m.group(2))
        qty   = int(m.group(3))
        # Sentinel/garbage level (the index snap publishes -0.01 x 0) — drop it
        # rather than let a 0-price level distort the weighted sums.
        if price <= 0 or idx > _MAX_LEVELS:
            continue
        orders = int(m.group(4)) if m.group(4) is not None else None
        found.append((idx, BookLevel(price=price, qty=qty, orders=orders)))
    if found:
        # Sort by the printed index and keep the FIRST occurrence of each, so a
        # repeated/garbled section can't produce two "level 1"s.
        seen: Dict[int, BookLevel] = {}
        for idx, lv in found:
            seen.setdefault(idx, lv)
        return tuple(seen[i] for i in sorted(seen))

    out: List[BookLevel] = []
    for m in _BARE_LEVEL.finditer(text):
        price = float(m.group(1))
        qty   = int(m.group(2))
        if price <= 0:
            continue
        orders = int(m.group(3)) if m.group(3) is not None else None
        out.append(BookLevel(price=price, qty=qty, orders=orders))
        if len(out) >= _MAX_LEVELS:
            break
    return tuple(out)


def _levels_from_json(rows) -> Tuple[BookLevel, ...]:
    """Levels from a structured best_5_*_data array (Angel One SnapQuote shape)."""
    out: List[BookLevel] = []
    if not isinstance(rows, (list, tuple)):
        return ()
    for row in rows[:_MAX_LEVELS]:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price", row.get("Price", 0)) or 0)
            qty   = int(float(row.get("quantity", row.get("Quantity", 0)) or 0))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        raw_ord = row.get("orders", row.get("no_of_orders", row.get("Orders")))
        try:
            orders = int(raw_ord) if raw_ord is not None else None
        except (TypeError, ValueError):
            orders = None
        out.append(BookLevel(price=price, qty=qty, orders=orders))
    return tuple(out)


def parse_snap(snap, ts: float = 0.0) -> OrderBook:
    """
    Parse a tick's `snap` payload into an OrderBook.

    Accepts the text blob (every rendering documented in the module docstring),
    a JSON string, or an already-decoded dict. Never raises: an unrecognisable
    payload yields an empty OrderBook, which every downstream filter treats as
    "no book" and refuses to trade on.
    """
    if snap is None:
        return OrderBook(ts=ts)

    data = snap if isinstance(snap, dict) else None
    text = ""
    if data is None:
        text = snap if isinstance(snap, str) else str(snap)
        stripped = text.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, dict):
                    data = decoded
            except (ValueError, TypeError):
                data = None      # not JSON after all — fall through to the text path

    if data is not None:
        bids = _levels_from_json(data.get("best_5_buy_data")
                                 or data.get("bids") or data.get("buy"))
        asks = _levels_from_json(data.get("best_5_sell_data")
                                 or data.get("asks") or data.get("sell"))

        def _num(*keys):
            for k in keys:
                v = data.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return None

        ltp      = _num("ltp", "last_traded_price", "LTP") or 0.0
        ltq_val  = _num("ltq", "last_traded_quantity", "LTQ")
        buy_qty  = _num("total_buy_quantity", "totalBuyQuantity", "buy_qty")
        sell_qty = _num("total_sell_quantity", "totalSellQuantity", "sell_qty")
    else:
        bid_sec = _BID_SEC.search(text)
        ask_sec = _ASK_SEC.search(text)
        bids = _levels_from_text(bid_sec.group(1)) if bid_sec else ()
        asks = _levels_from_text(ask_sec.group(1)) if ask_sec else ()

        m_ltp = _LTP_PAT.search(text)
        ltp   = float(m_ltp.group(1)) if m_ltp else 0.0
        m_ltq = _LTQ_PAT.search(text)
        ltq_val = float(m_ltq.group(1)) if m_ltq else None
        m_tot = _TOTQTY_PAT.search(text)
        buy_qty  = float(m_tot.group(1)) if m_tot else None
        sell_qty = float(m_tot.group(2)) if m_tot else None

    orders_seen = any(lv.orders is not None for lv in bids + asks)
    return OrderBook(
        bids        = bids,
        asks        = asks,
        ltp         = ltp if ltp and ltp > 0 else 0.0,
        ltq         = int(ltq_val) if ltq_val is not None and ltq_val >= 0 else None,
        buy_qty     = int(buy_qty)  if buy_qty  is not None else None,
        sell_qty    = int(sell_qty) if sell_qty is not None else None,
        orders_seen = orders_seen,
        ts          = ts,
    )


# ── Weighted Order Book Imbalance ─────────────────────────────────────────────

def weighted_depth(levels: Sequence[BookLevel],
                   weights: Sequence[float]) -> float:
    """Σ(quantity[i] × weight[i]) over the levels the weight vector covers.

    Levels beyond the weight vector are ignored (not silently weighted 1.0), so
    shortening the vector really does narrow the depth the signal looks at."""
    total = 0.0
    for i, lv in enumerate(levels):
        if i >= len(weights):
            break
        total += lv.qty * weights[i]
    return total


def obi(book: OrderBook, weights: Sequence[float]) -> Tuple[float, float, Optional[float]]:
    """
    (weighted_bids, weighted_asks, ratio).

    ratio is None when the ask side is empty or zero-quantity — an undefined
    ratio, NOT an infinitely bullish one. Callers must treat None as "cannot
    evaluate" and skip the symbol; returning inf here would fire an entry into a
    book with no offers at all.
    """
    wb = weighted_depth(book.bids, weights)
    wa = weighted_depth(book.asks, weights)
    if wa <= 0:
        return wb, wa, None
    return wb, wa, wb / wa


def total_orders(levels: Sequence[BookLevel],
                 depth: int = _MAX_LEVELS) -> Optional[int]:
    """
    Aggregate order count over the first `depth` levels, or None when the feed
    published no counts at all. Levels missing a count are skipped, so a
    partially-annotated book yields a CONSERVATIVE (under-stated) total — it can
    only make the min-order-count filter stricter, never falsely satisfy it.
    """
    vals = [lv.orders for lv in levels[:depth] if lv.orders is not None]
    return sum(vals) if vals else None


def single_ticket_level(levels: Sequence[BookLevel], depth: int,
                        min_share: float) -> Optional[int]:
    """
    Anti-spoofing: the 1-based index of the first level within `depth` whose
    quantity is a single order (`orders == 1`) AND which carries at least
    `min_share` of the side's total displayed quantity — the classic pulled-wall
    pattern, where one large ticket fakes support and is cancelled the moment
    price approaches it.

    Returns None when no such level exists, or when the feed publishes no order
    counts (nothing to test — the caller's SCALP_REQUIRE_ORDER_DATA switch is
    what decides whether missing counts should block trading).
    """
    side_qty = sum(lv.qty for lv in levels)
    if side_qty <= 0:
        return None
    for i, lv in enumerate(levels[:depth], start=1):
        if lv.orders == 1 and lv.qty >= side_qty * min_share:
            return i
    return None


# ── Tape ──────────────────────────────────────────────────────────────────────

def tape_stats(tape: Sequence[TapeEvent], now: float,
               window_s: float) -> Dict[str, float]:
    """
    Classify the last `window_s` seconds of prints into aggressive buying vs
    selling.

    A print at or above the prevailing ASK is a buyer crossing the spread
    (aggressive buy); at or below the BID it is a seller hitting the bid. Prints
    strictly inside the spread are counted as neutral rather than guessed at.
    When the book was unknown at print time (bid/ask 0.0 — e.g. before the first
    snap parsed), the tick direction vs the previous print is used instead, which
    is the same information a tape reader would use off price alone.

    Returns buy_qty / sell_qty / total_qty (shares), trades (print count),
    buy_ratio (buy ÷ (buy+sell), 0.5 when neither side traded) and
    buy_velocity (aggressive buy shares per second across the window).
    """
    cutoff   = now - window_s
    buy_qty  = 0.0
    sell_qty = 0.0
    total    = 0.0
    trades   = 0
    prev_price = 0.0
    for ev in tape:
        if ev.ts < cutoff:
            prev_price = ev.price     # still the reference for the next in-window print
            continue
        if ev.qty <= 0 or ev.price <= 0:
            prev_price = ev.price or prev_price
            continue
        trades += 1
        total  += ev.qty
        if ev.ask > 0 and ev.price >= ev.ask:
            buy_qty += ev.qty
        elif ev.bid > 0 and ev.price <= ev.bid:
            sell_qty += ev.qty
        elif ev.bid <= 0 and ev.ask <= 0 and prev_price > 0:
            if ev.price > prev_price:
                buy_qty += ev.qty
            elif ev.price < prev_price:
                sell_qty += ev.qty
        prev_price = ev.price
    directional = buy_qty + sell_qty
    return {
        "buy_qty":      buy_qty,
        "sell_qty":     sell_qty,
        "total_qty":    total,
        "trades":       float(trades),
        "buy_ratio":    (buy_qty / directional) if directional > 0 else 0.5,
        "buy_velocity": (buy_qty / window_s) if window_s > 0 else 0.0,
    }


def append_tape(prev: Optional[Tuple[TapeEvent, ...]], ev: TapeEvent,
                maxlen: int) -> Tuple[TapeEvent, ...]:
    """
    Append one print to a symbol's tape and return a NEW tuple.

    Immutable-and-swap rather than a mutating deque: the WS thread writes while
    the event loop reads, and iterating a deque that another thread appends to
    raises "deque mutated during iteration". A tuple rebuild is O(maxlen) with
    maxlen ~20-50, cheaper than a lock on the hot tick path, and gives readers a
    consistent snapshot for free (same pattern as the atomic depth-dict swap).
    """
    if not prev:
        return (ev,)
    if len(prev) >= maxlen:
        return prev[len(prev) - maxlen + 1:] + (ev,)
    return prev + (ev,)
