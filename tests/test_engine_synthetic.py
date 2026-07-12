"""
Synthetic end-to-end conformance test for the backtest engine.

Runs the REAL replay pipeline (SymbolSeries -> simulate -> Portfolio -> fills
-> metrics) on fabricated candles with hand-computed expected results, so the
engine's mechanics are verified by execution, not by reading:

  * entry timing (scan window, cutoff = bar must CLOSE by cutoff)
  * no look-ahead (no exit on the entry bar, even if its high crosses target)
  * exit resolution (target touch, SL-wins-ties, gap-through at the open)
  * EOD square-off (intraday) vs overnight holds + range-end square-off (delivery)
  * once-per-run re-entry lock + run-level loss limit (positional semantics)
  * deterministic fill priority when signals exceed free slots
  * RISK_MODE fixed_amount vs capital_pct sizing
  * delivery cost profile (STT both legs + DP) vs intraday
  * duplicate-bar dedup in the data layer

talib is ALWAYS stubbed (deterministic indicator outputs — the scenarios are
driven by candle geometry and toggles, not by real TA math). numpy is used if
installed (Docker); otherwise a minimal pure-python shim covering exactly the
operations the engine performs is injected, so the suite also runs on hosts
without the scientific stack:

    python3 tests/test_engine_synthetic.py
"""
from __future__ import annotations

import math
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── numpy shim (only when numpy is unavailable) ───────────────────────────────
try:
    import numpy  # noqa: F401
except ImportError:
    class A(list):
        """Tiny ndarray stand-in for the ops the engine actually uses."""
        @property
        def size(self):  # noqa: D401
            return len(self)

        def __getitem__(self, i):
            if isinstance(i, slice):
                return A(list.__getitem__(self, i))
            if isinstance(i, A):                       # boolean mask
                return A(v for v, m in zip(self, i) if m)
            return list.__getitem__(self, i)

        def _bin(self, other, fn):
            if isinstance(other, (list, A)):
                return A(fn(a, b) for a, b in zip(self, other))
            return A(fn(a, other) for a in self)

        def __add__(self, o):  return self._bin(o, lambda a, b: a + b)
        def __sub__(self, o):  return self._bin(o, lambda a, b: a - b)
        def __mul__(self, o):  return self._bin(o, lambda a, b: a * b)
        def __gt__(self, o):   return self._bin(o, lambda a, b: a > b)
        def __ge__(self, o):   return self._bin(o, lambda a, b: a >= b)
        def __lt__(self, o):   return self._bin(o, lambda a, b: a < b)
        def __le__(self, o):   return self._bin(o, lambda a, b: a <= b)
        def __invert__(self):  return A(not v for v in self)

        def min(self):    return __builtins__.min(self) if not isinstance(__builtins__, dict) else min(self)
        def mean(self):   return sum(self) / len(self)
        def any(self):    return any(bool(v) for v in self)
        def all(self):    return all(bool(v) for v in self)

        def cumsum(self):
            out, run = A(), 0.0
            for v in self:
                run += v
                out.append(run)
            return out

    np_shim = types.ModuleType("numpy")
    np_shim.ndarray = A
    np_shim.float64 = float
    np_shim.fromiter = lambda it, dtype, count=-1: A(it)
    np_shim.isnan = lambda x: (A(math.isnan(v) for v in x) if isinstance(x, (list, A))
                               else (isinstance(x, float) and math.isnan(x)))
    np_shim.any = lambda x: x.any() if isinstance(x, A) else any(x)
    np_shim.all = lambda x: x.all() if isinstance(x, A) else all(x)
    np_shim.diff = lambda x: A(x[i + 1] - x[i] for i in range(len(x) - 1))
    sys.modules["numpy"] = np_shim

# ── talib stub (ALWAYS — deterministic outputs) ───────────────────────────────
def _const_arr(close, value, warmup=14):
    n = len(close)
    nan = float("nan")
    import numpy as _np
    return _np.fromiter((nan if i < min(warmup, n) else value for i in range(n)),
                        _np.float64, n)

talib_stub = types.ModuleType("talib")
talib_stub.RSI      = lambda close, timeperiod=14: _const_arr(close, 55.0)
talib_stub.MACD     = lambda close, fastperiod=12, slowperiod=26, signalperiod=9: (
    _const_arr(close, 1.0, 33), _const_arr(close, 0.5, 33), _const_arr(close, 0.5, 33))
