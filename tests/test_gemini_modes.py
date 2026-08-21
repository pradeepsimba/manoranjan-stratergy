"""
Gemini screen-mode conformance: whitelist vs blacklist polarity.

Pure-python, no network — _grounded_screen is monkeypatched with canned answers,
so this exercises the REAL batching, capping, dedup and completeness logic.
"""
import asyncio, sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass
for mod in ("httpx", "talib", "numpy"):
    try:
        __import__(mod)
    except ImportError:
        sys.modules[mod] = types.ModuleType(mod)

import app.config as cfg
import app.services.gemini_filter as gf

PASS = 0
def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL {name}: {detail}"
    PASS += 1
    print(f"  ok  {name}")

def fake(answer):
    """Replace the network call. answer: list, None (failure), or a callable."""
    def _f(names, mode="bullish"):
        a = answer(names, mode) if callable(answer) else answer
        return a
    gf._grounded_screen = _f

cfg.GEMINI_API_KEY = "test-key"          # module-level static, not a _DEFAULTS key
UNIVERSE = [f"S{i}" for i in range(1, 11)]

print("=== gemini screen modes ===")

# ── bullish (whitelist) ──────────────────────────────────────────────────────
fake(["S1", "S2"])
syms, ok = asyncio.run(gf.analyse_stocks(UNIVERSE, "bullish"))
check("bullish returns the whitelist", syms == ["S1", "S2"], syms)
check("bullish marks the screen complete", ok is True)

# ── exclude_risky (blacklist) ────────────────────────────────────────────────
fake(["S3", "S7"])
syms, ok = asyncio.run(gf.analyse_stocks(UNIVERSE, "exclude_risky"))
check("exclude_risky returns the risky names", syms == ["S3", "S7"], syms)
check("exclude_risky marks the screen complete", ok is True)

# ── failure vs empty — the load-bearing distinction ──────────────────────────
fake(None)
syms, ok = asyncio.run(gf.analyse_stocks(UNIVERSE, "exclude_risky"))
check("a FAILED screen reports complete=False", (syms, ok) == ([], False), (syms, ok))
fake([])
syms, ok = asyncio.run(gf.analyse_stocks(UNIVERSE, "exclude_risky"))
check("a SUCCESSFUL empty screen reports complete=True", (syms, ok) == ([], True),
      (syms, ok))

# ── the cap applies to a whitelist, never to an exclusion list ───────────────
big = [f"S{i}" for i in range(1, 101)]
cfg.set_runtime_overrides({"GEMINI_MAX_STOCKS": 5})
gf.GEMINI_BATCH_SIZE = 10            # force the batched path over 100 names
fake(lambda names, mode: list(names))        # every symbol flagged
syms, ok = asyncio.run(gf.analyse_stocks(big, "bullish"))
check("bullish whitelist IS capped", len(syms) == 5, len(syms))
syms, ok = asyncio.run(gf.analyse_stocks(big, "exclude_risky"))
check("exclusion list is NOT capped (would re-admit risky names)",
      len(syms) == 100, len(syms))

# ── batching: a partial failure is reported, survivors still returned ────────
calls = {"n": 0}
def flaky(names, mode):
    calls["n"] += 1
    return None if calls["n"] == 2 else [names[0]]
fake(flaky)
syms, ok = asyncio.run(gf.analyse_stocks(big, "exclude_risky"))
check("a partially failed batch run reports complete=False", ok is False, ok)
check("...but still returns what the good batches found", len(syms) == 9, len(syms))

# ── dedup across batches ─────────────────────────────────────────────────────
fake(lambda names, mode: ["DUP", "DUP"])
syms, _ = asyncio.run(gf.analyse_stocks(big, "exclude_risky"))
check("duplicates collapse across batches", syms == ["DUP"], syms)

