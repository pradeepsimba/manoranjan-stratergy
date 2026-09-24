from __future__ import annotations

"""
Backtest replay engine for the Bank Nifty scalp strategy.

Steps each trading day 5-minute bar by bar, driving the SAME evaluate_entry
function the live scheduler calls (this repo's hard convention — live and
backtest share one strategy core for the ENTRY decision). c.html's own
runBacktest() is a confirmed empty stub, so there is no reference backtest
behavior to preserve fidelity with.

*** BACKTEST FIDELITY LIMITATION (2026-09-21 scalp-strategy rewrite) ***
The live strategy's exit lifecycle is a hard 12-SECOND window
(BN_SCALP_TIME_STOP_S) — this repo has no historical market data anywhere
at sub-5-minute granularity (only 5m OHLC bars, see CLAUDE.md's "Options
pricing"/"Self-recorded BankNifty history" notes), so a 12-second lifecycle
cannot be faithfully replayed here. Rather than fabricate a falsely-precise
simulation, _try_exit below resolves each position using the entry bar's
IMMEDIATE NEXT bar's OHLC-implied premium range (a coarse proxy for "did
target/stop get touched sometime in the ~5 minutes after entry" — see
fills.resolve_premium_touch's own docstring) and forces a TIME_SCRATCH at
that bar's close if neither was touched, since 12 seconds has by then long
since elapsed relative to a 5-minute bar regardless. This still exercises
the real evaluate_entry signal-quality/frequency logic end-to-end, but
treat any backtest ₹ P&L or win-rate number from this specific strategy as
a rough proxy, not a faithful simulation of the real sub-bar lifecycle —
that would need 1-minute-or-finer historical data this repo doesn't have.

Anti-look-ahead guarantees (still fully intact for the entry decision):
  * An entry decision at bar t only sees bars [.. t]; the option's IV/T are
    computed from that same bar's timestamp and closes [.. t].
  * A position opened at bar t is only eligible to exit on bars > t.

*** Day-level parallelism is REAL OS PROCESSES, not threads (2026-09-23,
performance pass, explicit user request) *** — days are fully independent
(fresh Portfolio() per day, no shared mutable state), so this is genuinely
CPU-bound embarrassingly-parallel work; the GIL meant the ThreadPoolExecutor
this replaced barely benefited from its "parallel" day-workers (per-bar
evaluate_entry/black_scholes is pure-Python/small-numpy-array work the GIL
mostly serializes across threads). Benchmarked against the exact synthetic
dataset/methodology before switching: a ~1-year backtest (250 trading days)
went from 83s (threads) to 28s (processes) — a 2.96x speedup — with
bit-for-bit identical trade count and net P&L confirmed between the two
implementations. A short (60-day) backtest only gained ~1.1x, since
spawning worker processes has real fixed startup cost that needs enough
per-worker work to amortize — the crossover favors processes for anything
beyond a small range, which is the realistic use case here.

Three hazards specific to running ProcessPoolExecutor from INSIDE this
live ASGI server (not a standalone script) were checked, not assumed safe:
  1. Linux's default `fork` start method is unsafe here — this process has
     an open asyncpg connection pool and live WebSocket connections; forking
     with those open risks a corrupted child. Uses
     multiprocessing.get_context("spawn") explicitly — spawn boots a
     genuinely fresh interpreter with none of that inherited.
  2. spawn does NOT inherit the parent's `app.config` module state, so a
     live Settings-page override (e.g. a customized BN_SCALP_TARGET_RS)
     would silently vanish from backtest runs unless forwarded explicitly —
     _pool_worker_init snapshots {k: getattr(cfg, k) for k in
     cfg.dynamic_defaults()} in the PARENT (which still sees the live
     value) and applies it in each child via cfg.set_runtime_overrides
     before this run's own per-run `overrides` are layered on top.
     CORRECTED 2026-09-23 (found in review — the earlier version of this
     note overclaimed): under the Docker/production launch (`uvicorn
     main:app`), `main.py` is imported as an ordinary module, so a spawned
     child's multiprocessing bootstrap only needs to import
     app.backtest.engine's own dependency tree — main.py's FastAPI-app/
     service objects genuinely never get constructed in the child. But
     under the LOCAL dev launch this repo's own CLAUDE.md also documents
     (`python main.py`), `main.py` runs AS `__main__` (script mode,
     `__spec__ is None`), and Python's spawn bootstrap unconditionally
     re-executes that script's module-level code in every child (as
     `__mp_main__`, per multiprocessing.spawn._fixup_main_from_path) to
     reconstruct `sys.modules['__main__']` — confirmed empirically with a
     standalone repro (a top-level print/object-construction in a toy
     "main.py"-shaped script executes once per spawned worker). This DOES
     re-run main.py's `db_service = DatabaseService()` /
     `mkt_service = MarketDataService()` / `app = FastAPI(...)` /
     `app.include_router(...)` construction in every worker under local
     dev — harmless (those constructors have no I/O side effects, and the
     `uvicorn.run(...)` call is correctly skipped since it's gated behind
     `if __name__ == "__main__":`, which is False for the `__mp_main__`
     re-exec), but it is real wasted per-worker startup work, not "zero
     re-trigger" as this note used to claim.
  3. cfg.thread_overrides (the OLD, thread-local-scoped mechanism — still
     used by nothing else in this file now) is UNNECESSARY here, not just
     replaced: each worker process is a separate OS process dedicated to
     ONE backtest run for its entire life, so cfg.set_runtime_overrides
     (a plain global) can never leak into the live event loop's own cfg
     reads the way it would if called from a thread sharing that process's
     memory — process isolation solves the "never on the event loop" problem
     by construction, rather than needing thread-local storage to work
     around it.
"""