talib_stub.ADX      = lambda h, l, c, timeperiod=14: _const_arr(c, 30.0, 27)
talib_stub.PLUS_DI  = lambda h, l, c, timeperiod=14: _const_arr(c, 30.0, 14)
talib_stub.MINUS_DI = lambda h, l, c, timeperiod=14: _const_arr(c, 10.0, 14)
sys.modules["talib"] = talib_stub

# httpx is only needed by the (unused-here) universe fetch — stub when absent
try:
    import httpx  # noqa: F401
except ImportError:
    sys.modules["httpx"] = types.ModuleType("httpx")

# ── app imports (AFTER the stubs) ─────────────────────────────────────────────
import app.config as cfg                              # noqa: E402
from app.backtest.data import SymbolSeries, _sort_candles   # noqa: E402
from app.backtest.engine import simulate              # noqa: E402
from app.backtest.fills import round_trip_costs       # noqa: E402
from app.models import Candle                         # noqa: E402

DAY1, DAY2 = "2026-07-06", "2026-07-07"               # Mon, Tue
from datetime import date                             # noqa: E402
D1, D2 = date(2026, 7, 6), date(2026, 7, 7)

# All 8 conditions off => entries ride on the 4 trend gates alone (documented
# degenerate mode) — the scenarios steer entries purely via candle geometry.
BASE = {
    "COND_NEAR_SUPPORT": False, "COND_BULLISH_PATTERN": False, "COND_ADX": False,
    "COND_RSI": False, "COND_MACD_CROSS": False, "COND_VOLUME_SURGE": False,
    "COND_ABOVE_VWAP": False, "COND_DEPTH": False,
    "MAX_CONCURRENT_POSITIONS": 1,
    "SLIPPAGE_BPS": 0.0,  # scenario math is exact without slip; slip tested separately
    # zero the whole cost model — pnl assertions become pure price arithmetic
    "COST_BROKERAGE_PCT": 0.0, "COST_BROKERAGE_CAP": 0.0, "COST_STT_SELL": 0.0,
    "COST_STT_BUY": 0.0, "COST_TXN_CHARGE": 0.0, "COST_GST": 0.0,
    "COST_STAMP_BUY": 0.0, "COST_SEBI": 0.0, "COST_DP_SELL": 0.0,
}


def bars(day, spec):
    """spec: list of (HH:MM, open, close, high, low, volume)."""
    return [Candle(start_time=f"{day} {t}", open=o, close=c, high=h, low=l, volume=v)
            for t, o, c, h, l, v in spec]


WARM = "2026-07-03"          # Friday before DAY1 — lookback warmup only


def series(token, name, candles):
    """Prepend a warmup day so the engine's 30-bar minimum lookback is met by
    the first in-range bar (production always has warmup days fetched). The
    warmup day rises continuously INTO the first real bar's open, so support/
    sl_offset math in the scenarios is unchanged (floored at MIN_SL_OFFSET)."""
    first_open = candles[0].open
    warm = rising_day(WARM, first_open - 7.5)   # 75 bars x 0.1 ends at first_open
    ss = SymbolSeries(token=token, name=name, series=_sort_candles(warm + candles))
    ss.index_days()
    return ss


def rising_day(day, base, n=75, step=0.1, vol=1000.0):
    """A steadily green day: 75 five-minute bars from 09:15, close rising."""
    out, t0 = [], 9 * 60 + 15
    for i in range(n):
        mins = t0 + 5 * i
        tm = f"{mins // 60:02d}:{mins % 60:02d}"
        o = base + step * i
        c = o + step
        out.append(Candle(start_time=f"{day} {tm}", open=o, close=c,
                          high=c + 0.02, low=o - 0.02, volume=vol))
    return out


def nifty_series(days):
    return series(cfg.NIFTY50_TOKEN, cfg.NIFTY50_NAME,
                  [b for d in days for b in rising_day(d, 25_000.0, vol=0.0)])


def run(symbols, days, overrides, mode="intraday", tf="5m", capital=None, slip=0.0):
    nifty = nifty_series(days)
    from_d = D1 if days[0] == DAY1 else D2
    to_d   = D2 if days[-1] == DAY2 else D1
    trades, equity, ndays = simulate(symbols, nifty, from_d, to_d, slip,
                                     capital, {**BASE, **overrides}, tf, mode)
    return trades


PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL {name}: {detail}"
    PASS += 1
    print(f"  ok  {name}")