# ── mode defaults to the live setting, read at call time ─────────────────────
fake(lambda names, mode: [mode])
cfg.set_runtime_overrides({"GEMINI_MODE": "exclude_risky"})
gf.GEMINI_BATCH_SIZE = 1000
syms, _ = asyncio.run(gf.analyse_stocks(UNIVERSE))
check("mode defaults to cfg.GEMINI_MODE", syms == ["exclude_risky"], syms)
cfg.set_runtime_overrides({"GEMINI_MODE": "bullish"})
syms, _ = asyncio.run(gf.analyse_stocks(UNIVERSE))
check("...and follows a runtime change", syms == ["bullish"], syms)

# ── no key => never a silent "complete" screen ───────────────────────────────
cfg.GEMINI_API_KEY = ""
syms, ok = asyncio.run(gf.analyse_stocks(UNIVERSE, "exclude_risky"))
check("missing API key reports complete=False", (syms, ok) == ([], False), (syms, ok))
# ── The actual watchlist inversion (_run_premarket) ──────────────────────────
print("")
print("=== premarket application ===")
cfg.GEMINI_API_KEY = "test-key"
import app.services.scheduler as sch
from app.state import get_state

st  = get_state()
UNI = {f"S{i}": f"S{i}" for i in range(1, 11)}


class _Stub:
    """Every attribute is an awaitable no-op (db / market_data / ws_manager)."""
    def __getattr__(self, _n):
        async def _noop(*a, **k):
            return None
        return _noop


async def _done(v):
    return v


def premarket(mode, answer, complete=True, cap=100):
    """Run the REAL _run_premarket with only the universe fetch + Gemini stubbed."""
    st.active_watchlist = {}
    st.full_watchlist   = {}
    st.gemini_shortlist = []
    st.gemini_excluded  = []
    cfg.set_runtime_overrides({"GEMINI_MODE": mode, "GEMINI_MAX_STOCKS": cap,
                               "GEMINI_ENABLED": True})

    async def fake_fetch():
        return dict(UNI)

    async def fake_analyse(names, md=None):
        return list(answer), complete

    sch.fetch_active_watchlist = fake_fetch
    sch.analyse_stocks         = fake_analyse
    svc = sch.SchedulerService(db=_Stub(), market_data=_Stub(), ws_manager=_Stub())
    svc._load_watchlist_overrides = lambda: _done({})
    asyncio.run(svc._run_premarket())
    return sorted(st.active_watchlist), sorted(st.gemini_excluded)


# exclude_risky: everything EXCEPT the flagged names is tradeable.
active, excluded = premarket("exclude_risky", ["S3", "S7"])
check("exclude mode trades the whole universe minus the risky names",
      active == sorted(set(UNI) - {"S3", "S7"}), active)
check("...and records WHAT was excluded", excluded == ["S3", "S7"], excluded)

# Nothing risky today => everything tradeable.
active, excluded = premarket("exclude_risky", [])
check("an empty risk list leaves the full universe tradeable",
      active == sorted(UNI) and excluded == [], (len(active), excluded))

# EVERY symbol risky => trade NOTHING. Falling through to the capped full list
# here would trade exactly the names the screen said to avoid.
active, excluded = premarket("exclude_risky", list(UNI))
check("all-risky trades NOTHING (no silent full-list fallback)",
      active == [] and len(excluded) == 10, (active, len(excluded)))

# A FAILED screen must not masquerade as "nothing risky".
active, excluded = premarket("exclude_risky", [], complete=False, cap=4)
check("a failed screen falls back to the capped full list",
      len(active) == 4 and excluded == [], (len(active), excluded))

# The cap still bounds exclude mode, and never re-admits an excluded name.
active, excluded = premarket("exclude_risky", ["S1"], cap=3)
check("the cap bounds exclude mode too", len(active) == 3, len(active))
check("...without forgetting the exclusion", excluded == ["S1"], excluded)
check("the capped list never contains an excluded name", "S1" not in active, active)

# bullish mode is unchanged.
active, excluded = premarket("bullish", ["S2", "S5"])
check("bullish mode still trades ONLY the shortlist",
      active == ["S2", "S5"] and excluded == [], (active, excluded))

st.active_watchlist = {}
st.full_watchlist   = {}
st.gemini_shortlist = []
st.gemini_excluded  = []
cfg.clear_runtime_overrides()
print("")
# ── The exclusion must bind at ENTRY, not only in the watchlist ──────────────
print("")
print("=== risk exclusion enforcement ===")
import asyncio as _aio

