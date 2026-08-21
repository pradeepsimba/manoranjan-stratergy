from __future__ import annotations

"""
Order-book scalper — execution, bracket and square-off management.

This is the only scalper module that touches AppState, the clock, the DB or the
paper broker; the decision logic it calls is pure (app/engine/scalper.py) and the
parsing it consumes is pure (app/engine/orderbook.py).

It is driven once per tick cycle from SchedulerService._run_active_phase, AFTER
the core strategy's exits and entries. Ordering matters:

  _tick_exits    already walks EVERY open position (both strategies) against
                 SL/target, so scalp brackets are handled there — this module
                 must not re-implement that, or a fill could be booked twice.
  _tick_entries  runs first so the core strategy's slower, TA-Lib-gated signals
                 aren't starved of capital by a burst of scalps within the cycle.

What this module owns, then, is only what the shared machinery can't know about:
the session windows, the max-hold time stop, the 14:45 flatten, and the scalp
book's own risk gates.

DB writes reuse the scheduler's retry queues (`queue_entry_save` / `write_exit`
callbacks) rather than talking to asyncpg directly — those queues are what makes
an entry survive a DB outage, and a second, parallel persistence path would be
the one place trades could silently vanish.
"""

import re
import time
from datetime import datetime
from typing import Callable, Dict, List, Tuple
from zoneinfo import ZoneInfo

import app.config as cfg
from app.engine.position_manager import can_enter_scalp
from app.engine.scalper import evaluate, plan_entry, session_profile
from app.models import STRATEGY_SCALP, OrderBook, Position, ScalpDecision
from app.services.paper_trade import force_close, place_paper_order
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# A dry-run signal repeats on every tick while the book stays imbalanced (there
# is no position to stop it re-firing), so the same symbol would flood the log
# ~10×/second. Log each symbol's dry-run signal at most this often.
_DRY_LOG_THROTTLE_S = 10.0

# Same idea for rejections: the reason map keeps only the LATEST reason per
# symbol, which is a live diagnostic rather than a history, so it needs no
# throttle — but it is trimmed if the tracked universe is unexpectedly large.
_MAX_REJECT_SYMBOLS = 500

# Any run of digits (with separators) in a rejection reason — replaced by "N" so
# the live values don't fragment the aggregated summary.
_NUM_RE = re.compile(r"[-+]?\d[\d,.]*")


def _bucket(symbol: str, reason: str) -> str:
    """
    Normalise one rejection reason into a countable bucket: the symbol becomes a
    placeholder and every number becomes N, so "W-OBI 1.80 < 3.0" and
    "W-OBI 2.40 < 3.0" collapse to "W-OBI N < N", and "AAA in re-entry cooldown
    (43s left)" to "<symbol> in re-entry cooldown (Ns left)".
    """
    return _NUM_RE.sub("N", reason.replace(symbol, "<symbol>"))


