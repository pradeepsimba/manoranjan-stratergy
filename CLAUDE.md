# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Bank Nifty options paper-trading** app (FastAPI), ported from a standalone JS prototype (`c.html`). It streams live 5-minute BankNifty + 6 leader-stock data over WebSocket, evaluates a leader-momentum + composite-indicator strategy **tick-by-tick**, decides BUY (long ATM Call) / SELL (long ATM Put), simulates the option leg's premium via **synthetic Black-Scholes pricing** (no real options-chain data anywhere — see "Options pricing" below), and persists everything to PostgreSQL. It also has a **backtest** engine that replays the identical strategy on historical data.

Single process, single Uvicorn worker by design — all state lives in one in-process singleton (`AppState`). At most **one active trade** at a time (matches the source prototype — no concurrent positions).

**Everything strategy-related is runtime-dynamic.** All tunables (gates, risk, pricing, costs, session timings, tick cadence) are editable live from the `/settings` page — no restart — persisted in the `app_settings` table and re-applied on startup. Backtests accept per-run strategy overrides that never touch live values. See "Dynamic settings layer" below.

## Run

```bash
# Docker (recommended — builds the TA-Lib C library)
cp .env.example .env          # set POSTGRES_DSN
docker compose up -d --build
docker compose logs -f app    # watch the phase driver

# Local (needs the TA-Lib C library installed first, plus a reachable Postgres)
pip install -r requirements.txt
python main.py                # serves http://0.0.0.0:8080
```

- Dashboard: `http://localhost:8080` (live status, active trade, entry-loop diagnostics, backtest runner).
- `TA-Lib` (the Python pkg) needs the **native C library** present at build time — the Dockerfile compiles it; for a local venv install it on the host first (`brew install ta-lib`, or build from source).
- There is **no test suite**. Validate changes with `python -m compileall -q app main.py` and by reading the flow.

## External dependencies (not in this repo)