import asyncio
import bisect
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from functools import partial
from typing import Dict, List, Optional, Tuple

import app.config as cfg
from app.backtest.data import SymbolSeries, load_backtest_data
from app.backtest.fills import (
    resolve_premium_touch,
    slip_buy_premium,
    slip_sell_premium,
)
from app.backtest.metrics import compute_metrics
from app.backtest.portfolio import BTPosition, Portfolio
from app.engine.bn_entry_exit import evaluate_entry
from app.engine.bn_pricing import black_scholes, estimate_iv, time_to_expiry_years
from app.models import Candle


def _slice_recent(ss: SymbolSeries, gidx: int, n: int) -> List[Candle]:
    return ss.series[max(0, gidx - n + 1): gidx + 1]


def _basket_recent_at(stocks: Dict[str, SymbolSeries], day: str, tm: str) -> Dict[str, List[Candle]]:
    """
    2026-09-21: builds the TOKEN-keyed candle dict evaluate_entry now wants
    (cfg.BN_SCALP_BASKET's 8 tokens only, not the 14-stock BN_ALL_STOCKS
    universe the old leader-vote rule needed) — untrimmed (full available
    history up to gidx) since session_vwap needs every bar from the day's
    open, same as the live scheduler's own basket_candles build.
    """
    out: Dict[str, List[Candle]] = {}
    for token in cfg.BN_SCALP_BASKET:
        ss = stocks.get(token)
        idx = ss.at.get(day, {}).get(tm) if ss else None
        if ss and idx is None:
            # Fall back to the latest available bar AT OR BEFORE tm for this
            # token on this day (found in review, 2026-09-23). Without this,
            # a single missing bar for one BN_SCALP_BASKET leg — a real,
            # documented vendor-gap class per CLAUDE.md's Kotak Bank/South
            # Indian Bank naming notes — zeroed this leg's ENTIRE day's VWAP
            # history for every subsequent bar that day (idx stayed None
            # forever after the gap, since the exact-timestamp lookup keeps
            # missing), a backtest-only divergence from live: the scheduler's
            # own basket_candles build (scheduler.py) just reads whatever has
            # accumulated in st.candles_5m, with no exact-timestamp lookup to
            # fail in the first place. by_day[day] is chronological, so this
            # never looks past tm (no look-ahead).
            day_idxs = ss.by_day.get(day, [])
            times = [ss.series[i].start_time[11:16] for i in day_idxs]
            pos = bisect.bisect_right(times, tm) - 1
            if pos >= 0:
                idx = day_idxs[pos]
        out[token] = ss.series[:idx + 1] if (ss and idx is not None) else []
    return out


