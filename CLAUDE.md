# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **NSE equity intraday paper-trading** app (FastAPI). It screens stocks pre-market with Gemini, streams live 5-minute data over WebSocket, evaluates a multi-signal long-only strategy **tick-by-tick** (8 conditions live, 7 in backtest — see parity caveat below), simulates fills (no real broker), and persists everything to PostgreSQL. It also has a **backtest** engine that replays the identical strategy on historical data.

Single process, single Uvicorn worker by design — all state lives in one in-process singleton (`AppState`).

**Everything strategy-related is runtime-dynamic.** All tunables (risk, strategy params, entry-condition/trend-gate toggles, session timings, costs, Gemini screen, tick cadence) are editable live from the `/settings` page — no restart — persisted in the `app_settings` table and re-applied on startup. The tradeable watchlist is editable mid-session from the dashboard. Backtests accept per-run strategy overrides that never touch live values. See "Dynamic settings layer" below.

## Run

```bash
# Docker (recommended — builds the TA-Lib C library)
cp .env.example .env          # set GEMINI_API_KEY + POSTGRES_DSN
docker compose up -d --build
docker compose logs -f app    # watch the phase driver

# Local (needs the TA-Lib C library installed first, plus a reachable Postgres)
pip install -r requirements.txt
python main.py                # serves http://0.0.0.0:8080
```

- Dashboard: `http://localhost:8080` (live status, positions, scans, backtest runner).
- `TA-Lib` (the Python pkg) needs the **native C library** present at build time — the Dockerfile compiles it; for a local venv install it on the host first (`brew install ta-lib`, or build from source).
- **Test suite:** `python3 tests/test_engine_synthetic.py` — an end-to-end conformance suite that replays the REAL backtest pipeline on synthetic candles with hand-computed expected results (entry window/cutoff, no-lookahead, gap fills, SL priority, delivery semantics, fill priority, risk modes, cost profiles, dedup, loss limit). It stubs `talib` (deterministic outputs) and ships a numpy shim, so it runs anywhere; under Docker it uses real numpy. Run it after touching the engine, `conditions.py`, `position_manager.py`, or `fills.py`. Also `python -m compileall -q app main.py` for the rest.

## External dependencies (not in this repo)

- **Market data server** `35.234.219.141` (self-signed cert, `verify=False`):
  - REST `:8000/api/historical-data/` — historical candles (POST, batched 100/req).
  - REST `:8000/api/clientstatus/` — the day's high-volume stock list `[[id, name, exchange_token, trading_symbol, instrumental_type], ...]`. `trading_symbol` (NOT `exchange_token`) is what the historical-data/WS API expects as `stock_symbol` — see the Keying gotcha below.
  - WebSocket `:8083/historical-data` — live 5m + 1h ticks.
- **Gemini** (`google-genai` SDK) — pre-market news screen with Google-Search grounding.
- **PostgreSQL** via `asyncpg`.

## Architecture / daily flow

Driven by `scheduler.SchedulerService._phase_driver` (IST wall clock):