# ── Scenario 1: basic entry + target exit + math ─────────────────────────────
def s1():
    print("S1 entry/target mechanics")
    day = rising_day(DAY1, 100.0)
    # entry at first eligible bar (starts 09:45, index 6): close = 100 + .1*6 + .1
    # support = min low of previous 10 bars = 100-.02 => sl_offset floored at 5
    # target = fill + 5*1.5 = fill + 7.5 ; make a later bar touch it
    entry_close = 100.0 + 0.1 * 6 + 0.1
    target = round(entry_close + 7.5, 2)
    day[40] = Candle(start_time=day[40].start_time, open=day[40].open,
                     close=day[40].open + 0.1, high=target + 1.0,
                     low=day[40].open - 0.02, volume=1000.0)
    tr = run({"1": series("1", "AAA", day)}, [DAY1], {})
    check("one trade", len(tr) == 1, f"{len(tr)}")
    t = tr[0]
    check("entry bar 09:45", t.entry_time.endswith("09:45"), t.entry_time)
    check("entry price = bar close", abs(t.entry_price - round(entry_close, 2)) < 0.01,
          f"{t.entry_price} vs {entry_close}")
    check("target outcome", t.outcome == "TARGET", t.outcome)
    check("exit at target level", abs(t.exit_price - target) < 0.01,
          f"{t.exit_price} vs {target}")
    check("no exit on entry bar", t.exit_time > t.entry_time, f"{t.exit_time}")
    check("pnl = (exit-entry)*qty", abs(t.net_pnl - round((t.exit_price - t.entry_price) * t.qty, 2)) < 0.05,
          f"{t.net_pnl}")
    check("qty = 500/sl_offset", t.qty == int(500.0 / 5.0), f"{t.qty}")


# ── Scenario 2: no look-ahead — entry bar's own high must not exit ───────────
def s2():
    print("S2 no look-ahead on the entry bar")
    day = rising_day(DAY1, 100.0)
    eb = day[6]                       # the entry bar (09:45)
    entry_close = eb.close
    target = round(entry_close + 7.5, 2)
    day[6] = Candle(start_time=eb.start_time, open=eb.open, close=eb.close,
                    high=eb.close + 50.0, low=eb.low, volume=1000.0)  # way past target
    # next bar gaps OPEN above the target -> must fill at the (better) open
    nb = day[7]
    day[7] = Candle(start_time=nb.start_time, open=target + 0.8, close=target + 0.9,
                    high=target + 1.0, low=target + 0.7, volume=1000.0)
    tr = run({"1": series("1", "AAA", day)}, [DAY1], {})
    check("still one trade", len(tr) == 1, f"{len(tr)}")
    check("exit NOT on entry bar", tr[0].exit_time > tr[0].entry_time,
          f"{tr[0].entry_time} -> {tr[0].exit_time}")
    check("gap-through-target fills at the better OPEN", tr[0].outcome == "TARGET"
          and tr[0].exit_time.endswith("09:50")
          and abs(tr[0].exit_price - (target + 0.8)) < 0.01,
          f"{tr[0].outcome} {tr[0].exit_time} @ {tr[0].exit_price}")


# ── Scenario 3: cutoff — bar must CLOSE by cutoff ─────────────────────────────
def s3():
    print("S3 entry window vs cutoff")
    day = rising_day(DAY1, 100.0)
    sym = {"1": series("1", "AAA", day)}
    tr = run(sym, [DAY1], {"SCAN_START_HOUR": 14, "SCAN_START_MIN": 25})
    check("14:25 bar (closes 14:30) eligible", len(tr) == 1 and tr[0].entry_time.endswith("14:25"),
          f"{[t.entry_time for t in tr]}")
    tr = run(sym, [DAY1], {"SCAN_START_HOUR": 14, "SCAN_START_MIN": 30})
    check("no bar closes by cutoff -> no trade", len(tr) == 0, f"{len(tr)}")
    tr = run(sym, [DAY1], {"SCAN_START_HOUR": 14, "SCAN_START_MIN": 20,
                           "CUTOFF_HOUR": 14, "CUTOFF_MIN": 28})
    check("off-grid cutoff 14:28 -> last entry 14:20", len(tr) == 1
          and tr[0].entry_time.endswith("14:20"), f"{[t.entry_time for t in tr]}")