from app.services.scalp_engine import ScalpEngine
from app.engine.orderbook import append_tape, parse_snap
from app.models import BookLevel, OrderBook, TapeEvent


def _book(ts):
    """A book that clears every default scalper filter."""
    return OrderBook(
        bids=tuple(BookLevel(p, q, o) for p, q, o in
                   [(100.00, 2000, 40), (99.95, 1500, 30), (99.90, 1200, 25)]),
        asks=tuple(BookLevel(p, q, o) for p, q, o in
                   [(100.05, 300, 12), (100.10, 250, 10), (100.15, 200, 8)]),
        ltp=100.05, orders_seen=True, ts=ts)


async def _noop(_p):
    return None


def scalp_cycle(excluded):
    """One armed scalper cycle over a single symbol, with `excluded` in force."""
    import time as _t
    st.positions.clear(); st.traded_today.clear(); st.reset_scalp_state()
    st.daily_pnl = 0.0
    st.active_watchlist = {"AAA": "AAA"}
    st.token_to_name    = {"AAA": "AAA"}
    st.ltp = {"AAA": 100.05}
    now = _t.monotonic()
    st.book = {"AAA": _book(now)}
    tape = ()
    for i in range(5):
        tape = append_tape(tape, TapeEvent(ts=now - 0.1 * i, price=100.05,
                                          qty=400, bid=100.0, ask=100.05), 40)
    st.tape = {"AAA": tape}
    st.gemini_excluded = list(excluded)
    st.dirty_ticks_scalp = {"AAA"}
    cfg.set_runtime_overrides({
        "SCALP_ENABLED": True, "SCALP_DRY_RUN": False,
        "SCALP_WARMUP_HOUR": 0,  "SCALP_WARMUP_MIN": 0,
        "SCALP_MORNING_HOUR": 0, "SCALP_MORNING_MIN": 0,
        "SCALP_MIDDAY_HOUR": 23, "SCALP_MIDDAY_MIN": 57,
        "SCALP_AFTERNOON_HOUR": 23, "SCALP_AFTERNOON_MIN": 58,
        "SCALP_SQUAREOFF_HOUR": 23, "SCALP_SQUAREOFF_MIN": 59,
    })
    eng = ScalpEngine(queue_entry_save=_noop, write_exit=_noop)
    _aio.run(eng.tick())
    return eng, sorted(st.positions)


# Baseline: nothing excluded -> the scalper takes the trade.
eng, pos = scalp_cycle([])
check("scalper trades a clean symbol", pos == ["AAA"], pos)

# The same setup, but the AI flagged it: no entry, and the reason is visible.
eng, pos = scalp_cycle(["AAA"])
check("scalper REFUSES an AI-excluded symbol", pos == [], pos)
check("...and says why (not a silent skip)",
      eng._rejects.get("AAA") == "excluded by the AI risk screen",
      eng._rejects.get("AAA"))
check("...without wasting an evaluation on it", eng.evaluated == 0, eng.evaluated)

# A manual watchlist add is an explicit human override of the AI verdict.
st.full_watchlist = {"AAA": "AAA"}
st.active_watchlist = {}
st.gemini_excluded = ["AAA"]
svc = sch.SchedulerService(db=_Stub(), market_data=_Stub(), ws_manager=_Stub())
svc._persist_watchlist_change = lambda **k: _done(None)
svc._mkt.restart = lambda: _done(None)
_aio.run(svc.watchlist_add("AAA"))
check("a manual add clears the AI exclusion", st.gemini_excluded == [],
      st.gemini_excluded)

st.positions.clear(); st.traded_today.clear(); st.reset_scalp_state()
st.active_watchlist = {}; st.full_watchlist = {}
st.token_to_name = {}; st.ltp = {}
st.gemini_shortlist = []; st.gemini_excluded = []
cfg.clear_runtime_overrides()
print("")
print("ALL GREEN - " + str(PASS) + " assertions passed")