def _open_position(signal, now: datetime, gidx: int) -> BTPosition:
    """Freeze this trade's risk parameters from cfg AT ENTRY — mirrors
    bn_entry_exit.open_trade_from_signal but returns the backtest's own
    BTPosition dataclass (the live/backtest split every dataclass in this
    repo already has — see Position vs BTPosition in the deleted equity engine)."""
    target_rs = cfg.BN_SCALP_TARGET_RS
    stop_rs = cfg.BN_SCALP_STOP_RS
    time_stop_s = cfg.BN_SCALP_TIME_STOP_S
    scratch_slippage_rs = cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS

    return BTPosition(
        direction=signal.direction,
        entry_time=now.isoformat(),
        entry_index_price=signal.entry_index_price,
        entry_gidx=gidx,
        target=signal.entry_premium + target_rs,
        current_sl=signal.entry_premium - stop_rs,
        sl_stage="Initial",
        strike=signal.strike,
        option_type="CE" if signal.direction == "BUY" else "PE",
        expiry=signal.expiry,
        entry_premium=signal.entry_premium,
        target_rs=target_rs, stop_rs=stop_rs, time_stop_s=time_stop_s,
        scratch_slippage_rs=scratch_slippage_rs,
        basket_score_at_entry=signal.basket_score,
        wobi_at_entry=signal.wobi,
        lot_size=cfg.BN_LOT_SIZE,
        confidence=signal.confidence,
        iv_used=signal.iv_used,
    )


def _try_exit(port: Portfolio, bn_ss: SymbolSeries, gidx: int,
             slippage_bps: float) -> None:
    """See this module's docstring for the backtest-fidelity limitation
    this approximates around (a 12s live lifecycle vs 5m historical bars)."""
    pos = port.active
    if pos is None or gidx <= pos.entry_gidx:
        return
    bar = bn_ss.series[gidx]
    now = datetime.fromisoformat(bar.start_time)

    # Look-ahead fix (2026-09-23, found in review): the touch check below can
    # resolve at bar gidx's OPEN — the earliest instant of that bar, before
    # its own close is known — so the lookback here must exclude gidx's own
    # close (unlike the entry-side/EOD lookbacks elsewhere in this module,
    # which correctly include their own bar since they price AT that bar's
    # close). Mirrors the live engine's closed_tail_closes fix for the same
    # class of bug (forming-bar IV leak).
    #
    # `- 1` extra (2026-09-24 fix, found in review — see app/models.py's
    # iv_lookback_closes for the full explanation of this exact bug class):
    # estimate_iv only re-slices its input down to BN_IV_LOOKBACK_BARS+1
    # closes when handed MORE than BN_IV_LOOKBACK_BARS — passing exactly
    # BN_IV_LOOKBACK_BARS (as this line used to) meant that re-slice never
    # fired, giving one FEWER log-return here than the entry-side/EOD
    # lookbacks in this same file use for the identical setting. The extra
    # `-1` requests one more close (51, not 50) while still correctly
    # excluding gidx's own close per the look-ahead fix above.
    lookback = bn_ss.closes[max(0, gidx - cfg.BN_IV_LOOKBACK_BARS - 1):gidx]
    iv = estimate_iv(lookback)
    expiry_dt = datetime.fromisoformat(pos.expiry)
    T = time_to_expiry_years(now, expiry_dt)

    def _premium(index_price: float) -> float:
        return black_scholes(index_price, pos.strike, T, cfg.BN_RISK_FREE_RATE, iv, pos.option_type)["price"]

    p_open = _premium(bar.open)
    p_a, p_b = _premium(bar.high), _premium(bar.low)
    p_hi, p_lo = max(p_a, p_b), min(p_a, p_b)   # CE rises with index, PE falls — max/min sidesteps the branch

    touch = resolve_premium_touch(pos.current_sl, pos.target, p_open, p_hi, p_lo)
    if touch is not None:
        exit_premium_raw, outcome = touch
        exit_premium = slip_sell_premium(exit_premium_raw, slippage_bps)
        port.close_position(now, bar.close, exit_premium, outcome)
        return

    # Neither touched within this bar's premium range — force the scratch
    # exit at this bar's own close (see the module docstring). Reads
    # pos.scratch_slippage_rs (frozen onto BTPosition at open, matching
    # BNTrade/NFTrade's own entry-freeze convention — see _open_position
    # above), NOT cfg.BN_SCALP_SCRATCH_SLIPPAGE_RS directly (2026-09-24 fix,
    # found in review): harmless today only because this setting is
    # currently static, but the frozen field existed specifically so this
    # read wouldn't silently follow a live/per-run value mid-trade if it's
    # ever promoted to dynamic — exactly the bug class CLAUDE.md documents
    # as already having caused a real production incident on the live side.
    exit_premium = slip_sell_premium(max(0.0, _premium(bar.close) - pos.scratch_slippage_rs), slippage_bps)
    port.close_position(now, bar.close, exit_premium, "TIME_SCRATCH")