# ── Scenario 4: EOD square-off + SL-wins-ties + gap-through-stop ─────────────
def s4():
    print("S4 exits: EOD, SL priority, gaps")
    day = rising_day(DAY1, 100.0)   # never hits target/SL -> EOD
    tr = run({"1": series("1", "AAA", day)}, [DAY1], {"RR_RATIO": 100.0})
    check("EOD square-off at last bar", len(tr) == 1 and tr[0].outcome == "EOD"
          and tr[0].exit_time.endswith("15:25"), f"{tr[0].outcome} {tr[0].exit_time}")

    day = rising_day(DAY1, 100.0)
    fill = 100.0 + 0.1 * 6 + 0.1
    stop, target = round(fill - 5.0, 2), round(fill + 7.5, 2)
    b = day[20]
    day[20] = Candle(start_time=b.start_time, open=(stop + target) / 2, close=b.close,
                     high=target + 1, low=stop - 1, volume=1000.0)   # touches BOTH
    tr = run({"1": series("1", "AAA", day)}, [DAY1], {})
    check("SL wins when both touched", tr[0].outcome == "STOP", tr[0].outcome)
    check("tie-fill at stop level", abs(tr[0].exit_price - stop) < 0.01,
          f"{tr[0].exit_price} vs {stop}")

    day = rising_day(DAY1, 100.0)
    b = day[20]
    day[20] = Candle(start_time=b.start_time, open=stop - 3.0, close=stop - 2.0,
                     high=stop - 1.5, low=stop - 4.0, volume=1000.0)  # gaps below stop
    tr = run({"1": series("1", "AAA", day)}, [DAY1], {})
    check("gap-through-stop fills at the worse OPEN", tr[0].outcome == "STOP"
          and abs(tr[0].exit_price - (stop - 3.0)) < 0.01, f"{tr[0].exit_price}")


# ── Scenario 5: delivery mode — overnight hold, gap exit, once-per-run ───────
def s5():
    print("S5 delivery semantics")
    d1 = rising_day(DAY1, 100.0)                       # no exit day 1
    fill = 100.0 + 0.1 * 6 + 0.1
    stop = round(fill - 5.0, 2)
    d2 = rising_day(DAY2, stop - 3.0)                  # day 2 opens through the stop
    sym = {"1": series("1", "AAA", d1 + d2)}
    # Pin the DELIVERY stop floor to 5 (its default is 15 — the shadow map is
    # itself under test here: without this override the stop would sit at
    # fill-15 and day 2's open would never reach it).
    tr = run(sym, [DAY1, DAY2], {"RR_RATIO": 100.0, "DELIVERY_RR_RATIO": 100.0,
                                 "DELIVERY_MIN_SL_OFFSET": 5.0},
             mode="delivery")
    check("held overnight", len(tr) == 1 and tr[0].entry_time.startswith(DAY1)
          and tr[0].exit_time.startswith(DAY2), f"{tr[0].entry_time} -> {tr[0].exit_time}")
    check("overnight gap fills at day-2 open", tr[0].outcome == "STOP"
          and abs(tr[0].exit_price - (stop - 3.0)) < 0.5, f"{tr[0].exit_price}")
    check("no re-entry after exit (once per run)", len(tr) == 1, f"{len(tr)}")
    # delivery risk profile shadows the plain keys
    tr = run(sym, [DAY1, DAY2], {"DELIVERY_RR_RATIO": 100.0,
                                 "DELIVERY_MIN_SL_OFFSET": 10.0}, mode="delivery")
    check("delivery MIN_SL_OFFSET applies", tr and tr[0].qty == int(500.0 / 10.0),
          f"{tr[0].qty if tr else 'no trade'}")


# ── Scenario 6: fill priority + concurrent cap ────────────────────────────────
def s6():
    print("S6 deterministic fill priority")
    # BBB has the tighter stop RATIO (same sl_offset 5, lower price -> higher
    # ratio... tightest = smallest sl/price -> the HIGHER-priced AAA wins)
    a = {"1": series("1", "AAA", rising_day(DAY1, 200.0)),
         "2": series("2", "BBB", rising_day(DAY1, 100.0))}
    tr = run(a, [DAY1], {"RR_RATIO": 100.0})
    check("only 1 position (cap)", len(tr) == 1, f"{len(tr)}")
    check("smallest sl/price ratio fills first", tr[0].symbol == "AAA", tr[0].symbol)


