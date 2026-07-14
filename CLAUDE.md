# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Alto** is a **multi-user equity paper-trading platform** (FastAPI), rebuilt from a prior
single-user, auto-firing BankNifty *options* trading bot. It streams live 5-minute candle data
for a discovered universe of NSE equities over WebSocket, lets each logged-in user manually place
BUY/SELL orders (Market or Limit, CNC/delivery or MIS/intraday) against their own virtual funds,
and persists every user's holdings/positions/orders to PostgreSQL.

**There is no trading strategy anywhere in this codebase.** Nothing auto-fires an order — a trade
happens only when a user submits one from the UI. The only "engine" (`app/engine/orders.py`) is
mechanical order-matching (funds/qty checks, limit-price crossing, EOD square-off), not a signal
generator. If you're asked to add auto-trading logic, stop and confirm that's really wanted —
it goes against this repo's whole reason for existing in its current form.

Single process, single Uvicorn worker by design — shared market data (candles, live prices,
connection status) lives in one in-process singleton (`AppState`); **per-user data (funds,
holdings, positions, orders) is NEVER cached in that singleton** — it's read/written straight
from Postgres on every request, since a process-wide singleton can't hold N users' account state.

## Run

```bash
# Docker (recommended)
cp .env.example .env          # set POSTGRES_DSN and SESSION_SECRET
docker compose up -d --build
docker compose logs -f app

# Local (needs a reachable Postgres)
pip install -r requirements.txt
python main.py                # serves http://0.0.0.0:8080
```

- App: `http://localhost:8080` — redirects to `/login` if you don't have a session; register an
  account (seeded with `cfg.STARTING_FUNDS`, default ₹5,00,000) to get in.
- There is **no test suite**. Validate changes with `python -m compileall -q app main.py`, a
  `node --check` pass over any touched `static/js/*.js`, and by reading the flow.
- `scripts/discover_instruments.py` — throwaway dev tool, NOT shipped functionality. Re-runs
  instrument discovery/verification standalone (see below) without starting the whole app.

## External dependencies (not in this repo)

- **Market data server** `35.234.219.141` (self-signed cert, `verify=False`):
  - REST `:8000/api/historical-data/` — historical candles (POST, batched via `HIST_BATCH_SIZE`).
  - REST `:8000/api/clientstatus/` — the server's own instrument catalog: a JSON array of
    `[id, name, token]` triples. This is the **source of truth** for `stockname` text (see the
    matching quirk below) — instrument discovery calls this live rather than hardcoding names.
  - WebSocket `:8083/historical-data` — live 5m ticks, subscribed via one `LIVE_FEED_INIT`
    message per connection (`{type, filters: [{stock_symbol, stockname, interval}], latestOnly}`).
  - **Matching quirk:** the server matches a historical-data request by **`stockname` text**, not
    solely by `stock_symbol` token — an exact-token-but-wrong-name request silently returns zero
    candles. This is why instrument discovery (`app/services/instrument_discovery.py`) sources
    names from `/api/clientstatus/` instead of any hardcoded/guessed list, and why
    `cfg.SEED_STOCK_CANDIDATES` (the offline fallback if that endpoint is unreachable) is a literal
    snapshot of that same endpoint's real response, not hand-typed guesses.
  - **~40 (symbol × interval) pairs per WebSocket connection** is the server's practical ceiling on
    one socket's filter list — `market_data.py` shards the instrument universe into batches of
    `cfg.WS_FILTER_BATCH_SIZE` (40) and runs one connection per batch concurrently.
  - Being listed in `/api/clientstatus/` does **not** guarantee historical OHLC exists (e.g. a
    very recent listing) — discovery only marks a symbol `tradable` after actually fetching a few
    days of candles for it and confirming a non-empty result.
- **PostgreSQL** via `asyncpg` — now a genuinely multi-user schema (`users`, `instruments`,
  `orders`, `holdings`, `positions`, plus the generic `app_settings` KV store).

## Architecture / daily flow

Driven by `scheduler.SchedulerService._phase_driver` (IST wall clock), now a 3-phase clock (no
strategy-specific WAIT_ZONE/CUTOFF split — those existed for entry-gate timing that no longer
exists):

