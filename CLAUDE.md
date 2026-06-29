# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An **NSE equity intraday paper-trading** app (FastAPI). It screens stocks pre-market with Gemini, streams live 5-minute data over WebSocket, evaluates a 7-signal long-only strategy **tick-by-tick**, simulates fills (no real broker), and persists everything to PostgreSQL. It also has a **backtest** engine that replays the identical strategy on historical data.

Single process, single Uvicorn worker by design — all state lives in one in-process singleton (`AppState`).

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
- There is **no test suite**. Validate changes by `python3 -m py_compile` across the tree and by reading the flow.

## External dependencies (not in this repo)

- **Market data server** `35.234.219.141` (self-signed cert, `verify=False`):
  - REST `:8000/api/historical-data/` — historical candles (POST, batched 100/req).
  - REST `:8000/api/clientstatus/` — the day's high-volume stock list `[[rank, name, token], ...]`.
  - WebSocket `:8083/historical-data` — live 5m + 1h ticks.
- **Gemini** (`google-genai` SDK) — pre-market news screen with Google-Search grounding.
- **PostgreSQL** via `asyncpg`.

## Architecture / daily flow

Driven by `scheduler.SchedulerService._phase_driver` (IST wall clock):

1. **09:00 PRE_MARKET** — `fetch_active_watchlist()` (client status) → `analyse_stocks()` (Gemini, returns bullish names) → `active_watchlist = {name: token}`. Empty Gemini result ⇒ trade the full list.
2. **09:15 WAIT_ZONE** — `_load_all_historical()` (5 days of 5m + today's 1h + NIFTY), then `market_data.start()` opens the WS.
3. **09:45–15:30 ACTIVE** — `_run_active_phase()` is a **tick-wise loop** every `TICK_EVAL_INTERVAL_MS` (default 100ms):
   - `_tick_exits` — every open position's live price vs SL/target.
   - `_tick_entries` — re-scan stocks that ticked since last cycle (`dirty_ticks`), on the **forming** 5m bar; fill those whose 7 signals align. Entries stop at 14:30 (CUTOFF); exits continue to 15:30.
   - Heavy indicator math (`scan_stock`) runs in `_SCAN_POOL` (ThreadPoolExecutor); fills/exits/DB stay on the event loop.
4. **15:30 CLOSED** — `_run_eod()` squares off survivors, writes `daily_stats`, resets all daily state.

Strategy core (shared by live **and** backtest, keep it that way): `check_trend` → `compute_indicators` → 7 conditions → `calc_quantity`.

**7 entry conditions:** near_support, bullish_pattern, adx_ok, rsi_ok (>30 or rising 3 bars), macd_bullish_cross, volume_surge, price_above_vwap. Plus a 4-gate trend filter: stock daily-green, stock hourly-green, NIFTY daily-green, NIFTY above session-VWAP.

**Risk guards (`can_enter`):** max 3 concurrent open positions, no same-day re-entry, ₹2000 daily loss limit, ₹500 risk/trade.

## Hard conventions — get these wrong and it breaks

- **Keying:** `candles_5m/1h` and `dirty_ticks` are keyed by **TOKEN** (numeric string). `ltp`, `positions`, `closed_positions`, `traded_today` are keyed by **SYMBOL NAME**. `active_watchlist = {name: token}` is the bridge. Always map correctly.
- **`positions` holds OPEN trades only.** Closing moves a position to `closed_positions` (in `paper_trade._finalize`). `len(positions)` is therefore a true *concurrent* count — do not reintroduce closed positions into it.
- **Locking:** candle lists are mutated by the WS thread and read by pool workers — every shared-candle access goes through `st.candle_lock(token)`; NIFTY lists through `st._nifty_lock`. Positions/`daily_pnl`/`dirty_ticks` are mutated only on the event-loop thread (no lock needed); pool workers only *read* them.
- **TA-Lib inputs:** pass raw NumPy `float64` arrays (built from candle slices), never DataFrames or `.ta` chains, into worker threads. Only the minimum lookback tail (`TALIB_LOOKBACK`) is materialized; session VWAP is the exception and uses today's bars.
- **Session VWAP = today only.** `compute_indicators` must receive today's bars as `session_candles_5m` (live derives this in `scan_stock`); passing the multi-day buffer computes a wrong multi-day VWAP.
- **No look-ahead in backtest:** at bar `t` an entry uses only bars `[..t]` and fills at `close[t]`; a position opened at `t` exits only on bars `> t`. Days are independent (fresh portfolio, EOD square-off) and run in parallel; trades merge in day order.

## Layout

```
main.py                      FastAPI app + lifespan (DB init → scheduler.start); serves / and /indicators
app/config.py                ALL tunables (timing, risk, strategy params, costs, intervals)
app/state.py                 AppState singleton (candles, ltp, positions, locks, dirty_ticks)
app/models.py                Candle (slots), Position, EntrySignal, IndicatorResult, TrendGate, enums
app/engine/
  entry_engine.py            scan_stock — the per-stock decision (live)
  indicator_engine.py        compute_indicators (TA-Lib), session_vwap_last, swing_low, patterns
  trend_filter.py            check_trend (pure), compute_nifty_gates
  position_manager.py        calc_quantity, can_enter (state injected, used by live + backtest)
app/services/
  scheduler.py               phase driver + tick-wise engine + EOD + dashboard payload
  market_data.py             WebSocket client; _process_tick updates candles/ltp/dirty_ticks
  historical_data.py         REST client (batched parallel fetch, persistent httpx)
  gemini_filter.py           analyse_stocks (google-genai, grounding + JSON-array schema)
  paper_trade.py             place_paper_order, check_tick_exit, force_close, _finalize
  database.py                asyncpg pool + schema + positions/scan_log/daily_stats/backtest tables
app/backtest/                data.py, engine.py, portfolio.py, fills.py, metrics.py
app/api/dashboard.py         REST + WS endpoints (/api/status, /api/indicators, /api/backtest[/{id}/trades|export.csv], /ws/dashboard, …)
app/ws/dashboard_ws.py       browser WS broadcast manager
static/                      index.html, indicators.html, js/dashboard.js, css/
```

Backtest is triggered from the dashboard: `POST /api/backtest {from_date, to_date, slippage_bps?, capital?}` runs in a background task and is polled via `GET /api/backtest/{id}`; results export at `…/export.csv`.

## Gotchas & known limitations

- **Pure tick-wise on the forming bar** (by design): RSI/MACD/ADX/volume are recomputed on the *incomplete* 5m bar each cycle, so they jitter and signals can appear/vanish within a bar. Volume-surge naturally fires late in each bar.
- **`GEMINI_MODEL`** — must be a real `google-genai` model id (currently `gemini-2.5-flash`). On any failure the screen returns `[]` and silently falls back to the full watchlist, so a bad id disables the AI filter without an error.
- **Daily-green gate** uses today's open = the open of today's first 5m bar (derived in `scan_stock` / `compute_nifty_gates`). The 1d series is no longer fetched or used; if you re-add it, remember it is not updated by the WS.
- **Backtest hourly gate** buckets by clock-hour from 5m data, which may not match the server's real 1h candle boundaries — possible live/backtest parity drift.
- **JSONB reads:** asyncpg returns `jsonb` columns as strings — decode with `_decode_jsonb` (see `database.py`) on any new read path.
- **Secrets:** `.env` is gitignored; never put real keys in `config.py` defaults or `.env.example` (GitHub push-protection will block, and it has happened here).

## Conventions for edits

- Keep `git` commits/pushes only when asked. `.env` must never be committed.
- Match the existing style: keyword-only dataclass construction, module-level `import app.config as cfg`, IST via `ZoneInfo("Asia/Kolkata")`.
- After changes, run `python3 -m py_compile` over the touched files (no test suite exists).
- Live and backtest **must** share the strategy core (`check_trend`/`compute_indicators`/`calc_quantity`); don't fork the decision logic.