# ── Scenario 7: risk modes ────────────────────────────────────────────────────
def s7():
    print("S7 risk modes")
    sym = {"1": series("1", "AAA", rising_day(DAY1, 100.0))}
    tr = run(sym, [DAY1], {"RR_RATIO": 100.0})
    check("fixed_amount qty 100", tr[0].qty == 100, f"{tr[0].qty}")
    tr = run(sym, [DAY1], {"RR_RATIO": 100.0, "RISK_MODE": "capital_pct",
                           "RISK_CAPITAL_PCT": 0.02}, capital=100_000.0)
    check("capital_pct qty = 2% of run capital / stop", tr[0].qty == int(100_000 * 0.02 / 5.0),
          f"{tr[0].qty}")


# ── Scenario 8: costs + dedup ─────────────────────────────────────────────────
def s8():
    print("S8 cost profiles + duplicate-bar dedup")
    with cfg.thread_overrides({"COST_STT_SELL": 0.00025, "COST_STT_BUY": 0.0,
                               "COST_DP_SELL": 0.0, "COST_BROKERAGE_PCT": 0.0,
                               "COST_BROKERAGE_CAP": 0.0, "COST_TXN_CHARGE": 0.0,
                               "COST_GST": 0.0, "COST_STAMP_BUY": 0.0, "COST_SEBI": 0.0}):
        intraday = round_trip_costs(10_000.0, 10_000.0)
    with cfg.thread_overrides({"COST_STT_SELL": 0.001, "COST_STT_BUY": 0.001,
                               "COST_DP_SELL": 15.93, "COST_BROKERAGE_PCT": 0.0,
                               "COST_BROKERAGE_CAP": 0.0, "COST_TXN_CHARGE": 0.0,
                               "COST_GST": 0.0, "COST_STAMP_BUY": 0.0, "COST_SEBI": 0.0}):
        delivery = round_trip_costs(10_000.0, 10_000.0)
    check("intraday STT sell-only", abs(intraday - 2.5) < 1e-9, f"{intraday}")
    check("delivery = STT both legs + DP", abs(delivery - (10 + 10 + 15.93)) < 1e-9,
          f"{delivery}")

    day = rising_day(DAY1, 100.0)
    dup = day + [day[10]]                # replayed bar
    ss = series("1", "AAA", dup)
    check("dedup: series length unchanged", len(ss.series) == len(day) + 75,
          f"{len(ss.series)} vs {len(day) + 75} (incl. warmup)")


# ── Scenario 9: run-level loss limit stops entries ────────────────────────────
def s9():
    print("S9 loss limit halts entries")
    # Two symbols; first stops out for ~-500; limit 400 -> second never enters.
    day_a = rising_day(DAY1, 200.0)
    fill = 200.0 + 0.1 * 6 + 0.1
    stop = round(fill - 5.0, 2)
    b = day_a[10]
    day_a[10] = Candle(start_time=b.start_time, open=stop - 1.0, close=stop - 1.0,
                       high=stop - 0.5, low=stop - 2.0, volume=1000.0)
    # Symbol B must be GATE-BLOCKED (close below its day open of 100) until
    # after A's stop-out at 10:05, then turn green — so its first eligible
    # entry moment happens with the loss limit already breached.
    day_b = []
    for i, bb in enumerate(rising_day(DAY1, 100.0)):
        if i <= 10:   # declining: close < day open -> daily-green gate blocks
            o = 100.0 - 0.05 * i
            day_b.append(Candle(start_time=bb.start_time, open=o, close=o - 0.05,
                                high=o + 0.02, low=o - 0.1, volume=1000.0))
        else:         # recovery: crosses back above the 100 day-open ~bar 17
            o = 99.45 + 0.1 * (i - 10)
            day_b.append(Candle(start_time=bb.start_time, open=o, close=o + 0.1,
                                high=o + 0.12, low=o - 0.02, volume=1000.0))
    syms = {"1": series("1", "AAA", day_a), "2": series("2", "BBB", day_b)}
    tr = run(syms, [DAY1], {"DAILY_LOSS_LIMIT": 400.0, "MAX_CONCURRENT_POSITIONS": 2,
                            "RR_RATIO": 100.0})
    stopped = [t for t in tr if t.symbol == "AAA"]
    check("A stopped out ~-500", stopped and stopped[0].outcome == "STOP"
          and stopped[0].net_pnl < -400, f"{[ (t.symbol, t.outcome, t.net_pnl) for t in tr]}")
    check("no further entries past loss limit", all(t.symbol == "AAA" for t in tr),
          f"{[t.symbol for t in tr]}")


if __name__ == "__main__":
    for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9):
        fn()
    print(f"\nALL GREEN — {PASS} assertions passed")