1. **PRE_MARKET** (before `MARKET_OPEN_HOUR:MIN`, default 09:15) — idle, no live feed.
2. **OPEN** (`MARKET_OPEN` to `MARKET_CLOSE`, default 09:15–15:30) — on first entry: if
   `instruments` is empty, runs discovery (`instrument_discovery.discover_and_verify`) once; loads
   5 days of history for every tradable instrument into `AppState.candles_5m`; starts the (possibly
   multi-connection) live feed. Every `TICK_EVAL_INTERVAL_MS` (default 100ms), `_tick_loop` drains
   whatever tokens just ticked and, for each: checks resting LIMIT orders for a price-crossing fill
   (`app/engine/orders.match_pending_limit_orders`) and folds the tick into the shared
   `WATCHLIST_TICK` broadcast delta. At `MIS_SQUAREOFF_HOUR:MIN` (default 15:20, once per day):
   every user's open MIS position is auto-closed at the last known price
   (`order_engine.eod_square_off_all_mis`), each square-off logged as a real MARKET order for
   audit-trail parity with a manually-placed exit.
3. **CLOSED** (after `MARKET_CLOSE`) — the live feed is stopped for the day (`_run_eod`). No
   per-day state is cleared otherwise — `candles_5m`/`ltp` just keep rolling (capped at
   `MAX_CANDLE_BUFFER`), refreshed by the next day's historical reload.

Order lifecycle lives entirely in `app/engine/orders.py`, called from the API layer
(`app/api/trading.py`, user-initiated) and the scheduler (limit-order matching, EOD square-off) —
**never** from anything resembling a signal evaluator. See that file's module docstring for the
funds-ledger rule (`BUY` debits `qty*price`, `SELL` credits `qty*price`, uniformly for CNC and
MIS, long and short — this single rule is what keeps the ledger self-consistent without a
separate margin/reservation system).

## Dynamic settings layer

- `app/config.py` holds **static** system values (market-data endpoints, DSN, session secret,
  pool/buffer sizes, the `SEED_STOCK_CANDIDATES` fallback catalog) as plain attributes, and
  **dynamic** tunables in `_DEFAULTS`, resolved via module `__getattr__` with precedence: runtime
  overrides (Settings page / DB) → default. `cfg.X` therefore always returns the current value.
- **Never copy a dynamic `cfg.X` into a module-level constant or default-argument value** — it
  freezes at import and silently stops being dynamic.
- **Adding a tunable = two places:** a default in `config._DEFAULTS` **and** a SPEC entry in
  `app/services/settings.py` (label/type/bounds/group). An import-time assertion in `settings.py`
  raises `RuntimeError` if the two ever drift.
- Settings API: `GET /api/settings` (grouped describe), `PUT /api/settings {changes}` (validate →
  persist atomically (`replace_app_settings`, one transaction) → apply; a value equal to its
  default deletes the override row), `POST /api/settings/reset {keys?}`. Session-time changes
  are cross-validated (`validate_time_order`: market open ≤ MIS square-off ≤ market close) on
  save, on partial reset, and at startup load (self-heals to defaults on drift).
- There is no per-backtest-run override concept anymore (the backtest engine is gone) — every
  dynamic tunable is simply a live, global, persisted value.

## Hard conventions — get these wrong and it breaks

- **Keying is unified on TOKEN everywhere** — `candles_5m`, `tick_version`, and `ltp` are all
  `Dict[token, ...]` in `AppState`. This is a deliberate simplification over the old BN engine's
  split (candles by token, LTP by name) — don't reintroduce a name-keyed map for anything that
  needs to correlate with `instruments.token` (the DB primary key).
- **No per-user data in `AppState`.** Funds/holdings/positions/orders are Postgres-only, read
  fresh per request (`DatabaseService` methods) or pushed via `AccountWSManager` on change. Do
  not add a `active_trade`/`funds` field back onto the shared singleton — it can't represent
  multiple users and the last rewrite's entire point was fixing that.
- **Two WebSocket managers, two trust levels.** `app/ws/market_ws.py` (`MarketWSManager`,
  `/ws/market`) is a **public, unauthenticated broadcast** — only ever shared data (prices,
  candles, phase/status) goes through it. `app/ws/account_ws.py` (`AccountWSManager`,
  `/ws/account`) is **per-user, authenticated at connect** via the session cookie
  (`app/services/auth.user_id_from_session`) — order fills, funds/holdings/positions changes go
  through this one, targeted only at that user's own sockets (`Dict[user_id, Set[WebSocket]]`).
  Never broadcast account data on the public channel.