- **Market data server** `35.234.219.141` (self-signed cert, `verify=False`):
  - REST `:8000/api/historical-data/` — historical candles (POST, batched 100/req).
  - WebSocket `:8083/historical-data` — live 5m ticks.
  - **Protocol migration (2026-07-23):** the vendor switched `stock_symbol` from numeric Kite-style instrument tokens (e.g. `"1333"`) to real NSE trading-symbol strings (e.g. `"HDFCBANK"`) — the old tokens now silently return nothing for most instruments. `cfg.BN_ALL_STOCKS`/`BN_INDEX_TOKEN` hold the new symbol strings; verified the vendor's `stockname`-matching still accepts this repo's own internal ALL-CAPS display names unchanged (no dict keys needed to change, only the token/symbol *values*).
  - **Matching quirk (predates the migration, still holds):** the server matches a historical-data request by **`stockname` text**, not solely by `stock_symbol` — an exact-symbol-but-wrong-name request silently returns zero candles (bit us for Kotak: the canonical name is `"Kotak Bank"`, not `"Kotak Mahindra Bank"`). If a stock in `cfg.BN_ALL_STOCKS` ever starts returning empty history again, suspect a stockname/symbol mismatch first.
  - **BankNifty index now returns ZERO data, live or historical, under either the old or new identifier** (confirmed empirically 2026-07-23 — previously it at least returned the current day's partial session; the vendor now provides nothing for it at all). This is why the index candle is **synthesized** from the 11 constituent stocks' `BN_INDEX_WEIGHTS`-weighted % change (see `MarketDataService._update_synthetic_index` in `market_data.py`) whenever `st.bn_index_synthetic` is `True` — a one-way latch that permanently switches back to real ticks the instant the vendor ever resumes sending them. Synthetic bars still flow through the same `bn_index_bars` self-recording pipeline as real ones would have (see below) — don't build a second, parallel storage path for them.
- **PostgreSQL** via `asyncpg`.

## Architecture / daily flow

Driven by `scheduler.SchedulerService._phase_driver` (IST wall clock):

1. **09:00 PRE_MARKET** — no-op. The universe is a static config constant (`cfg.BN_INDEX_TOKEN` + `cfg.BN_ALL_STOCKS`), not fetched — there's nothing that can fail here (unlike the deleted equity engine's Gemini/watchlist screen).
2. **09:15 WAIT_ZONE** — `_load_all_historical()`: fetches 5 days of 5m history for the 11 BN stocks (fully archived, works fine) and MERGES (never replaces) today's BankNifty bars into whatever's already accumulated in `st.bn_index_candles_5m` from prior live sessions — see the historical-archive note above. Then `market_data.start()` opens a single WS connection subscribed to all 12 tokens (well under the server's ~40-entries-per-connection buffer limit, so no split connections needed like the old equity engine).
3. **09:30–15:00 ACTIVE** (then 15:00–15:30 CUTOFF, exits only) — `_run_active_phase()` is a **tick-wise loop** every `TICK_EVAL_INTERVAL_MS` (default 100ms), running directly on the event loop (no thread pool — only 12 instruments, unlike the equity engine's hundreds):
   - `_tick_exits` — if a trade is open, ratchet the trailing/breakeven stop and check target/stop against the live BankNifty price every cycle (this is also where the live option-premium mark is refreshed for the dashboard — Black-Scholes is closed-form, cheap enough to recompute every 100ms).
   - `_tick_entries` — wall-clock bar-close detection: tracks `st.last_evaluated_bar`; the instant the BankNifty 5m candle list's last bar changes to a NEW bar, evaluates exactly once against that just-closed bar (`bn_entry_exit.evaluate_entry`). No polling/pending-signal latch needed (the deleted JS prototype needed one because it only polled every 3s; 100ms is fast enough to catch a bar close directly).
4. **15:30 CLOSED** — `_run_eod()` squares off any survivor, writes `daily_stats`, **saves today's accumulated BankNifty bars to `bn_index_bars`** (the self-recording archive), persists `funds`, and resets per-day state. **`st.bn_index_candles_5m` is NOT cleared** — see below.

Strategy core (shared by live **and** backtest, keep it that way): `bn_signals.*` (gates) → `bn_entry_exit.evaluate_entry` / `evaluate_exit` → `bn_pricing.*` (strike/expiry/premium).

**c.html has no working backtest** (`runBacktest()` there is a confirmed empty stub) — this repo's backtest engine is a fresh design, not a port of anything, but it drives the exact same `evaluate_entry`/`evaluate_exit` functions the live scheduler calls. One deliberate improvement over what a literal port of the source's `checkExit` would have done: the backtest resolves SL/target touches with gap-at-open + intrabar high/low (see `app/backtest/fills.resolve_index_touch`), not a close-only check — there's no reference implementation to preserve fidelity with, so it follows this repo's own existing (equity-era) convention instead.

**Entry gates (all must align):** sideways-range filter (BankNifty's last 5 closes must span ≥ `BN_SIDEWAYS_RANGE_MIN` points) → momentum filter (single strong candle, 2-candle combo, or ATR-relative impulsive move) → leader-stock direction vote (≥ `BN_SAME_DIRECTION_REQUIRED` of 6 leaders agree) → per-leader volume-surge count (≥ same threshold) → composite BN indicator gate (RSI level + MACD zero-cross + leader candle-pattern tally + EMA20/50 stack, scored bull vs bear). BUY → long ATM Call; SELL → long ATM Put. Always exactly 1 lot (`cfg.BN_LOT_SIZE = 30`) — no position sizing formula, unlike the old equity engine's risk-based `calc_quantity`.

**"Strong quantity" gate:** a literal port of the source's fixed absolute per-stock threshold (`cfg.BN_STOCK_QTY_THRESHOLD`), compared against `Candle.last_qty` — the real per-trade quantity embedded in the vendor's current-protocol tick payload (parsed from the `quote` field's `"...qty N..."` text in `market_data.py`). An earlier vendor protocol had no per-tick quantity at all (only cumulative 5m bar volume, hundreds of thousands — wildly mismatched against these tens/hundreds-scale thresholds), which made this gate a permanently-satisfied no-op; that limitation no longer applies since the vendor migrated its protocol (2026-07-23) and now exposes a real per-trade qty field.

**Exit:** target/stop/breakeven/trailing on the underlying BankNifty index price (points, frozen at entry from `cfg.BN_TARGET_POINTS`/`BN_STOPLOSS_POINTS`/`BN_BREAKEVEN_TRIGGER`/`BN_TRAIL_TRIGGER`/`BN_TRAIL_DISTANCE` — a later Settings change never retroactively alters an already-open trade). Settlement is the **option premium P&L × lot size**, not raw index points (`(exit_premium - entry_premium) * 30`) — index points are kept on the trade only as a diagnostic (`index_pnl_points`).

**Options pricing — fully synthetic, no real option-chain data:** ATM strike = `round(spot/100)*100`; expiry = next weekly Thursday 15:30 IST; premium = standard Black-Scholes off BankNifty spot + a realized-volatility estimate from its own 5m bar-close log-returns (annualized via 75 bars/day × 252 days/year — a deliberate deviation from the source's tick-based estimator, required so live and backtest can share one `estimate_iv` implementation; backtest has no tick stream to replay). Risk-free rate, IV bounds, and default IV are all dynamic settings. See `app/engine/bn_pricing.py`.

**Self-recorded BankNifty history:** because the market-data server never provides BankNifty index history via the REST API (see above — now returns nothing at all, not even "today"), `scheduler._run_eod` writes the day's accumulated `bn_index_candles_5m` buffer into the `bn_index_bars` table every day (idempotent upsert by `start_time`) — since 2026-07-23 this buffer is populated by the synthetic index (see above) rather than real vendor ticks, but the recording pipeline itself is unchanged. `app/backtest/data.load_backtest_data` reads BankNifty history **from this table**, not from the REST API — the runnable backtest date range grows by one day at a time as the live engine runs, now backed by synthetic bars for any day recorded after the migration. The 11 BN stocks ARE fully archived on the external server, so they're still fetched directly for every backtest run.

## Dynamic settings layer

- `app/config.py` holds **static** system values (endpoints, DSN, the BN instrument universe, pool/buffer sizes, lot size) as plain attributes, and **dynamic** tunables in `_DEFAULTS`, resolved via module `__getattr__` with precedence: *thread-local overrides (backtest run) → runtime overrides (Settings page / DB) → default*. `cfg.X` therefore always returns the current value.
- **Never copy a dynamic `cfg.X` into a module-level constant or default-argument value** — it freezes at import and silently stops being dynamic. This is why `BNTrade`/`BTPosition` **freeze** their risk parameters (target/stoploss/breakeven/trail) from cfg exactly once, at entry (`bn_entry_exit.open_trade_from_signal`) — that's a deliberate snapshot for correctness (an open trade's economics must not shift under it), not the anti-pattern the rule above warns about.
- **Adding a tunable = two places:** a default in `config._DEFAULTS` **and** a SPEC entry in `app/services/settings.py` (label/type/bounds/group/`bt` flag). Time-of-day settings are virtual `"HH:MM"` SPEC keys expanded to their `*_HOUR`/`*_MIN` config pairs. An import-time assertion in `settings.py` raises `RuntimeError` if the two ever drift.
- `cfg.thread_overrides(...)` may only be entered in **backtest worker threads** (`_simulate_day`), never on the event loop.
- Backtest per-run overrides: `POST /api/backtest {overrides: {SPEC_KEY: value}}`, validated with `expand_changes(bt_only=True)` (only `bt: True` SPEC entries), recorded in `backtest_runs.params`, applied via `cfg.thread_overrides` inside each day worker.
- Settings API: `GET /api/settings` (grouped describe), `PUT /api/settings {changes}` (validate → persist atomically (`replace_app_settings`, one transaction) → apply; a value equal to its default deletes the override row), `POST /api/settings/reset {keys?}`. Session-time changes are cross-validated (`validate_time_order`); indicator-period combos are cross-validated (`validate_bn_indicator_periods`, e.g. MACD fast < slow, lookback ≥ what RSI/EMA/MACD need to converge) on save, on partial reset, and at startup load (self-heals to defaults).

## Hard conventions — get these wrong and it breaks

- **Keying:** `candles_5m` is keyed by **TOKEN** (numeric string) — the 11 BN stocks only; the BankNifty index has its OWN dedicated field, `bn_index_candles_5m` (a plain list, not a dict), with its own lock `st._bn_index_lock`. `ltp` is keyed by **SYMBOL NAME**; the index's live price is the separate `st.bn_index_ltp` float. Don't conflate the two — there is deliberately no "13th entry" in `candles_5m`/`ltp` for BankNifty.
- **`st.bn_index_candles_5m` is never cleared at EOD** (unlike everything else, which resets daily) — it's the ONLY source of multi-day BankNifty depth (the composite indicator gate needs `BN_INDICATOR_LOOKBACK_BARS`, default 200, bars to converge; the external server can never supply more than ~75 bars/day). It persists and grows across real trading days within one long-running process, capped at `MAX_CANDLE_BUFFER` (300 bars, ~4 sessions). A restart loses it; `_load_all_historical`'s merge-not-replace logic (via `MarketDataService._upsert_list`) is what lets it survive a same-process day rollover.
- **Single active trade.** `st.active_trade: Optional[BNTrade]` — not a dict. Closing sets it to `None` and appends to `st.closed_trades`. There is no concurrent-position concept anywhere in this engine (deliberately, matching the source).
- **Locking:** `candles_5m` token locks and `_bn_index_lock` guard the WS-thread-vs-event-loop boundary — `MarketDataService._process_tick` (WS callback context) and the scheduler's tick loop are the only two touchpoints, and both go through `st.candle_lock(token)` / `st._bn_index_lock`.
- **Candle lists are strictly chronological.** `market_data._upsert`/`_upsert_list` update the in-progress bar on an equal `start_time`, append on a newer one, and **drop** stale out-of-order bars (reconnect replays). `_load_all_historical`'s catch-up merge reuses `MarketDataService._upsert_list` directly (not a second copy) so there's exactly one implementation of "how a BankNifty bar gets folded in," whether it arrives via WS tick or REST catch-up.
- **TA-Lib inputs:** pass raw NumPy `float64` arrays, never DataFrames, into `bn_signals.py`'s RSI/EMA calls. `bn_signals.bn_composite_indicator`'s MACD is **not** `talib.MACD`(which would force a signal line) — it's two plain `talib.EMA` calls subtracted, matching the source's raw EMA12-EMA26 zero-cross with no signal line at all.
- **Shared decision core, not just shared formulas:** `bn_entry_exit.evaluate_exit` reads risk parameters (target/breakeven/trail) FROM THE TRADE OBJECT it's given, not from `cfg` — and it's called with either a live `BNTrade` (`app/models.py`) or a backtest `BTPosition` (`app/backtest/portfolio.py`) interchangeably, because the two dataclasses share the exact field names `evaluate_exit` touches (`direction`, `entry_index_price`, `current_sl`, `sl_stage`, `target`, `trail_trigger`, `trail_distance`, `breakeven_trigger`, `strike`, `option_type`, `expiry`). Don't rename a field on one without the other — this duck-typing is how backtest and live stay provably in sync without a shared decision function needing two call signatures.
- **No look-ahead in backtest:** at bar `t` an entry uses only bars `[..t]`; IV/expiry/premium are computed from bar `t`'s own timestamp and closes `[..t]` only. A position opened at bar `t` exits only on bars `> t`. Backtest is **intraday-only** (fresh portfolio per day, EOD square-off, days run in parallel) — there is no positional/delivery/1d mode (nothing in the source ever holds an option position overnight).
- **Options are cash-only, always exactly 1 lot** — there is no leverage/margin concept and no quantity-sizing formula anywhere in this engine (unlike the deleted equity engine's `calc_quantity`/`INTRADAY_LEVERAGE`).

## Layout

```
main.py                      FastAPI app + lifespan (DB init → settings load → scheduler.start); serves /, /settings
app/config.py                static system config (incl. BN_INDEX_TOKEN/BN_ALL_STOCKS/BN_LEADER_STOCKS/BN_LOT_SIZE) + dynamic tunables
app/state.py                 AppState singleton (candles_5m by token, bn_index_candles_5m, ltp, bn_index_ltp, active_trade, closed_trades, funds, bn_diagnostic, locks)
app/models.py                Candle (slots), BNSignal, BNTrade, BNDiagnostic, enums
app/engine/
  bn_pricing.py               ATM strike/expiry/Black-Scholes/normal_cdf/estimate_iv — pure, no state
  bn_signals.py                sideways/momentum/leader-vote/candle-pattern/EMA-stack/composite-indicator gates
  bn_entry_exit.py            evaluate_entry/evaluate_exit — the shared live+backtest decision core; open_trade_from_signal/finalize_exit helpers
  bn_breakout.py              swing/S-R/pivot breakout detection + weighted global signal — a c.html UI-parity port for the Stock Candles panel, entirely separate from the BN trading strategy above (never feeds evaluate_entry/evaluate_exit)
app/services/
  scheduler.py                phase driver + tick-wise engine + EOD + dashboard payload + the 15m S/R refresh loop (Stock Candles panel only)
  market_data.py              single WS connection (BankNifty + 11 stocks); _process_tick updates candles_5m/bn_index_candles_5m/ltp/bn_index_ltp
  historical_data.py          REST client (batched parallel fetch, persistent httpx)
  bn_trade.py                 place_paper_order, check_tick_exit, force_close — paper-order lifecycle + funds/daily_pnl bookkeeping
  settings.py                 SPEC registry (labels/types/bounds/groups/bt flag), validation, override + funds persistence (BN_FUNDS_KEY)
  database.py                 asyncpg pool + schema + positions/daily_stats/backtest/app_settings/bn_index_bars tables
app/backtest/                 data.py (SymbolSeries + BankNifty from bn_index_bars), engine.py (evaluate_entry/evaluate_exit-driven replay), portfolio.py, fills.py, metrics.py (unchanged, instrument-agnostic)
app/api/dashboard.py          REST + WS endpoints (/api/status, /api/backtest[/{id}/trades|export.csv], /ws/dashboard, …)
app/ws/dashboard_ws.py        browser WS broadcast manager
static/                       index.html, settings.html, css/dashboard.css
  js/dashboard.js              WS connect/render loop, backtest UI, local IndexedDB trade log, CSV export, auto-screenshot, Trade Conditions modal
  js/breakout.js                Stock Candles panel renderer (global signal / breakout banner / 12-stock candle table / S-R table)
  js/qtyAudit.js                Big Trades qty-audit — browser-local IndexedDB tick log, fed by TICK_UPDATE
  js/kiteForm.js                Kite manual-order form — DECORATIVE ONLY (c.html port), never calls the real paper-trading engine
  js/settings.js                Settings page renderer
scripts/bn_smoke_test.py      throwaway dev tool — NOT shipped functionality; feeds real historical bars through evaluate_entry/evaluate_exit outside the app
```

Backtest is triggered from the dashboard: `POST /api/backtest {from_date, to_date, slippage_bps?, overrides?}` runs in a background task and is polled via `GET /api/backtest/{id}`; results export at `…/export.csv`. There is no `timeframe`/`mode`/`capital` field — backtest is always 5m/intraday/1-lot.

**Stock Candles panel (`bn_breakout.py` + the dashboard's `stockCandles`/`globalSignal`/`breakout`/`srLevels` payload fields)** is a c.html UI-parity port — a breakout/support-resistance scanner and `BN_INDEX_WEIGHTS`-weighted global signal over the same 12-instrument universe the BN strategy already streams. It is purely informational and **never** feeds `evaluate_entry`/`evaluate_exit` — don't wire it into the trading decision. 5m S/R is computed on the fly from in-memory candles every `STATE_UPDATE`; 15m S/R comes from a separate periodic REST refresh (`SchedulerService._refresh_15m_sr_loop`, every 5 min) since this app otherwise never streams/fetches 15m candles — the live WS subscription filters stay 5m-only.

**The Kite manual-order form (`js/kiteForm.js`) and the Local Trade Log (browser IndexedDB, in `dashboard.js`) are deliberately NOT wired to the real paper-trading engine** — matching c.html's own behavior and an explicit user decision:
- Kite form Submit/Exit only mutate local JS state + a client-side Black-Scholes preview (localStorage funds) — never call a backend endpoint, never touch `st.active_trade`.
- The Local Trade Log is a second, browser-only record of the REAL server trades (mirrored from `STATE_UPDATE`'s `activeTrade`/`closedTrades`), kept deliberately separate from "Today's Trades" (the real, Postgres-backed history) rather than reading `/api/positions`. Its IndexedDB store is keyed by a derived `localId` (`${orderId}_ENTRY`/`${orderId}_EXIT`) and written with `put()`, not `add()` — a page reload always re-delivers the current trade state, so idempotent overwrite (not an in-memory dedupe set, which can't survive a refresh) is what prevents duplicate rows.

## WebSocket broadcast types

The `/ws/dashboard` endpoint broadcasts two distinct message shapes — **the page filters to `STATE_UPDATE` only**:

| `type` | Cadence | Payload |
|--------|---------|---------|
| `STATE_UPDATE` | 1 s | Full dashboard payload: `clock`, `phase`, `wsStatus`, `apiStatus`, `bnLtp`, `dailyPnl`, `funds`, `activeTrade`, `closedTrades`, `entryLoop` (the `BNDiagnostic` — the "why didn't it fire" panel) |
| `TICK_UPDATE` | ~100 ms | `{prices: {name: ltp, ...}}` — a live-price delta only; not rendered by the current dashboard.js (present for future use), but must not be treated as a `STATE_UPDATE` |

## Gotchas & known limitations

- **The BankNifty history-archive limitation (see "External dependencies" above) is the single most important thing to remember when touching backtest or the historical loader.** If a backtest run fails with "no self-recorded BankNifty history," that's expected until the live engine has run at least one full day in this environment — it is NOT a bug to "fix" by fetching a date range from the REST API.
- **Kotak Bank naming:** `cfg.BN_ALL_STOCKS["KOTAK BANK"] = "1922"` — NOT `"KOTAK MAHINDRA BANK"`. The market-data server matches by exact stockname text; confirmed by direct testing that the "Mahindra" variant silently returns zero candles for the identical, correct token.
- **Pure tick-wise on the forming bar** (by design, inherited intentionally from the source): the composite indicator gate is recomputed on the *incomplete* forming 5m bar each cycle if you feed it partial data — but `evaluate_entry` is only ever called once per bar (on the just-closed bar, via `last_evaluated_bar` dedup), so this repo's BN engine does NOT re-fire mid-bar the way the old equity engine's tick loop did.
- **`BNTrade.current_premium`/`current_iv`** are refreshed on every `_tick_exits` cycle (even when not exiting) purely for the live dashboard mark — don't confuse this with `entry_premium`/`exit_premium`, which are the two values that actually determine `pnl`.
- **JSONB reads:** asyncpg returns `jsonb` columns as strings — decode with `_decode_jsonb` (see `database.py`) on any new read path.
- **Position rows are day-scoped in SQL:** `get_today_positions` filters on `(created_at AT TIME ZONE 'Asia/Kolkata')::date` (backed by an expression index); `update_position_exit` now keys on `order_id` (not `symbol` — every row's symbol is just `"BANKNIFTY"`, so `order_id` is the only thing that disambiguates same-day trades).
- **Options cost model rates are placeholders** (`BN_COST_*` settings) — not verified against current India options STT/exchange-txn figures. Safe for relative backtest signal quality (win rate, R-multiple), not yet for trusting absolute ₹ P&L.
- **Frontend DOM diffing:** `dashboard.js` maintains cell-level diffing via `_setCell(td, html, cls)` (no-ops when content is unchanged) and coalesces WS pushes into one paint per animation frame (`scheduleRender` + `requestAnimationFrame`). Do not replace this pattern with `tbody.innerHTML = ...` — it re-introduces flash.
- **Secrets:** `.env` is gitignored; never put real keys in `config.py` defaults or `.env.example`.

## Conventions for edits

- Keep `git` commits/pushes only when asked. `.env` must never be committed.
- Match the existing style: keyword-only dataclass construction, module-level `import app.config as cfg`, IST via `ZoneInfo("Asia/Kolkata")`.
- After changes, run `python3 -m py_compile` over the touched files (no test suite exists).
- Live and backtest **must** share the strategy core (`bn_entry_exit.evaluate_entry`/`evaluate_exit`, `bn_pricing.*`, `bn_signals.*`); don't fork the decision logic. If live and backtest need different treatment of the same moment (e.g. intrabar SL/target touch resolution), do it at the CALLER level (the scheduler vs. the backtest replay loop), never by branching inside the shared functions themselves.