def _try_entry(port: Portfolio, bn_ss: SymbolSeries, stocks: Dict[str, SymbolSeries],
               gidx: int, day: str, tm: str, slippage_bps: float) -> None:
    if port.active is not None:
        return
    bn_recent = _slice_recent(bn_ss, gidx, 5)   # only bn_recent[-1] is read now — see scheduler.py's mirror comment
    bn_closes_lookback = bn_ss.closes[max(0, gidx - cfg.BN_INDICATOR_LOOKBACK_BARS):gidx + 1]
    basket_recent = _basket_recent_at(stocks, day, tm)

    now = datetime.fromisoformat(bn_ss.series[gidx].start_time)
    # port.active is always None here (the function already returned above
    # otherwise), so trades_today is just the closed-trade count — no
    # in-flight trade to add (found in review: a stale ternary here used to
    # imply otherwise).
    trades_today = len(port.trades)
    signal, _diag = evaluate_entry(now, bn_recent, bn_closes_lookback,
                                   basket_recent, port.last_exit_time, trades_today)
    if signal is None:
        return
    signal.entry_premium = slip_buy_premium(signal.entry_premium, slippage_bps)
    port.open_position(_open_position(signal, now, gidx))


def _simulate_day_impl(day: str, bn_ss: SymbolSeries, stocks: Dict[str, SymbolSeries],
                       slippage_bps: float) -> List:
    scan_start = f"{cfg.SCAN_START_HOUR:02d}:{cfg.SCAN_START_MIN:02d}"
    cutoff     = f"{cfg.CUTOFF_HOUR:02d}:{cfg.CUTOFF_MIN:02d}"

    port = Portfolio()
    grid = sorted(bn_ss.at.get(day, {}).items())   # [(time, gidx), ...]
    for tm, gidx in grid:
        _try_exit(port, bn_ss, gidx, slippage_bps)
        if scan_start <= tm < cutoff:
            _try_entry(port, bn_ss, stocks, gidx, day, tm, slippage_bps)

    # EOD square-off any survivor at the day's last bar close.
    if port.active is not None and grid:
        last_gidx = grid[-1][1]
        last_bar = bn_ss.series[last_gidx]
        now = datetime.fromisoformat(last_bar.start_time)
        lookback = bn_ss.closes[max(0, last_gidx - cfg.BN_IV_LOOKBACK_BARS):last_gidx + 1]
        iv = estimate_iv(lookback)
        expiry_dt = datetime.fromisoformat(port.active.expiry)
        T = time_to_expiry_years(now, expiry_dt)
        bs = black_scholes(last_bar.close, port.active.strike, T, cfg.BN_RISK_FREE_RATE,
                           iv, port.active.option_type)
        exit_premium = slip_sell_premium(bs["price"], slippage_bps)
        port.close_position(now, last_bar.close, exit_premium, "EOD")

    return port.trades