- **Locking:** `candles_5m` token locks (`AppState.candle_lock(token)`) guard the WS-thread-vs-
  event-loop boundary — `MarketDataService._process_tick` (WS callback context) and any reader
  are the only touchpoints, both going through `st.candle_lock(token)`.
- **Candle lists are strictly chronological.** `market_data._upsert` updates the in-progress bar
  on an equal `start_time`, appends on a newer one, and **drops** stale out-of-order bars
  (reconnect replays) — the one implementation used both for live WS ticks and the historical
  catch-up load.
- **Order-matching is mechanical, not a strategy.** `app/engine/orders.py`'s `place_order`/
  `execute_fill`/`match_pending_limit_orders` decide *whether an order the user already placed can
  execute right now* — never *what* to trade. Keep it that way; if a feature needs to decide
  *which* instrument/direction/quantity to trade on its own, that is exactly the kind of logic
  this rebuild removed, and it does not belong back in this engine.
- **CNC never shorts; MIS can.** `product=CNC` SELL requires existing `holdings.qty >= qty` (real
  delivery equity can't be shorted). `product=MIS` SELL with no/insufficient existing long
  position opens or extends a short `positions` row — closed out by a later opposite-side MIS fill
  or the EOD square-off sweep. Don't add margin/leverage modeling — orders are cash-only by design
  (a `BUY` always requires the full `qty*price` in funds up front).
- **Single source of truth for the tradable universe:** the `instruments` table, populated by
  `instrument_discovery.discover_and_verify()` (see "External dependencies" above). Don't
  reintroduce a hardcoded stock dict as the live universe — `cfg.SEED_STOCK_CANDIDATES` is a
  discovery *fallback seed*, never read directly by the trading/market-data code paths.

## Layout

```
main.py                      FastAPI app + lifespan (DB init → settings load → scheduler.start,
                              SessionMiddleware); serves /, /login, /holdings, /positions,
                              /orders, /console, /settings
app/config.py                static system config (market-data endpoints, DSN, session secret,
                              SEED_STOCK_CANDIDATES) + dynamic tunables
app/state.py                 AppState singleton — SHARED market data only (candles_5m by token,
                              tick_version, ltp, ws/api status, locks); no per-user fields
app/models.py                Candle (slots), MarketPhase/OrderSide/OrderType/Product/
                              OrderStatus/PositionStatus enums
app/engine/
  orders.py                   place_order/execute_fill/match_pending_limit_orders/cancel_order/
                              square_off_position/eod_square_off_all_mis — mechanical order
                              execution, NOT a strategy (see "Hard conventions" above)
app/services/
  scheduler.py                phase driver (PRE_MARKET/OPEN/CLOSED) + tick loop (limit-order
                              matching + tick-delta broadcast) + MIS square-off + EOD feed stop
  market_data.py              sharded WS connections (one per <=40-pair batch) covering the
                              discovered instrument universe; _process_tick updates
                              candles_5m/ltp/tick_version, all keyed by token
  historical_data.py          REST client (batched parallel fetch, persistent httpx) — unchanged
                              from the prior engine, zero strategy coupling
  instrument_discovery.py     builds the tradable universe from /api/clientstatus/, verified
                              against real historical candle availability before persisting
  auth.py                     pbkdf2_hmac password hashing, get_current_user FastAPI dependency,
                              session-cookie helpers (register/login/logout live in app/api/auth.py)
  settings.py                 SPEC registry (labels/types/bounds/groups), validation, override
                              persistence (session timings, starting funds, tick cadence)
  database.py                 asyncpg pool + schema + users/instruments/orders/holdings/
                              positions/app_settings CRUD; plus read-only Console/Journal
                              aggregates (get_user_journal, get_completed_orders,
                              get_trade_stats, get_realized_pnl_total/_by_symbol) — all
                              derived over orders+positions, no separate ledger table
app/api/
  auth.py                      POST /api/auth/{register,login,logout}, GET /api/auth/me
  market.py                    GET /api/status, /api/instruments, /api/instruments/{token}/candles,
                              settings endpoints, public WS /ws/market
  trading.py                   authenticated: /api/orders (GET/POST/DELETE), /api/holdings,
                              /api/positions (+ /{id}/exit), /api/funds, authenticated WS /ws/account;
                              plus read-only reporting: /api/journal (activity ledger with a
                              running funds balance walked BACKWARD from current funds so the top
                              row always reconciles to live funds), /api/console/summary,
                              /api/console/tradebook, /api/console/pnl — all derived, never mutate
app/ws/
  market_ws.py                 MarketWSManager — public broadcast (prices/candles/status only)
  account_ws.py                AccountWSManager — per-user targeted push (order/position/funds)
static/                       login.html, index.html (terminal), holdings.html, positions.html,
                              orders.html, console.html (reports/tradebook/journal, tabbed),
                              settings.html, css/dashboard.css
  js/util.js                   shared: apiFetch/apiGet/apiPost/apiDelete, connectWS (WS + 3s
                              reconnect), toast, lineChart, number/date formatters
  js/app.js                    shared bootstrap for every authenticated page: auth gate (redirect
                              to /login), header clock/phase/ws-status wiring, funds display,
                              owns the /ws/market and /ws/account connections and re-dispatches
                              their messages as 'market:tick' / 'account:update' CustomEvents
  js/auth.js                   login.html only — login/register form
  js/watchlist.js, orderTicket.js   index.html only — watchlist + chart + order ticket
  js/holdings.js, positions.js, orders.js   one per matching page
  js/console.js                console.html only — Overview/Tradebook/Journal tabs (lazy-loaded),
                              CSV export, refreshes on 'account:update'; read-only reporting
  js/settings.js               settings.html — generic spec-driven renderer, unchanged shape
scripts/discover_instruments.py   throwaway dev tool — NOT shipped functionality
```

## WebSocket message types

| Channel | `type` | Cadence | Payload |
|---|---|---|---|
| `/ws/market` (public) | `MARKET_STATE` | 1 s | `clock`, `phase`, `wsStatus`, `apiStatus` |
| `/ws/market` (public) | `WATCHLIST_TICK` | ~100 ms | `{prices: {token: ltp, ...}}` — delta only |
| `/ws/account` (per-user) | `ORDER_UPDATE` | on fill/reject/cancel | `{order: {...}, holding?/position?}` |
| `/ws/account` (per-user) | `POSITIONS_UPDATE` | on manual/EOD square-off | `{position: {...}, order: {...}}` |

## Gotchas & known limitations

- **The market-data server's `stockname`-text matching quirk is the single most important thing
  to remember when touching instrument discovery or the historical loader.** Don't hardcode a
  new symbol's name — source it from `/api/clientstatus/` (live, or the `SEED_STOCK_CANDIDATES`
  snapshot as a documented fallback) and let discovery verify it has real candle history before
  trusting it.
