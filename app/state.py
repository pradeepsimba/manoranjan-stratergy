from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Dict, List, Optional

import app.config as cfg
from app.models import (
    BNDiagnostic, BNTrade, Candle, NFDiagnostic, NFTrade, PendingBNEntry, PendingNFEntry, TradingPhase,
)


class AppState:
    _instance: Optional["AppState"] = None
    _creation_lock = threading.Lock()

    def __new__(cls) -> "AppState":
        # Double-checked locking: get_state() runs on the event loop AND from
        # the WS tick handler, so the steady-state path must not serialize.
        inst = cls._instance
        if inst is None:
            with cls._creation_lock:
                inst = cls._instance
                if inst is None:
                    inst = super().__new__(cls)
                    inst._init()
                    cls._instance = inst
        return inst

    def _init(self) -> None:
        # ── Session ───────────────────────────────────────────────────────────
        self.phase:      TradingPhase = TradingPhase.PRE_MARKET
        self.ws_status:  str          = "—"
        self.api_status: str          = "—"

        # ── Candle stores — BankNifty index + the 12 BN stocks, all keyed by
        # TOKEN. Capped at 300 bars (deque maxlen set on assignment). ─────────
        self.candles_5m: Dict[str, List[Candle]] = {}
        # deque(maxlen=...), not a plain list — MarketDataService._upsert_list
        # relies on maxlen for O(1) eviction of the oldest bar once the buffer
        # is full, matching the deque-per-token stores in candles_5m.
        self.bn_index_candles_5m: Deque[Candle] = deque(maxlen=cfg.MAX_CANDLE_BUFFER)

        # ── Live prices (keyed by SYMBOL NAME; BankNifty index kept separately) ─
        self.ltp:          Dict[str, float] = {}
        self.bn_index_ltp: float            = 0.0

        # ── Synthetic BankNifty index (vendor stopped streaming the real index
        # under either the old or new protocol — see market_data.py's
        # _update_synthetic_index) — a one-way latch: flips to False forever
        # the moment a genuine index tick is ever seen again. bn_synthetic_anchor
        # is the running open-anchor for the CURRENT synthetic bar, seeded at
        # startup from the last real close in the self-recorded bn_index_bars
        # archive (see scheduler.py's startup sequence). ──────────────────────
        self.bn_index_synthetic: bool = True
        self.bn_synthetic_anchor: float = 0.0

        # ── Execution-simulation fill delay (2026-09-24, explicit user
        # decision) — see bn_entry_exit.fill_delayed_entry/resolve_delayed_
        # exit_premium and bn_trade.py's arm_pending_entry/try_fill_pending_
        # entry/check_tick_exit. bn_index_tick_seq/nf_index_tick_seq are
        # incremented every time bn_index_ltp/nf_index_ltp is actually
        # WRITTEN (market_data.py, real or synthetic tick alike) — the only
        # way a pending fill can tell "a genuinely new tick arrived" apart
        # from "the 100ms tick loop just re-polled the same still-stale
        # price". Deliberately unlocked, same reasoning as dirty_ticks_push
        # below (market_data._process_tick is fully synchronous, single
        # event loop, can't interleave with itself).
        self.bn_index_tick_seq: int = 0
        self.nf_index_tick_seq: int = 0
        self.pending_entry:    Optional[PendingBNEntry] = None
        self.pending_entry_nf: Optional[PendingNFEntry] = None

        # ── The single active Bank Nifty options trade ────────────────────────
        self.active_trade:   Optional[BNTrade] = None
        self.closed_trades:  List[BNTrade]     = []   # today's closed trades
        self.last_exit_time: Optional[str]     = None  # ISO timestamp, cooldown
        self.daily_pnl:      float             = 0.0
        # Running paper-account balance — persists ACROSS days (see database's
        # _BN_FUNDS key), unlike daily_pnl which resets every EOD.
        self.funds: float = 0.0

        # ── Latest entry-loop diagnostic ("why didn't it fire") for the dashboard ─
        # (last_evaluated_bar/last_trade_candle — the old "one eval per closed
        # bar" dedup — were removed 2026-09-22 alongside the switch to
        # tick-wise entry evaluation; st.active_trade plus evaluate_entry's own
        # cooldown check already fully gate re-entry, so no bar-boundary
        # bookkeeping is needed any more.)
        self.bn_diagnostic: Optional[BNDiagnostic] = None
        # Scalp strategy (2026-09-21) risk guardrail: trades OPENED today,
        # for cfg.SCALP_MAX_TRADES_PER_DAY — reset every EOD alongside
        # closed_trades. Shared trading-window guardrail needs no state
        # (pure function of wall-clock time — see bn_entry_exit._in_trading_window).
        # NOTE (2026-09-24 fix, found in review): SCALP_MAX_TRADES_PER_DAY is
        # documented (risk_guardrails.max_trades_ok's own docstring) as ONE
        # shared cap across both instruments, "not a pair per instrument" —
        # but every caller used to check bn_trades_today/nf_trades_today
        # against the cap SEPARATELY, silently doubling the real daily limit
        # (5+5=10 instead of a true 5 combined). This field still tracks
        # each instrument's own count (still needed for the per-instrument
        # tradesToday/maxTradesToday diagnostic display) — see
        # trades_today_combined below for the value the gate actually
        # checks now.
        self.bn_trades_today: int = 0

        # ── Live-price ticker push (100ms delta broadcast) ────────────────────
        self.dirty_ticks_push: set = set()

        # ── 15m support/resistance levels (Stock Candles panel only, unrelated
        # to the BN trading strategy) — refreshed every 5 min via a periodic
        # REST fetch (this app otherwise never streams 15m candles), keyed by
        # TOKEN. 5m S/R for the same panel is computed on the fly from
        # candles_5m/bn_index_candles_5m, no separate storage needed. ─────────
        self.sr_15m_levels: Dict[str, Dict[str, List[float]]] = {}

        # Per-token locks: each instrument's candle list gets its own lock so
        # WS tick writes and the tick loop don't contend across unrelated tokens.
        self._token_locks:      Dict[str, threading.Lock] = {}
        self._token_locks_meta: threading.Lock            = threading.Lock()
        self._bn_index_lock: threading.Lock = threading.Lock()

        # ── Nifty 50 — a second, independent instrument running in parallel
        # to BankNifty above. candles_5m/ltp (keyed by symbol string) are
        # SHARED across both instruments — no separate store needed there.
        # funds/daily_pnl are also SHARED (one paper account, two strategies)
        # — only per-instrument STRATEGY EXECUTION state is separate. ───────
        self.nf_index_candles_5m: Deque[Candle] = deque(maxlen=cfg.MAX_CANDLE_BUFFER)
        self.nf_index_ltp:        float        = 0.0
        self.nf_index_synthetic:  bool         = True
        self.nf_synthetic_anchor: float        = 0.0
        self._nf_index_lock: threading.Lock = threading.Lock()

        self.active_trade_nf:      Optional[NFTrade] = None
        self.closed_trades_nf:     List[NFTrade]      = []
        self.last_exit_time_nf:    Optional[str]      = None
        self.nf_diagnostic:        Optional[NFDiagnostic] = None
        self.nf_trades_today: int = 0   # NF mirror of bn_trades_today above

        # market_data_service/bn_option_ltp/nf_option_ltp — REMOVED 2026-09-22
        # (field and all, not just left dead): these existed only for the
        # "real-option-LTP paper trading" feature (2026-09-17), which let a
        # real tick snap a trade's settlement premium past the scalp
        # strategy's tight ~₹2-3 target/stop bracket (see bn_trade.
        # check_tick_exit's docstring for the full incident writeup).
        # market_data_service existed solely so bn_trade.py/nf_trade.py
        # could reach MarketDataService.set_bn_option_symbol/
        # set_nf_option_symbol without a circular import — once those
        # setters were confirmed to have zero remaining callers anywhere,
        # a repo-wide grep also confirmed market_data_service/bn_option_ltp/
        # nf_option_ltp themselves had zero readers outside state.py and
        # market_data.py's own (also-removed) writer — an earlier version
        # of this comment claimed "AppState fields are read in several
        # places by attribute name," which was the actual reason given for
        # NOT removing them; that claim didn't hold up once checked, so
        # they were removed outright instead, for consistency with
        # premium_synthetic's own removal the same day.

        # ── Live ATM CE/PE watchlist (2026-09-18, explicit user decision) —
        # separate from the removed real-option-LTP feature above: THAT
        # used to track one specific trade's FROZEN strike (set at entry,
        # never changes for that trade's lifetime); THIS tracks whatever
        # the CURRENT live ATM strike is (recomputed continuously off
        # bn_index_ltp/nf_index_ltp
        # by SchedulerService._tick_atm_watch), regardless of whether a
        # trade is open. Symbols are set by MarketDataService.set_bn_atm_
        # watch/set_nf_atm_watch; LTPs are filled in by _process_tick.
        self.bn_atm_ce_symbol: Optional[str] = None
        self.bn_atm_pe_symbol: Optional[str] = None
        self.bn_atm_ce_ltp: Optional[float] = None
        self.bn_atm_pe_ltp: Optional[float] = None
        self.nf_atm_ce_symbol: Optional[str] = None
        self.nf_atm_pe_symbol: Optional[str] = None
        self.nf_atm_ce_ltp: Optional[float] = None
        self.nf_atm_pe_ltp: Optional[float] = None
        # The strike these symbols were derived from — kept here (not just as
        # SchedulerService._bn_atm_watch_strike/_nf_atm_watch_strike, a plain
        # unlocked instance int used only for that method's own cheap
        # did-it-change check) so _build_payload can read strike + symbols +
        # LTP as ONE atomic group under _atm_watch_lock below. Reading the
        # scheduler's own unlocked copy here used to let _build_payload pair
        # the NEW strike (written first, before set_bn_atm_watch/set_nf_atm_
        # watch ran) with the PREVIOUS strike's still-stale symbol/LTP for
        # one payload push (2026-09-23 fix, found in review — the original
        # fix below only closed the race for the 4 symbol/LTP fields, not
        # this one too).
        self.bn_atm_watch_strike: Optional[int] = None
        self.nf_atm_watch_strike: Optional[int] = None
        # Guards the group writes above (MarketDataService.set_bn_
        # atm_watch/set_nf_atm_watch on a strike change, _process_tick's LTP
        # updates) against _build_payload's read of the same group, which
        # runs in a REAL executor thread (SchedulerService._push_dashboard_
        # loop) that can preempt mid-read — unlike the event-loop-only code
        # that writes these fields, which can't interleave with itself.
        # Without this lock, _build_payload could read a torn snapshot: the
        # NEW strike's ce/pe symbols paired with the PREVIOUS strike's
        # stale LTP (2026-09-23 fix, found in review).
        self._atm_watch_lock: threading.Lock = threading.Lock()

    @property
    def trades_today_combined(self) -> int:
        """
        The value SCALP_MAX_TRADES_PER_DAY actually gates on (2026-09-24 fix,
        found in review) — one shared count across BOTH instruments, matching
        risk_guardrails.max_trades_ok's own documented intent ("one shared
        cap... not a pair per instrument"). bn_trades_today/nf_trades_today
        individually still exist for the per-instrument diagnostic display.
        """
        return self.bn_trades_today + self.nf_trades_today

    def candle_lock(self, token: str) -> threading.Lock:
        """Return (and lazily create) the per-token candle lock."""
        try:
            return self._token_locks[token]
        except KeyError:
            with self._token_locks_meta:
                if token not in self._token_locks:
                    self._token_locks[token] = threading.Lock()
                return self._token_locks[token]


def get_state() -> AppState:
    return AppState()