# ── Process-pool workers (2026-09-23 — see module docstring for the full
# rationale/safety reasoning) ────────────────────────────────────────────
# Per-worker-process globals, set ONCE by _pool_worker_init at pool startup
# (not per day/task) — bn_index/stocks are pickled once per WORKER, not once
# per DAY, which is what makes this a net win over just re-running
# ThreadPoolExecutor's "free" (but GIL-serialized) shared-memory access.
_worker_bn_index: Optional[SymbolSeries] = None
_worker_stocks:   Optional[Dict[str, SymbolSeries]] = None


def _pool_worker_init(bn_index: SymbolSeries, stocks: Dict[str, SymbolSeries],
                      live_cfg_snapshot: Dict, overrides: Dict) -> None:
    global _worker_bn_index, _worker_stocks
    _worker_bn_index = bn_index
    _worker_stocks = stocks
    cfg.set_runtime_overrides(live_cfg_snapshot)
    if overrides:
        # Filtered to known dynamic keys only (found while benchmarking,
        # 2026-09-23) — cfg.set_runtime_overrides raises KeyError on an
        # unknown key, but the OLD mechanism this replaced (cfg.
        # thread_overrides, a plain thread-local dict merge with no
        # validation at all) silently ignored one instead, since a STATIC
        # config key (e.g. BN_SCALP_TARGET_RS) is never even looked up
        # through __getattr__ in the first place. `overrides` here should
        # already only ever contain validated dynamic SPEC keys — the real
        # caller path (dashboard.py's start_backtest) filters through
        # settings.expand_changes(bt_only=True) before this function is
        # ever reached — but an initializer exception here breaks the
        # WHOLE process pool for every day, a much bigger blast radius than
        # the old per-thread silent-ignore. This filter is a pure safety
        # net matching the old behavior, not expected to ever actually drop
        # anything in the real flow.
        valid_keys = set(cfg.dynamic_defaults())
        cfg.set_runtime_overrides({k: v for k, v in overrides.items() if k in valid_keys})


def _simulate_day_worker(day: str, slippage_bps: float) -> List:
    return _simulate_day_impl(day, _worker_bn_index, _worker_stocks, slippage_bps)


# Caps TOTAL concurrent backtest runs across this whole process (found in
# review, 2026-09-23) — see cfg.MAX_CONCURRENT_BACKTEST_RUNS's own comment.
# Safe to construct at import time: Python 3.10+ no longer binds a Semaphore
# to a specific event loop at construction, only at first await, and this
# module is only ever imported into the single ASGI server process (never
# into a spawned backtest worker — those import only their own dependency
# tree via _pool_worker_init, not app.api.dashboard/app.backtest.engine's
# run_backtest). Acquired/released around simulate() in run_backtest below,
# not around simulate() itself, so it's scoped to one asyncio.Semaphore per
# server process regardless of how many /api/backtest requests race in.
_BACKTEST_RUN_SEM = asyncio.Semaphore(cfg.MAX_CONCURRENT_BACKTEST_RUNS)