- **No cost/brokerage model.** Fills are at exact LTP (market) or exact limit price (limit) with
  no slippage, brokerage, STT, or exchange-charge simulation — intentional for this rebuild's
  scope; add a cost model in `app/engine/orders.py`'s `execute_fill` if that's ever wanted, not
  by resurrecting the old BN engine's placeholder cost-rate settings (deleted, and were never
  verified against real rates anyway).
- **JSONB reads:** asyncpg returns `jsonb` columns as strings — decode with `_decode_jsonb` (see
  `database.py`) on any new read path that touches a jsonb column (currently only `app_settings`).
- **Session auth only — no OAuth, no email verification, no password reset.** `SESSION_SECRET`
  in `.env` must be a real random value in any non-throwaway deployment; the code default is
  intentionally insecure so it's obvious you haven't set one.
- **Frontend has no build step.** Plain HTML/CSS/vanilla JS served directly by FastAPI's
  `StaticFiles` mount — there's no bundler, no framework, no JSX. Every page loads `util.js` then
  `app.js` (except `login.html`, which loads `auth.js` instead of `app.js` since it's the one
  page reachable without a session) then its own page script.
- **Secrets:** `.env` is gitignored; never put real values in `config.py` defaults or `.env.example`.

## Conventions for edits

- Keep `git` commits/pushes only when asked. `.env` must never be committed.
- Match the existing style: keyword-only dataclass construction, module-level `import app.config
  as cfg`, IST via `ZoneInfo("Asia/Kolkata")`.
- After changes, run `python -m compileall -q app main.py` (Python) and `node --check` over any
  touched `static/js/*.js` file — no test suite exists.
- Keep the order-matching engine (`app/engine/orders.py`) mechanical. If a requested feature would
  make it decide *which* instrument/direction to trade rather than *whether* a user's own order
  can fill, push back — that reintroduces exactly what this rebuild removed.
- Keep `AppState` free of per-user fields — anything about a specific user's account belongs in
  Postgres, read through `DatabaseService`, not cached on the shared singleton.