1. **09:00 PRE_MARKET** — `fetch_active_watchlist()` (client status) → `full_watchlist = {name: token}` (ALL high-volume stocks). `analyse_stocks()` (Gemini) returns bullish names → `active_watchlist` = the **tradeable** subset. Empty/failed Gemini ⇒ fall back to the first `GEMINI_MAX_STOCKS` of the full list.
2. **09:15 WAIT_ZONE** — `_load_all_historical()` (5 days of 5m + today's 1h + NIFTY, fetched concurrently via `asyncio.gather`), then `market_data.start()` opens the WS — subscribed to the **active** (tradeable) subset only. Every WS group (primaries included) is chunked to ≤40 symbols per connection (server buffer limit); on any REconnect the day's 5m bars are REST-backfilled through the chronological upsert so an outage can't leave a splice in the candle series.
3. **09:45–15:30 ACTIVE** — `_run_active_phase()` is a **tick-wise loop** every `TICK_EVAL_INTERVAL_MS` (default 100ms):
   - `_tick_exits` — every open position's live price vs SL/target.
   - `_tick_entries` — re-scan stocks that ticked since last cycle (`dirty_ticks`), on the **forming** 5m bar; fill those whose signals all align. Entries stop at 14:30 (CUTOFF); exits continue to 15:30.
   - `_full_scan_all` — at entry and every 5 min, scans the **entire** `full_watchlist` to populate `indicator_snapshot` (for the `/indicators` page). Only `active_watchlist` stocks are `tradeable` and can fire signals; `scan_stock(..., tradeable=False)` updates indicators but never returns a signal. Non-Gemini stocks get no WS ticks, so this 5-min scan is their only indicator source.
   - Heavy indicator math (`scan_stock`) runs in `_SCAN_POOL` (ThreadPoolExecutor); fills/exits/DB stay on the event loop.
   - **Mid-session restart recovery:** if `active_watchlist` is empty on entering ACTIVE/CUTOFF, premarket + historical load run on the fly (retrying every 60s if the watchlist fetch fails). Today's positions/`traded_today`/`daily_pnl`/closed trades are then restored from the DB (`_restore_positions_from_db`) *before* the WS starts, and restored open symbols are force-added to the watchlists so their SL/target monitoring resumes. `_run_eod` performs the same restore so a restart after 15:30 still squares off orphaned OPEN rows and writes correct stats.
4. **15:30 CLOSED** — `_run_eod()` squares off survivors, writes `daily_stats`, resets all daily state, then sleeps to the next premarket.

Strategy core (shared by live **and** backtest, keep it that way): `check_trend` → `compute_indicators` → entry conditions → `calc_quantity`.

**Backtest fast path (same function, same math):** `compute_indicators` accepts keyword-only `ohlcv_window` (precomputed float64 array views that MUST end on the same bar as `candles_5m` — built once per symbol in `SymbolSeries.index_days`; when given, `candles_5m` only feeds the 3-bar pattern check, so the backtest passes just the last 3 bars), `session_vwap` (O(1) prefix-sum VWAP via `session_vwap_from_cumsums`), and `entry_short_circuit=True` (evaluates the cheap gates — support/pattern/VWAP/volume — first and skips the TA-Lib calls when one already vetoes the conjunctive entry; the returned `IndicatorResult` is then partial, which is fine because the caller rejects it). Live always calls with defaults so the dashboard gets the full snapshot. Don't reimplement any condition outside `compute_indicators` to "optimize" the backtest — pass precomputed inputs in instead.

**Entry conditions:** near_support, bullish_pattern, adx_ok, rsi_ok (>30 or rising 3 bars), macd_bullish_cross, volume_surge, price_above_vwap — **plus** `depth_bullish` (order-book buy-side ratio ≥ 0.4, i.e. not sell-skewed). Plus a 4-gate trend filter: stock daily-green, stock hourly-green, NIFTY daily-green, NIFTY above session-VWAP.

**Live = 8 conditions, backtest = 7 (parity caveat):** `depth_bullish` uses live order-book depth parsed from the WS `snap` field (`st.depth[symbol] = {bid, ask, spread, buy_qty, sell_qty, ratio}`). It defaults to **pass** when no snap data has arrived, so it only vetoes a clearly bearish book. Historical data has no order book, so the **backtest omits `depth_bullish`** — live is therefore slightly stricter than backtest.

**Risk guards (`can_enter`):** max 3 concurrent open positions, no same-day re-entry, ₹2000 daily loss limit. Per-trade risk (`calc_quantity`) is mode-switched by `RISK_MODE`: `fixed_amount` = ₹500 (`RISK_PER_TRADE`) or `capital_pct` = `RISK_CAPITAL_PERCENT`% of account capital per stop-out (true percent: 10 = 10%) — stop PLACEMENT defaults to the swing low (`MIN_SL_OFFSET` floor) in both; `SL_PCT`/`DELIVERY_SL_PCT` > 0 switches the stop to a true percentage of entry (no ₹ floor — the % is price-proportional), which is what lets `capital_pct` risk reach its full % at 1× delivery leverage. The `capital_pct` basis is the FULL account/run equity (`total_capital` arg — backtests pass `cap_total`), never the available remainder, or risk would decay as positions open. Delivery variants (`DELIVERY_RISK_MODE`/`DELIVERY_RISK_CAPITAL_PERCENT`) shadow these in positional replays.

## Dynamic settings layer

- `app/config.py` holds **static** system values (endpoints, DSN, pool/buffer sizes) as plain attributes, and **dynamic** tunables in `_DEFAULTS`, resolved via module `__getattr__` with precedence: *thread-local overrides (backtest run) → runtime overrides (Settings page / DB) → default*. `cfg.X` therefore always returns the current value.
- **Never copy a dynamic `cfg.X` into a module-level constant or default-argument value** — it freezes at import and silently stops being dynamic. Use `None` defaults resolved inside the function (see `calc_quantity`, backtest engine).
- **Adding a tunable = two places:** a default in `config._DEFAULTS` **and** a SPEC entry in `app/services/settings.py` (label/type/bounds/group/`bt` flag). Time-of-day settings are virtual `"HH:MM"` SPEC keys expanded to their `*_HOUR`/`*_MIN` config pairs.
- `cfg.thread_overrides(...)` may only be entered in **backtest worker threads** (`_simulate_day`), never on the event loop — live scan-pool threads must keep reading global values. Warmup days is passed explicitly into `load_backtest_data` for the same reason.
- Entry-condition toggles live in `app/engine/conditions.py` — one `_CONDITIONS` table drives `build_entry_checks`/`failed_entry_checks` (live, full diagnostics) and `entry_ok` (backtest short-circuit); a disabled condition auto-passes.
- **Custom entry rules** (`CUSTOM_ENTRY_RULES`, settings type `rules`): OR-of-ANDs clause groups over the `RULE_FIELDS` registry in `conditions.py`, validated by `validate_rules` (whitelists live beside the evaluator). Mode `and` = extra condition; `replace` = swaps out the fixed 8 (trend gates unaffected) **and disables `cheap_gates_veto`** — the short-circuit would wrongly reject rule sets that don't require the cheap conditions. Missing numeric data fails a clause EXCEPT `depth_ratio` (None→pass, live-only). It's a normal dynamic setting: persisted, resettable, self-healing, and per-run backtest-overridable. The Settings page renders it with a dedicated builder UI (`renderRuleBuilder` in settings.js), not the generic control. Trend-gate toggles are applied in `trend_filter.trend_blockers` (shared live + backtest); `check_trend` records the ACTUAL gate states so `Position.trend` / the DB `daily_green`/`hourly_green` columns stay honest diagnostics. The `entry_short_circuit` veto in `compute_indicators` calls `conditions.cheap_gates_veto`, which reads the same `_CONDITIONS` table — there is no separate copy to keep in sync.
- Session timings are dynamic: the phase driver sleeps in **≤30s chunks** (`_sleep_toward`) and re-evaluates; premarket/EOD are deduplicated per date via `self._premarket_date` / `self._eod_date` — do not remove these guards or premarket (Gemini calls) and EOD (stats overwrite) re-run every 30s.
- Manual watchlist edits (`POST /api/watchlist/add|remove`) mutate `active_watchlist`, restart the market-data WS (subscription filters are rebuilt on connect), and persist day-scoped under the `_WATCHLIST_OVERRIDES` key in `app_settings`; `_run_premarket` re-applies them after recovery/restart. Removal is refused (409) while the symbol has an open position.
- Backtest per-run overrides: `POST /api/backtest {overrides: {SPEC_KEY: value}}`, validated with `expand_changes(bt_only=True)` (only `bt: True` SPEC entries), recorded in `backtest_runs.params`, applied via `cfg.thread_overrides` inside each day worker.
- Settings API: `GET /api/settings` (grouped describe), `PUT /api/settings {changes}` (validate → persist atomically (`replace_app_settings`, one transaction) → apply; a value equal to its default deletes the override row), `POST /api/settings/reset {keys?}`. Session-time changes are cross-validated (`validate_time_order`) on save, on partial reset, at startup load (self-heals to default timings), and — scoped to the scan/cutoff pair only — on backtest overrides.

## Hard conventions — get these wrong and it breaks

- **Keying:** `candles_5m/1h` and `dirty_ticks` are keyed by **TOKEN** — this is `trading_symbol` (e.g. `"TATAMOTORS"`), NOT `exchange_token`: the historical-data/WS API correlates every request/response row by `stock_symbol`, and that field must be the real NSE trading symbol (`fetch_active_watchlist` in `app/engine/watchlist.py` builds it that way; nothing downstream assumes it's numeric — it's an opaque string key everywhere). `ltp`, `positions`, `closed_positions`, `traded_today`, `depth` (order-book snap) are keyed by **SYMBOL NAME**. `full_watchlist` (all high-volume) and `active_watchlist` (Gemini-tradeable subset) are both `{name: token}`; `token_to_name` (all tokens → name) is the reverse bridge the tick loop iterates. Always map correctly. **Caveat:** `NIFTY50_TOKEN = "99926000"` (in `app/config.py`) is a hardcoded static value predating this trading_symbol convention — unverified whether the market data server still resolves it under the new contract; if the NIFTY feed ever goes silent, check whether it now needs NIFTY's own `trading_symbol` instead.
- **`positions` holds OPEN trades only.** Closing moves a position to `closed_positions` (in `paper_trade._finalize`). `len(positions)` is therefore a true *concurrent* count — do not reintroduce closed positions into it.
- **Locking:** candle lists are mutated by the WS thread and read by pool workers — every shared-candle access goes through `st.candle_lock(token)`; NIFTY lists through `st._nifty_lock`. Positions/`daily_pnl`/`dirty_ticks` are mutated only on the event-loop thread (no lock needed); pool workers only *read* them.
- **Candle lists are strictly chronological.** `market_data._upsert/_upsert_list` update the in-progress bar on an equal `start_time`, append on a newer one, and **drop** stale out-of-order bars (reconnect replays) — the day-suffix walk in `scan_stock` and the pattern checks depend on this ordering; don't remove the guard.
- **`last_scan_results` order == recency.** `record_scan` pops-then-reinserts so the dashboard's `scan_snapshot()[-N:]` slices really are the N most recent scans; a plain key re-assignment would freeze them on first-inserted symbols.
- **TA-Lib inputs:** pass raw NumPy `float64` arrays (built from candle slices), never DataFrames or `.ta` chains, into worker threads. Only the minimum lookback tail (`TALIB_LOOKBACK`) is materialized; session VWAP is the exception and uses today's bars.
- **Session VWAP = today only.** `compute_indicators` must receive today's bars as `session_candles_5m` (live derives this in `scan_stock`); passing the multi-day buffer computes a wrong multi-day VWAP.
- **No look-ahead in backtest:** at bar `t` an entry uses only bars `[..t]` and fills at `close[t]`; a position opened at `t` exits only on bars `> t`. Backtests have a **mode** (`BACKTEST_MODE` / per-run `mode`, UI "Mode"): `intraday` = days are independent (fresh portfolio, EOD square-off) and run in parallel, trades merge in day order; `delivery` = **positional** (`_simulate_range_intraday`): one portfolio across the range, chronological, overnight holds (gaps resolve at the open via `_try_exit`), square-off at each symbol's last in-range bar. The `1d` timeframe is always positional (`_simulate_range_daily`) — its bars ARE days. In both positional modes `traded_today` = no re-entry per run and `DAILY_LOSS_LIMIT` = run-level loss stop. The per-bar exits→entries loop is shared (`_replay_day`) — don't fork it.

## Layout

```
main.py                      FastAPI app + lifespan (DB init → settings load → scheduler.start); serves /, /indicators, /settings
app/config.py                static system config + dynamic tunables (defaults, runtime overrides, thread-local backtest overrides)
app/state.py                 AppState singleton (candles, ltp, depth, positions, full/active watchlist, token_to_name, indicator_snapshot, locks, dirty_ticks)
app/models.py                Candle (slots), Position, EntrySignal, IndicatorResult, TrendGate, enums
app/engine/
  entry_engine.py            scan_stock — the per-stock decision (live)
  conditions.py              build_entry_checks / failed_entry_checks — 8 conditions + runtime toggles (shared live + backtest)
  indicator_engine.py        compute_indicators (TA-Lib), session_vwap_candles, patterns
  trend_filter.py            check_trend (gate toggles applied here), compute_nifty_gates
  position_manager.py        calc_quantity, can_enter (state injected, used by live + backtest)
app/services/
  scheduler.py               phase driver + tick-wise engine + EOD + dashboard payload
  market_data.py             WebSocket client; _process_tick updates candles/ltp/depth(snap)/dirty_ticks
  historical_data.py         REST client (batched parallel fetch, persistent httpx; JSON decode + Candle build run via asyncio.to_thread — keep them off the event loop)
  gemini_filter.py           analyse_stocks (google-genai, Search grounding; JSON array parsed from text)
  paper_trade.py             place_paper_order, check_tick_exit, force_close, _finalize
  settings.py                SPEC registry (labels/types/bounds/groups/bt flag), validation, override persistence
  snapshot.py                stub_entry / apply_depth — shared snapshot-entry helpers (STATE_UPDATE, INDICATOR_UPDATE, /api/indicators)
  database.py                asyncpg pool + schema + positions/daily_stats/backtest/app_settings tables
app/backtest/                data.py (SymbolSeries + per-symbol numpy mirrors/prefix sums), engine.py, portfolio.py, fills.py, metrics.py
app/api/dashboard.py         REST + WS endpoints (/api/status, /api/indicators, /api/backtest[/{id}/trades|export.csv], /ws/dashboard, …)
app/ws/dashboard_ws.py       browser WS broadcast manager
static/                      index.html, indicators.html, settings.html, js/dashboard.js, js/indicators.js, js/settings.js, css/
app/engine/watchlist.py      fetch_active_watchlist — client status → full_watchlist (normalises non-breaking spaces)
```

Backtest is triggered from the dashboard: `POST /api/backtest {from_date, to_date, slippage_bps?, capital?, overrides?}` runs in a background task and is polled via `GET /api/backtest/{id}`; results export at `…/export.csv`.

## WebSocket broadcast types

The `/ws/dashboard` endpoint broadcasts two distinct message shapes — **both pages connect to the same endpoint**:

| `type` | Cadence | Payload |
|--------|---------|---------|
| `STATE_UPDATE` | 1 s | Full dashboard payload: `clock`, `phase`, `wsStatus`, `niftyLtp`, `positions`, `scanResults`, `geminiList`, `watchlist`; `indicatorSnapshot` rides along only every 10th push (`_SNAPSHOT_EVERY_N_PUSHES`) — the deltas keep ticking symbols fresh |
| `INDICATOR_UPDATE` | ~100 ms (per dirty tick) | Only `indicatorSnapshot` — all other fields absent |

**Critical:** `dashboard.js` (main page) filters to `STATE_UPDATE` only — rendering on `INDICATOR_UPDATE` would blank every scalar field (nifty, clock, watchlist, etc.). `indicators.js` accepts both types and merges `indicatorSnapshot` into its local `rowsMap`.

## Gotchas & known limitations

- **Mid-session recovery phase restoration:** `_run_premarket()` sets `st.phase = PRE_MARKET` and `_run_wait_zone()` sets `st.phase = WAIT_ZONE` as side-effects, even when called from mid-session recovery inside `_phase_driver`. After each recovery sub-call, explicitly restore `st.phase` to the correct running phase (ACTIVE/CUTOFF) or the dashboard will show the wrong state for minutes.
- **`_build_indicator_snapshot` on `SchedulerService`:** the STATE_UPDATE payload's `indicatorSnapshot` comes from this static method, which fills in LTP stubs for `full_watchlist` stocks that haven't been scanned yet. The stub shape and order-book merge are shared helpers in `app/services/snapshot.py` (also used by the INDICATOR_UPDATE push and `/api/indicators`) — if you add new snapshot fields, extend `stub_entry`/`apply_depth` there.
- **Pure tick-wise on the forming bar** (by design): RSI/MACD/ADX/volume are recomputed on the *incomplete* 5m bar each cycle, so they jitter and signals can appear/vanish within a bar. Volume-surge naturally fires late in each bar. **The parity consequence is directional, not just noise:** live effectively ORs the entry conjunction over every tick moment in the bar (~3000 samples at 100ms) while the backtest samples once at bar close — live's per-bar entry probability is strictly ≥ backtest's, the extra entries skew toward intra-bar momentum spikes, and a live position can enter AND stop out inside one bar (structurally impossible in the backtest, which forbids exits on the entry bar). This is the single largest reason backtest metrics won't estimate live P&L exactly.
- **Live paper fills carry `SLIPPAGE_BPS`** (buy slipped up, sells slipped down, SL/target anchored on the slipped fill) — same model as the backtest's `fills.py`, so live paper P&L and backtest P&L share both the cost AND fill model. Conditions still evaluate at the bar's close price while sizing/fills use `st.ltp` — the paise-level divergence can only cause a safe rejection, never a mispriced trade.
- **Fill priority is deterministic and shared**: when more symbols signal on one bar/cycle than free position slots, both engines fill in `(sl_offset ÷ price, symbol)` order (tightest stop first) — see `_fill_signals` (backtest) and `_tick_entries` (live). Don't remove the sort; unordered fills make backtests non-reproducible and live/backtest books diverge arbitrarily.
- **Backtest entry window**: a bar is entry-eligible when it CLOSES by the cutoff (`_last_entry_start` = cutoff − timeframe), matching live's wall-clock stop. Grid-aligned defaults give the same bar set as the old `start < cutoff` check; off-grid cutoffs and coarse timeframes don't leak post-cutoff data anymore.
- **All conditions disabled = trades on trend gates alone.** Disabling every `COND_*` with custom rules off degenerates the strategy to "buy anything green at 09:45" (sizing guards still apply). Composition of documented per-condition auto-pass — intended mechanically, but there is no warning; don't "clean up" the auto-pass semantics without adding one.
- **Stale 1h feed fails closed**: `scan_stock` treats an hourly candle older than ~75min as missing (dead `primary-1h` connection), so the hourly gate blocks instead of gating on a frozen candle all day. The backtest synthesizes its hour candle from 5m data and is unaffected.
- **`GEMINI_MODEL`** — must be a real `google-genai` model id (currently `gemini-2.5-flash`). On any failure the screen returns `[]` and silently falls back to the (capped) full watchlist, so a bad id disables the AI filter without an error. Note: Google-Search grounding and a `response_schema` are **mutually exclusive** — `gemini_filter` uses grounding and parses the JSON array out of the text (`_find_json_array`); do not re-add `response_schema`.
- **Daily-green gate** uses today's open = the open of today's first 5m bar (derived in `scan_stock` / `compute_nifty_gates`). The 1d series is no longer fetched or used; if you re-add it, remember it is not updated by the WS.
- **The NIFTY feed has volume=0 on every bar (all timeframes — verified against the live server).** A pure session VWAP for the index is therefore always 0, and the `GATE_NIFTY_VWAP` gate would never pass. `session_vwap_candles` / `session_vwap_from_cumsums(…, cum_tp)` degrade to the session **TWAP** (mean typical price) when volume is absent — do not remove that fallback or the default-gated strategy silently stops trading.
- **Backtest hourly gate** buckets by clock-hour from 5m data, which may not match the server's real 1h candle boundaries — possible live/backtest parity drift.
- **JSONB reads:** asyncpg returns `jsonb` columns as strings — decode with `_decode_jsonb` (see `database.py`) on any new read path.
- **Position rows are day-scoped in SQL:** `update_position_exit` and `get_today_positions` both filter on `(created_at AT TIME ZONE 'Asia/Kolkata')::date` (backed by an expression index). Keep new position queries on that same expression — a rolling `NOW() - INTERVAL` window can touch the previous day's orphaned rows.
- **`calc_quantity` floors at 1 share** even when a single share risks more than `RISK_PER_TRADE` (stops wider than ₹500 on very high-priced stocks). Deliberate "always tradeable" choice — changing it alters live behavior and all backtest results.
- **Concurrent positions share the account.** Sizing uses *available* capital = `ACCOUNT_BALANCE − Σ(open value ÷ INTRADAY_LEVERAGE)` (live: computed in `scan_stock` + re-checked at fill in `_tick_entries`; backtest: `Portfolio.margin_used()` + per-fill re-check). Without this, each of the `MAX_CONCURRENT_POSITIONS` could consume the FULL leveraged buying power. Position value may still exceed raw capital by up to `INTRADAY_LEVERAGE`× — that's margin, not a bug; set leverage to 1 for cash-only sizing.
- **Frontend DOM diffing:** both `dashboard.js` and `indicators.js` maintain a `_rowEls` / `_posRowEls` cache (symbol → `<tr>`) and patch cells in-place via `_setCell(td, html, cls)` which no-ops when content is unchanged. Reorder uses `DocumentFragment` appended once (atomic). `scheduleRender()` + `requestAnimationFrame` coalesces rapid WS ticks into one paint. Do not replace this pattern with `tbody.innerHTML = ...` — it re-introduces flash.
- **Secrets:** `.env` is gitignored; never put real keys in `config.py` defaults or `.env.example` (GitHub push-protection will block, and it has happened here).

## Conventions for edits

- Keep `git` commits/pushes only when asked. `.env` must never be committed.
- Match the existing style: keyword-only dataclass construction, module-level `import app.config as cfg`, IST via `ZoneInfo("Asia/Kolkata")`.
- After changes, run `python3 -m py_compile` over the touched files (no test suite exists).
- Live and backtest **must** share the strategy core (`check_trend`/`compute_indicators`/`calc_quantity`); don't fork the decision logic.