def simulate(bn_index: SymbolSeries, stocks: Dict[str, SymbolSeries],
            from_d: date, to_d: date, slippage_bps: float,
            overrides: Optional[Dict] = None) -> Tuple[List, List, int]:
    """
    Run the full replay. Days are independent (intraday, EOD square-off), so
    they execute in parallel across REAL OS PROCESSES (see module docstring —
    benchmarked ~3x faster than the ThreadPoolExecutor this replaced, on a
    realistic ~1-year backtest range, with identical results confirmed).
    """
    lo_s, hi_s = from_d.isoformat(), to_d.isoformat()
    days = sorted(d for d in bn_index.by_day if lo_s <= d <= hi_s)
    if not days:
        return [], [], 0

    workers = max(1, min(cfg.SCAN_WORKERS, len(days)))
    # The PARENT still sees the live server's real cfg values here (this
    # function itself runs in the caller's own worker thread — see
    # run_backtest's asyncio.to_thread — not yet in a spawned process), so
    # this snapshot genuinely reflects whatever's live on the Settings page
    # right now, not a stale/default value.
    live_snapshot = {k: getattr(cfg, k) for k in cfg.dynamic_defaults()}
    mp_ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=mp_ctx,
        initializer=_pool_worker_init,
        initargs=(bn_index, stocks, live_snapshot, overrides or {}),
    ) as pool:
        # map preserves input order → results already in chronological day order
        per_day = list(pool.map(partial(_simulate_day_worker, slippage_bps=slippage_bps), days))
    trades: List = []
    for day_trades in per_day:
        trades.extend(day_trades)

    cum = 0.0
    equity_curve: List = []
    for t in trades:
        cum += t.net_pnl
        equity_curve.append((t.exit_time, round(cum, 2)))

    return trades, equity_curve, len(days)


async def run_backtest(
    db, run_id: str, from_d: date, to_d: date,
    slippage_bps: float, overrides: Optional[Dict] = None,
) -> None:
    """Orchestrate one backtest run: fetch → simulate (in a worker thread) → persist."""
    try:
        overrides = overrides or {}
        warmup   = int(overrides.get("BACKTEST_WARMUP_DAYS", cfg.BACKTEST_WARMUP_DAYS))
        lookback = int(overrides.get("BN_INDICATOR_LOOKBACK_BARS", cfg.BN_INDICATOR_LOOKBACK_BARS))
        bn_index, stocks = await load_backtest_data(
            db, from_d, to_d, warmup_days=warmup, lookback=lookback)
        if bn_index is None:
            await db.fail_backtest_run(
                run_id, f"No self-recorded BankNifty history for {from_d} → {to_d} yet "
                        f"(the archive grows by one day at a time as the live engine runs — "
                        f"see app.services.database.bn_index_bars). Try a range that "
                        f"includes a day the engine has already completed.")
            return

        # Bounded to cfg.MAX_CONCURRENT_BACKTEST_RUNS total in-flight runs
        # (found in review, 2026-09-23) — without this, N concurrent
        # /api/backtest requests each spawn their own SCAN_WORKERS-sized
        # ProcessPoolExecutor with no cap across runs, oversubscribing the
        # host's CPU/memory. `async with` releases on exception/cancellation
        # via its own try/finally, so a failed/cancelled run can't leak a
        # permanently-held slot.
        async with _BACKTEST_RUN_SEM:
            trades, equity, days = await asyncio.to_thread(
                simulate, bn_index, stocks, from_d, to_d, slippage_bps, overrides
            )

        summary = compute_metrics(trades, equity, days)
        summary["stocks_loaded"] = len(stocks)

        await db.save_backtest_trades(run_id, trades)
        await db.finish_backtest_run(run_id, summary)
        print(f"Backtest {run_id} done: {summary['total_trades']} trades, "
              f"net ₹{summary['net_pnl']:+.2f}")
    except Exception as e:
        await db.fail_backtest_run(run_id, f"{type(e).__name__}: {e}")
        print(f"Backtest {run_id} failed: {e}")