class ScalpEngine:
    def __init__(
        self,
        queue_entry_save: Callable[[Position], None],
        write_exit:       Callable[[Position], "object"],   # awaitable
    ) -> None:
        self.state             = get_state()
        self._queue_entry_save = queue_entry_save
        self._write_exit       = write_exit
        # symbol → latest rejection reason. The single most useful diagnostic
        # here: "no signals" is ambiguous, "W-OBI 1.8 < 3.0 on 34 symbols" is not.
        self._rejects: Dict[str, str]   = {}
        self._dry_logged: Dict[str, float] = {}
        # Symbols already warned about during a flatten (the square-off retries
        # every cycle, so an unpriced symbol would otherwise log ~10×/second).
        self._flatten_warned: set = set()
        # Symbols currently qualifying — the edge latch behind `signals` (see
        # _evaluate). A symbol enters once and only counts again after it fails.
        self._passing: set = set()
        self.evaluated = 0    # evaluations run today (many per symbol per second)
        self.signals   = 0    # DISTINCT setups: symbols that started qualifying
        self.fills     = 0    # signals that actually became orders

    # ── Per-cycle entry point ─────────────────────────────────────────────────

    async def tick(self) -> None:
        """
        One scalper cycle. Cheap by construction: the whole path is dict lookups
        and arithmetic over ≤5 book levels and ≤40 tape prints per symbol, so it
        runs inline on the event loop at the 100ms tick cadence (unlike the core
        strategy's TA-Lib scan, which needs the thread pool).

        Never raises: the caller's loop must keep running _tick_exits for the
        core strategy even if the scalper hits a bad tick.
        """
        st   = self.state
        now  = datetime.now(IST)
        prof = session_profile(now)
        st.scalp_session = prof

        if not cfg.SCALP_ENABLED:
            # Flipping the master switch off must stop NEW risk without
            # abandoning risk already on the book: open scalps keep their time
            # stop and their 14:45 flatten. Without this they would silently lose
            # both and drift to the 15:30 EOD square-off — an operator disabling
            # the strategy would be *extending* the holding period of its trades.
            if any(p.strategy == STRATEGY_SCALP for p in st.positions.values()):
                await self._manage_open(now, prof)
            # Drain rather than leave a stale set behind: market_data stops adding
            # the moment the switch flips, so whatever is in there is history.
            if st.dirty_ticks_scalp:
                st.dirty_ticks_scalp.clear()
            return

        # Position management first — flattening frees capital and a position
        # slot that this same cycle's entries may then use.
        await self._manage_open(now, prof)

        if prof.window in ("closed", "squareoff"):
            if st.dirty_ticks_scalp:
                st.dirty_ticks_scalp.clear()
            return

        # Snapshot-and-clear (atomic rebind; a tick landing mid-swap is picked up
        # next cycle) — the same pattern as the core _tick_entries.
        dirty, st.dirty_ticks_scalp = st.dirty_ticks_scalp, set()
        if not dirty:
            return

        candidates = self._evaluate(dirty, prof.required_ratio)
        if not candidates:
            return

        # Execution is gated, EVALUATION is not: warm-up and a paused midday
        # still score every symbol so the dashboard shows what would have fired.
        # This is what makes the warm-up window a real scanner instead of a
        # 30-minute blind spot.
        if not prof.execute:
            for sym, _tok, _book, dec in candidates:
                self._log_signal(sym, dec, mode="scan_only", note=prof.note)
            return

        await self._fill(candidates, prof)

    # ── Signal evaluation ─────────────────────────────────────────────────────

    def _evaluate(self, dirty: set, required_ratio: float
                  ) -> List[Tuple[str, str, OrderBook, ScalpDecision]]:
        st       = self.state
        t2n      = st.token_to_name
        now_mono = time.monotonic()
        out: List[Tuple[str, str, OrderBook, ScalpDecision]] = []
        # The AI risk screen's verdict has to bind HERE, not only when the
        # watchlist is built: _restore_positions_from_db force-adds a restored
        # open symbol back into active_watchlist (it needs ticks for SL/target),
        # and unlike the core strategy the scalper deliberately ignores
        # traded_today — so without this a symbol Gemini flagged as risky could
        # be scalped again once its original position closed and the cooldown
        # expired. A manual watchlist add clears the symbol from this list (see
        # watchlist_add), so an explicit human override still wins.
        excluded = set(st.gemini_excluded)

        for tok in dirty:
            sym = t2n.get(tok)
            if sym is None:
                continue
            if sym in excluded:
                self._note_reject(sym, "excluded by the AI risk screen")
                continue
            # ONE book reference for both evaluate() and plan_entry(): the WS
            # thread publishes a new OrderBook object per change, so re-reading
            # st.book later could size a trade off a book that never passed the
            # filters. (Reading the attribute once is what makes that impossible.)
            book = st.book.get(sym)
            tape = st.tape.get(sym, ())
            self.evaluated += 1
            dec = evaluate(book, tape, now_mono, required_ratio,
                           st.ltp.get(sym, 0.0))
            if not dec.ok:
                self._note_reject(sym, dec.reason)
                # Clear the edge latch so the setup counts again if it returns.
                self._passing.discard(sym)
                continue
            # `signals` is EDGE-triggered: counted once when a symbol starts
            # qualifying, not once per cycle it keeps qualifying. A book stays
            # imbalanced for many cycles, so a level-triggered counter would read
            # ~10 per second per symbol and "Signals 3,214" would describe what
            # was really one setup — useless for judging the strategy or the
            # thresholds. Latching per symbol (rather than diffing the whole
            # passing set each cycle) also survives symbols that tick
            # intermittently: absence from a cycle's dirty set is not a reset.
            if sym not in self._passing:
                self._passing.add(sym)
                self.signals += 1
            out.append((sym, tok, book, dec))

        # Strongest imbalance first, symbol as the deterministic tie-break —
        # when more symbols signal than there are free slots, WHICH ones get
        # filled must not depend on set iteration order (the same reasoning as
        # the core engine's tightest-stop-first fill priority).
        out.sort(key=lambda c: (-(c[3].metrics.get("obiRatio") or 0.0), c[0]))
        return out

    # ── Order placement ───────────────────────────────────────────────────────

    async def _fill(self, candidates, prof) -> None:
        st  = self.state
        lev = max(1, int(cfg.INTRADAY_LEVERAGE))

        # Both strategies draw on the same account: available capital is equity
        # minus the margin the whole open book has already committed (identical
        # definition to scan_stock / Portfolio.margin_used, so the two engines
        # can't double-spend the same rupee).
        committed = sum(p.entry_price * p.quantity
                        for p in st.positions.values()) / lev
        available = cfg.ACCOUNT_BALANCE - committed
        total_cap = cfg.ACCOUNT_BALANCE

        scalp_open   = sum(1 for p in st.positions.values()
                           if p.strategy == STRATEGY_SCALP)
        trades_today = sum(st.scalp_trades_today.values())
        now_mono     = time.monotonic()

        for sym, tok, book, dec in candidates:
            # Never open a position whose stop we cannot then monitor: _tick_exits
            # skips symbols with no st.ltp, so an entry priced purely off the book
            # (possible when a tick's LTP field didn't parse but its snap did)
            # would sit unmanaged until the EOD flat.
            if not st.ltp.get(sym):
                self._note_reject(sym, "no live price to monitor the stop against")
                continue

            last_exit     = st.scalp_last_exit.get(sym)
            last_exit_ago = (now_mono - last_exit) if last_exit is not None else None

            ok, reason = can_enter_scalp(
                symbol        = sym,
                open_symbols  = st.positions,
                scalp_open    = scalp_open,
                trades_symbol = st.scalp_trades_today.get(sym, 0),
                trades_today  = trades_today,
                last_exit_ago = last_exit_ago,
                daily_pnl     = st.daily_pnl,
                scalp_pnl     = st.scalp_pnl,
            )
            if not ok:
                self._note_reject(sym, reason)
                # A book-wide stop (concurrency, daily churn cap, either loss
                # limit) rejects every remaining candidate too — stop rather than
                # re-derive it per symbol. Tested structurally, NOT by matching
                # the rejection prose: a reworded message must not silently turn
                # this into a per-symbol skip.
                if self._book_wide_stop(scalp_open, trades_today):
                    break
                continue

            sig, why = plan_entry(
                symbol        = sym,
                token         = tok,
                book          = book,
                ltp           = st.ltp.get(sym, 0.0),
                available     = available,
                total_capital = total_cap,
                metrics       = dec.metrics,
            )
            if sig is None:
                self._note_reject(sym, why)
                continue

            if cfg.SCALP_DRY_RUN:
                # Forward-test mode: everything up to and including sizing runs,
                # nothing is placed. NOTE this counts SIGNALS, not simulated
                # trades — with no position opened, the same setup keeps
                # re-qualifying, so the log will show more entries than an armed
                # run would take (see the throttle above).
                self._log_signal(sym, dec, mode="dry_run", signal=sig)
                continue

            pos = place_paper_order(
                symbol        = sym,
                token         = tok,
                quantity      = sig.quantity,
                entry_price   = sig.ltp,
                sl_offset     = sig.sl_offset,
                target_offset = sig.target_offset,
                strategy      = STRATEGY_SCALP,
            )
            self.fills   += 1
            scalp_open   += 1
            trades_today += 1
            st.scalp_trades_today[sym] = st.scalp_trades_today.get(sym, 0) + 1
            # Committing capital as we go: the next candidate in THIS cycle must
            # see the reduced availability, or a burst of simultaneous signals
            # could each size against the full account.
            available -= sig.capital_needed

            self._log_signal(sym, dec, mode="filled", signal=sig)

            try:
                await self._queue_entry_save(pos)
            except Exception as e:
                print(f"[SCALP] entry persist failed ({sym}): {e}")

    def _book_wide_stop(self, scalp_open: int, trades_today: int) -> bool:
        """
        True when a limit that applies to the WHOLE scalp book is breached, so no
        further candidate this cycle can be entered. Mirrors the book-wide subset
        of can_enter_scalp's gates (the per-symbol ones — cooldown, per-symbol
        cap, already-open — deliberately excluded: those only skip one symbol).
        """
        st = self.state
        return (scalp_open   >= cfg.SCALP_MAX_CONCURRENT_POSITIONS
                or trades_today >= cfg.SCALP_MAX_TRADES_PER_DAY
                or st.scalp_pnl <= -cfg.SCALP_DAILY_LOSS_LIMIT
                or st.daily_pnl <= -cfg.DAILY_LOSS_LIMIT)

    # ── Open-position management ───────────────────────────────────────────────

    async def _manage_open(self, now: datetime, prof) -> None:
        """
        Scalp-specific exits. SL/target are NOT handled here — the scheduler's
        _tick_exits already checks every open position against them each cycle.
        """
        st = self.state

        if prof.window == "squareoff":
            today = now.strftime("%Y-%m-%d")
            if st.scalp_squareoff_date != today:
                # Once per day: drop any in-flight intent and say so. With the
                # paper broker every fill is immediate, so there is nothing
                # queued to cancel — this is the hook a real broker's
                # cancelOrder(pending) loop belongs in, and it must run BEFORE
                # the flatten so a pending buy can't fill into a flat book.
                self._cancel_pending()
                st.scalp_squareoff_date = today
                print(f"=== SCALP: {prof.window} at {now.strftime('%H:%M')} — "
                      f"cancelling pending intents and flattening ===")
            # Flatten unconditionally (not just once): a position that somehow
            # exists after the square-off time must still be closed, and this is
            # a no-op when the scalp book is empty.
            await self._flatten_all("SCALP SQUARE-OFF")
            return

        # Time stop: a scalp that has neither hit target nor stop within
        # SCALP_MAX_HOLD_S has failed its premise — it is now an unmanaged
        # directional bet tying up capital and a position slot.
        max_hold = float(cfg.SCALP_MAX_HOLD_S)
        now_mono = time.monotonic()
        for sym in list(st.positions.keys()):
            pos = st.positions.get(sym)
            if pos is None or pos.strategy != STRATEGY_SCALP:
                continue
            if pos.opened_at is None:
                # Restored from the DB after a restart: monotonic timestamps
                # don't survive the process, so there is no honest age to test.
                # SL, target and the square-off still manage it.
                continue
            if now_mono - pos.opened_at < max_hold:
                continue
            ltp = st.ltp.get(sym)
            if not ltp:
                continue     # no live price — let the next tick (or EOD) handle it
            await self._close(sym, ltp, "SCALP TIME STOP")

    async def _flatten_all(self, label: str) -> None:
        st = self.state
        for sym in list(st.positions.keys()):
            pos = st.positions.get(sym)
            if pos is None or pos.strategy != STRATEGY_SCALP:
                continue
            ltp = st.ltp.get(sym)
            if not ltp:
                # No live price: do NOT fabricate one. Closing at entry here
                # would book an invented fill (and this runs every cycle, so it
                # would do so the instant the window opens). _run_eod handles
                # exactly this case properly — it REST-fetches each symbol's real
                # last 5m close before squaring off — and it still guarantees the
                # position is flat today. Warn once per symbol so a stuck feed is
                # visible rather than silent.
                if sym not in self._flatten_warned:
                    self._flatten_warned.add(sym)
                    print(f"[SCALP] {label}: no live price for {sym} — leaving it "
                          f"to the EOD square-off (which fetches a real last price)")
                continue
            await self._close(sym, ltp, label)

    async def _close(self, symbol: str, price: float, label: str,
                     synthetic: bool = False) -> None:
        try:
            closed = force_close(symbol, price, synthetic=synthetic, label=label)
        except Exception as e:
            print(f"[SCALP] close failed ({symbol}): {e}")
            return
        if closed is None:
            return
        self.state.scalp_log.append({
            "time":   datetime.now(IST).strftime("%H:%M:%S"),
            "symbol": symbol,
            "mode":   "exit",
            "note":   label,
            "pnl":    closed.pnl,
        })
        try:
            await self._write_exit(closed)
        except Exception as e:
            print(f"[SCALP] exit persist failed ({symbol}): {e}")

    def _cancel_pending(self) -> None:
        """
        Cancel any working scalp order.

        The paper broker fills synchronously — an order either exists as a
        Position or was never placed — so there is no pending queue to walk and
        this is deliberately a no-op today. It exists as the named seam for a
        real broker integration: that is where you iterate the broker's open
        order book, cancel every scalp-tagged working order, and reconcile the
        result against st.positions before the flatten runs below.
        """
        return

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def _note_reject(self, symbol: str, reason: str) -> None:
        if len(self._rejects) > _MAX_REJECT_SYMBOLS:
            self._rejects.clear()
        self._rejects[symbol] = reason

    def _log_signal(self, symbol: str, dec: ScalpDecision, mode: str,
                    signal=None, note: str = "") -> None:
        if mode in ("dry_run", "scan_only"):
            now = time.monotonic()
            last = self._dry_logged.get(symbol, 0.0)
            if now - last < _DRY_LOG_THROTTLE_S:
                return
            self._dry_logged[symbol] = now
        entry = {
            "time":    datetime.now(IST).strftime("%H:%M:%S"),
            "symbol":  symbol,
            "mode":    mode,
            "note":    note,
            "metrics": dec.metrics,
        }
        if signal is not None:
            entry.update({
                "qty":    signal.quantity,
                "entry":  signal.ltp,
                "sl":     round(signal.ltp - signal.sl_offset, 2),
                "target": round(signal.ltp + signal.target_offset, 2),
            })
        self.state.scalp_log.append(entry)
        if mode in ("filled", "dry_run"):
            m = dec.metrics
            print(f"[SCALP:{mode}] {symbol} W-OBI {m.get('obiRatio')} "
                  f"(need {m.get('requiredRatio')}) · tape buy "
                  f"{m.get('tapeBuyQty')} ratio {m.get('tapeBuyRatio')} · "
                  f"spread {m.get('spreadPct')}%"
                  + (f" · qty {signal.quantity} @ {signal.ltp}" if signal else ""))

    def reject_summary(self, limit: int = 8) -> List[Dict[str, object]]:
        """
        Most common current rejection reasons, most frequent first.

        Reasons carry live values ("W-OBI 1.80 < 3.0") and often the symbol
        ("AAA in re-entry cooldown"), so they must be normalised before counting
        or every symbol lands in its own bucket and the summary degenerates into
        an unaggregated list — which is precisely the failure it exists to avoid.
        """
        counts: Dict[str, int] = {}
        # Snapshot first: GET /api/scalp is a sync endpoint, so FastAPI serves it
        # from a threadpool while the engine's event-loop cycle calls
        # _note_reject hundreds of times a second. Iterating the live dict there
        # would raise "dictionary changed size during iteration".
        for symbol, reason in list(self._rejects.items()):
            counts[_bucket(symbol, reason)] = counts.get(_bucket(symbol, reason), 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [{"reason": r, "symbols": n} for r, n in top]

    def snapshot(self) -> dict:
        """Scalper block for the dashboard payload / GET /api/scalp."""
        st   = self.state
        # Resolve on demand when the engine hasn't ticked yet (outside market
        # hours, or while disabled) — otherwise the pre-flight check that matters
        # most, "which window am I in and what ratio would it demand", reads "—"
        # exactly when someone is setting the strategy up.
        prof = st.scalp_session or session_profile(datetime.now(IST))
        # list() before the comprehension: snapshot() is reached BOTH from the
        # event loop (the 1 Hz dashboard payload) and from a threadpool (the sync
        # GET /api/scalp), and on the latter path a fill or exit landing mid-loop
        # would raise "dictionary changed size during iteration".
        open_scalps = [
            {
                "symbol": p.symbol, "qty": p.quantity, "entry": p.entry_price,
                "sl": p.stop_loss, "target": p.target,
                "ltp": st.ltp.get(p.symbol, p.entry_price),
                "heldS": (round(time.monotonic() - p.opened_at, 1)
                          if p.opened_at is not None else None),
            }
            for p in list(st.positions.values()) if p.strategy == STRATEGY_SCALP
        ]
        return {
            "enabled":       bool(cfg.SCALP_ENABLED),
            "dryRun":        bool(cfg.SCALP_DRY_RUN),
            "window":        prof.window if prof else "—",
            "execute":       bool(prof.execute) if prof else False,
            "requiredRatio": prof.required_ratio if prof else None,
            "note":          prof.note if prof else "",
            "evaluated":     self.evaluated,
            "signals":       self.signals,
            "fills":         self.fills,
            "openScalps":    open_scalps,
            "scalpPnl":      round(st.scalp_pnl, 2),
            "tradesToday":   sum(st.scalp_trades_today.values()),
            "booksTracked":  len(st.book),
            "rejects":       self.reject_summary(),
            # 15, not the deque's full 60: this rides on EVERY 1 Hz STATE_UPDATE,
            # which also goes to the dashboard and indicators pages, and each entry
            # carries a metrics dict (~400 bytes). The most any UI renders is 14
            # (the /scalping log; the dashboard panel shows 8), so a bigger slice is
            # pure bandwidth — the same reasoning that keeps indicatorSnapshot to
            # every 10th push. The full history stays in st.scalp_log.
            "log":           list(st.scalp_log)[-15:],
            # The limits the counters above are measured against, so the UI can
            # render "2 / 3" without a second round trip to the settings API.
            "caps": {
                "maxConcurrent":   cfg.SCALP_MAX_CONCURRENT_POSITIONS,
                "maxTradesPerDay": cfg.SCALP_MAX_TRADES_PER_DAY,
                "maxHoldS":        cfg.SCALP_MAX_HOLD_S,
                "dailyLossLimit":  cfg.SCALP_DAILY_LOSS_LIMIT,
            },
        }

    def reset_daily(self) -> None:
        """Daily counter reset (state structures are cleared by
        AppState.reset_scalp_state, called from the same EOD path)."""
        self._rejects.clear()
        self._dry_logged.clear()
        self._flatten_warned.clear()
        self._passing.clear()
        self.evaluated = 0
        self.signals   = 0
        self.fills     = 0
