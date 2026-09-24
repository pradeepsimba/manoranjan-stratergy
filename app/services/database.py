from __future__ import annotations

"""
PostgreSQL persistence layer using asyncpg connection pool.
Stores every executed position, daily P&L summaries, backtest runs/trades,
and dynamic settings overrides.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

import asyncpg

import app.config as cfg
from app.models import BNTrade, Candle, NFTrade

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20)    NOT NULL,
    token           VARCHAR(20)    NOT NULL,
    entry_price     NUMERIC(10,2),
    entry_time      TEXT,
    quantity        INTEGER,
    stop_loss       NUMERIC(10,2),
    target          NUMERIC(10,2),
    sl_offset       NUMERIC(10,2),
    target_offset   NUMERIC(10,2),
    order_id        VARCHAR(50),
    status          VARCHAR(20)    DEFAULT 'OPEN',
    exit_price      NUMERIC(10,2),
    exit_time       TEXT,
    pnl             NUMERIC(10,2)  DEFAULT 0,
    -- rsi/macd_line/adx/plus_di/minus_di/vwap/candle_pattern/daily_green/
    -- hourly_green: leftover equity-indicator columns from before this app
    -- became the options strategy — save_position() below never writes any
    -- of them (confirmed 2026-09-22: grepping every INSERT/UPDATE against
    -- this table shows none of these 9 names). NOT dropped here — an actual
    -- DROP COLUMN is a real, hard-to-reverse schema change against a live
    -- database this app doesn't control the only copy of; flagging instead
    -- of doing it unprompted. Safe to drop in a real migration once you've
    -- confirmed nothing else reads them.
    rsi             NUMERIC(6,2),
    macd_line       NUMERIC(10,4),
    adx             NUMERIC(6,2),
    plus_di         NUMERIC(6,2),
    minus_di        NUMERIC(6,2),
    vwap            NUMERIC(10,2),
    candle_pattern  VARCHAR(50),
    daily_green     BOOLEAN,
    hourly_green    BOOLEAN,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id               SERIAL PRIMARY KEY,
    stat_date        DATE UNIQUE,
    total_trades     INTEGER       DEFAULT 0,
    winning_trades   INTEGER       DEFAULT 0,
    total_pnl        NUMERIC(10,2) DEFAULT 0,
    max_drawdown     NUMERIC(10,2) DEFAULT 0,
    gemini_shortlist JSONB
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id      VARCHAR(32) PRIMARY KEY,
    from_date   DATE,
    to_date     DATE,
    status      VARCHAR(16)  DEFAULT 'running',   -- running | done | error
    params      JSONB,
    summary     JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id          SERIAL PRIMARY KEY,
    run_id      VARCHAR(32) REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    symbol      VARCHAR(40),
    token       VARCHAR(20),
    entry_time  TEXT,
    entry_price NUMERIC(12,2),
    exit_time   TEXT,
    exit_price  NUMERIC(12,2),
    quantity    INTEGER,
    stop_loss   NUMERIC(12,2),
    target      NUMERIC(12,2),
    outcome     VARCHAR(10),
    gross_pnl   NUMERIC(12,2),
    costs       NUMERIC(12,2),
    net_pnl     NUMERIC(12,2),
    r_multiple  NUMERIC(8,3)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_status ON positions(symbol, status);
CREATE INDEX IF NOT EXISTS idx_positions_created_at_date ON positions(((created_at AT TIME ZONE 'Asia/Kolkata')::date));

-- Legacy equity-indicator columns (inert under the BN options strategy — left
-- in place, nullable, rather than destructively dropped).
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS rsi            NUMERIC(6,2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS adx            NUMERIC(6,2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS candle_pattern VARCHAR(50);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS macd           NUMERIC(12,4);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS support_level  NUMERIC(12,2);

-- Bank Nifty options columns (idempotent) — added to both live positions and
-- backtest_trades so the two share the same option-leg shape.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS direction      VARCHAR(4);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS strike         INTEGER;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS option_type    VARCHAR(2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS expiry         TEXT;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS entry_premium  NUMERIC(10,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS exit_premium   NUMERIC(10,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS iv_used        NUMERIC(6,4);
-- Final exit outcome label (2026-09-24, found in review) — e.g. "TARGET HIT"/
-- "STOP HIT"/"TIME_SCRATCH HIT"/"EOD SQUARE-OFF"/"MANUAL EXIT". NULL for
-- still-OPEN rows. See app/models.py's BNTrade.exit_reason comment for why
-- this was missing: `status` alone (always 'CLOSED' once closed) carries no
-- information about WHY, unlike backtest_trades' existing `outcome` column
-- this brings live positions to parity with.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS outcome        VARCHAR(30);

ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS direction      VARCHAR(4);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS strike         INTEGER;
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS option_type    VARCHAR(2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS expiry         TEXT;
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS entry_premium  NUMERIC(10,2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS exit_premium   NUMERIC(10,2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS iv_used        NUMERIC(6,4);

-- Self-recorded BankNifty 5m history. The market-data server has NO
-- historical archive for the BankNifty index itself (confirmed empirically —
-- every from_date/to_date range returns only the current day, unlike NIFTY 50
-- and individual stocks, which both return full multi-day history). This
-- table is our own growing archive, written once per day at EOD from the
-- live-accumulated candle buffer, so a real multi-day backtest becomes
-- possible over time without depending on the external server for it.
CREATE TABLE IF NOT EXISTS bn_index_bars (
    start_time TEXT PRIMARY KEY,
    open       NUMERIC(10,2),
    high       NUMERIC(10,2),
    low        NUMERIC(10,2),
    close      NUMERIC(10,2),
    volume     NUMERIC(14,2)
);

-- Nifty 50 parallel-engine additions ─────────────────────────────────────────

-- `instrument` disambiguates BankNifty vs Nifty 50 rows in the shared
-- positions/daily_stats tables — added rather than splitting into separate
-- tables, since order_id is already globally unique (BN-/NF- prefixes) and
-- the dashboard just needs one flat "today's trades" log with a column.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS instrument VARCHAR(20) DEFAULT 'BANKNIFTY';

-- Self-recorded Nifty 50 5m history — same rationale/shape as bn_index_bars,
-- populated from day one even though backtest-for-NF isn't wired up yet, so
-- the archive is already accumulating by the time that follow-up happens.
CREATE TABLE IF NOT EXISTS nf_index_bars (
    start_time TEXT PRIMARY KEY,
    open       NUMERIC(10,2),
    high       NUMERIC(10,2),
    low        NUMERIC(10,2),
    close      NUMERIC(10,2),
    volume     NUMERIC(14,2)
);

-- daily_stats gains its own instrument-scoped row so BN and NF get separate
-- daily summaries instead of being blended into one. The original
-- `stat_date UNIQUE` constraint (auto-named daily_stats_stat_date_key) is
-- replaced by a composite (stat_date, instrument) unique index.
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS instrument VARCHAR(20) NOT NULL DEFAULT 'BANKNIFTY';
ALTER TABLE daily_stats DROP CONSTRAINT IF EXISTS daily_stats_stat_date_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_stats_date_instrument ON daily_stats(stat_date, instrument);
"""


class DatabaseService:
    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(cfg.POSTGRES_DSN, min_size=2, max_size=15)
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        print("PostgreSQL connected and schema applied")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ── Positions (the single Bank Nifty options trade) ────────────────────────

    async def save_position(self, trade: Union[BNTrade, NFTrade], instrument: str = "BANKNIFTY") -> None:
        symbol = cfg.BN_INDEX_NAME if instrument == "BANKNIFTY" else cfg.NF_INDEX_NAME
        token  = cfg.BN_INDEX_TOKEN if instrument == "BANKNIFTY" else cfg.NF_INDEX_TOKEN
        # trade.target is an absolute OPTION-PREMIUM level (₹) since the
        # 2026-09-21 scalp rewrite, not an index-points level — must diff
        # against entry_premium (same ₹ scale), not entry_index_price (the
        # BankNifty/Nifty spot, a completely different scale). Using
        # entry_index_price here used to silently write a meaningless
        # ~spot-sized number into every row's target_offset column (found in
        # review, 2026-09-23) — audit/export-only, never read by
        # evaluate_exit or live P&L.
        target_offset = abs(trade.target - trade.entry_premium)
        iv_used = trade.entry_signal.iv_used if trade.entry_signal else None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO positions
                    (symbol, token, entry_price, entry_time, quantity,
                     stop_loss, target, sl_offset, target_offset, order_id,
                     status, direction, strike, option_type, expiry,
                     entry_premium, iv_used, instrument)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                """,
                symbol, token,
                trade.entry_index_price, trade.entry_time, trade.lot_size,
                trade.current_sl, trade.target,
                # trade.stoploss_points (-> sl_offset column) is a vestigial
                # field, always frozen at 0.0 under the 2026-09-21 scalp
                # rewrite — target/stop are now absolute PREMIUM levels
                # (target_rs/stop_rs), not index-point offsets. Every
                # position row since that rewrite has sl_offset=0.00 with no
                # real meaning; kept only for schema/column stability, not
                # dropped (see this table's other "legacy column" notes
                # above) — flagged here (found in review, 2026-09-23) so
                # nobody reads a real signal into this column later.
                trade.stoploss_points, target_offset,
                trade.order_id, trade.status.value, trade.direction,
                trade.strike, trade.option_type, trade.expiry,
                trade.entry_premium, iv_used, instrument,
            )

    async def update_position_exit(self, order_id: str, exit_price: float,
                                   exit_time: str, pnl: float,
                                   exit_premium: Optional[float] = None,
                                   outcome: Optional[str] = None) -> None:
        # Wrapped in an explicit transaction (found in review, 2026-09-23,
        # alongside the matched>1 guard below): raising INSIDE conn.
        # transaction() makes asyncpg roll back automatically before the
        # exception propagates — so an order_id collision that matches 2+
        # rows gets its erroneous double-close UNDONE, not just detected
        # after the fact. Without this, the UPDATE below would already be
        # committed by the time matched>1 is noticed, and there'd be no way
        # to undo closing an unrelated historical row.
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                tag = await conn.execute(
                    """
                    UPDATE positions
                    SET status='CLOSED', exit_price=$1, exit_time=$2, pnl=$3, exit_premium=$4, outcome=$5
                    WHERE order_id=$6 AND status='OPEN'
                    """,
                    exit_price, exit_time, pnl, exit_premium, outcome, order_id,
                )
                self._check_update_position_exit_tag(tag, order_id)

    @staticmethod
    def _check_update_position_exit_tag(tag: str, order_id: str) -> None:
        # asyncpg returns a command tag like "UPDATE 1"/"UPDATE 0" — a 0 here
        # (found in review, 2026-09-23) used to be indistinguishable from
        # success: the WHERE clause matches nothing if this order_id's entry
        # row was never saved (a prior save_position failure) or was already
        # closed, silently leaving the exit unrecorded with no error raised
        # anywhere. Raising surfaces it to the caller's own error handling
        # (scheduler.py's _persist_closed_exit retries + logs CRITICAL on
        # exhaustion; the manual-exit endpoint returns a 500) instead of a
        # completely invisible gap in the trade audit log.
        #
        # matched > 1 is a second, related guard (also found in review,
        # 2026-09-23): `positions.order_id` has NO unique constraint in the
        # schema above, and the app-level generator (HHMMSS + an in-process
        # counter that resets to 1 on every restart, no date component)
        # doesn't actually guarantee uniqueness across days — a prior day's
        # trade left stuck status='OPEN' (e.g. by this exact retry-exhaustion
        # path) could in principle share an order_id with a new trade. If it
        # ever does, this UPDATE's WHERE clause would silently match and
        # close BOTH rows in one statement, corrupting an unrelated
        # historical row's exit_price/pnl instead of raising. A real fix
        # needs a DB-level uniqueness constraint (a schema migration, out of
        # scope for this pass) — this is a narrower, pure-application-code
        # safety net that at least turns silent corruption into a visible
        # error the caller's retry/CRITICAL-log path already handles.
        try:
            matched = int(tag.split()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(
                f"update_position_exit: could not parse asyncpg command tag {tag!r} "
                f"for order_id={order_id!r} — treating as failed rather than silently succeeding"
            ) from None
        if matched == 0:
            raise RuntimeError(
                f"update_position_exit: no OPEN position row matched order_id={order_id!r} "
                f"— entry row missing or already closed"
            )
        if matched > 1:
            raise RuntimeError(
                f"update_position_exit: order_id={order_id!r} matched {matched} OPEN rows "
                f"(expected exactly 1) — order_id collision; rolling back this exit rather "
                f"than closing an unrelated row"
            )

    async def get_today_positions(self) -> List[Dict[str, Any]]:
        today = datetime.now(_IST).date()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM positions "
                "WHERE (created_at AT TIME ZONE 'Asia/Kolkata')::date = $1 ORDER BY id",
                today,
            )
        return [dict(r) for r in rows]

    async def get_all_positions(self) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM positions ORDER BY id DESC LIMIT 500")
        return [dict(r) for r in rows]

    # ── Self-recorded BankNifty index history ──────────────────────────────────

    async def save_bn_index_bars(self, candles: List[Candle]) -> None:
        """
        Upsert today's (or any) BankNifty bars into our own growing archive.
        Idempotent — safe to call every EOD with the whole in-memory buffer
        (up to MAX_CANDLE_BUFFER bars); already-stored bars just no-op update.
        """
        if not candles:
            return
        rows = [(c.start_time, c.open, c.high, c.low, c.close, c.volume) for c in candles]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO bn_index_bars (start_time, open, high, low, close, volume)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (start_time) DO UPDATE
                    SET open=$2, high=$3, low=$4, close=$5, volume=$6
                """,
                rows,
            )

    async def get_bn_index_bars(self, from_iso: str, to_iso: str) -> List[Candle]:
        """Our self-recorded BankNifty bars in [from_iso, to_iso), chronological."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM bn_index_bars WHERE start_time >= $1 AND start_time < $2 "
                "ORDER BY start_time",
                from_iso, to_iso,
            )
        return [
            Candle(
                start_time=r["start_time"],
                open=float(r["open"] or 0), high=float(r["high"] or 0),
                low=float(r["low"] or 0), close=float(r["close"] or 0),
                volume=float(r["volume"] or 0),
            )
            for r in rows
        ]

    # ── Self-recorded Nifty 50 index history (mirrors bn_index_bars above) ────

    async def save_nf_index_bars(self, candles: List[Candle]) -> None:
        if not candles:
            return
        rows = [(c.start_time, c.open, c.high, c.low, c.close, c.volume) for c in candles]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO nf_index_bars (start_time, open, high, low, close, volume)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (start_time) DO UPDATE
                    SET open=$2, high=$3, low=$4, close=$5, volume=$6
                """,
                rows,
            )

    async def get_nf_index_bars(self, from_iso: str, to_iso: str) -> List[Candle]:
        """Our self-recorded Nifty 50 bars in [from_iso, to_iso), chronological."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM nf_index_bars WHERE start_time >= $1 AND start_time < $2 "
                "ORDER BY start_time",
                from_iso, to_iso,
            )
        return [
            Candle(
                start_time=r["start_time"],
                open=float(r["open"] or 0), high=float(r["high"] or 0),
                low=float(r["low"] or 0), close=float(r["close"] or 0),
                volume=float(r["volume"] or 0),
            )
            for r in rows
        ]

    # ── Daily stats ───────────────────────────────────────────────────────────

    async def upsert_daily_stats(
        self,
        total_trades: int,
        winning_trades: int,
        total_pnl: float,
        gemini_shortlist: Optional[List[str]],
        max_drawdown: float = 0.0,
        instrument: str = "BANKNIFTY",
    ) -> None:
        # IST calendar date — the trading day, regardless of the host timezone.
        # gemini_shortlist=None → keep whatever is already stored (COALESCE): a
        # restart-after-close restores trades but NOT the shortlist, and must not
        # clobber the real one written earlier in the day.
        today = datetime.now(_IST).date()
        shortlist = json.dumps(gemini_shortlist) if gemini_shortlist is not None else None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_stats
                    (stat_date, total_trades, winning_trades, total_pnl,
                     gemini_shortlist, max_drawdown, instrument)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (stat_date, instrument) DO UPDATE
                    SET total_trades=$2, winning_trades=$3, total_pnl=$4,
                        gemini_shortlist=COALESCE($5, daily_stats.gemini_shortlist),
                        max_drawdown=$6
                """,
                today, total_trades, winning_trades, total_pnl,
                shortlist, max_drawdown, instrument,
            )

    # ── App settings (runtime overrides + internal key-value state) ──────────

    async def get_app_settings(self) -> Dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM app_settings")
        # Routed through the shared _decode_jsonb helper (found in review:
        # this used to inline the identical json.loads-if-str logic instead
        # of using it, diverging from the repo's stated "the" jsonb-decode
        # convention) — wrapped as a single-key dict since _decode_jsonb
        # operates on named columns within one row, not a key/value table.
        return {r["key"]: self._decode_jsonb({"value": r["value"]}, "value")["value"]
               for r in rows}

    async def set_app_settings(self, changes: Dict[str, Any]) -> None:
        if not changes:
            return
        rows = [(k, json.dumps(v)) for k, v in changes.items()]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ($1, $2::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_at=NOW()
                """,
                rows,
            )

    async def delete_app_settings(self, keys: List[str]) -> None:
        if not keys:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM app_settings WHERE key = ANY($1::text[])", list(keys)
            )

    async def replace_app_settings(self, store: Dict[str, Any],
                                   delete_keys: List[str]) -> None:
        """
        Upsert + delete in ONE transaction — a settings save must be all-or-
        nothing, or a failure between the two writes leaves the DB persisting
        values that were never applied live (and a restart would silently
        change trading behavior).
        """
        if not store and not delete_keys:
            return
        rows = [(k, json.dumps(v)) for k, v in store.items()]
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if rows:
                    await conn.executemany(
                        """
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES ($1, $2::jsonb, NOW())
                        ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_at=NOW()
                        """,
                        rows,
                    )
                if delete_keys:
                    await conn.execute(
                        "DELETE FROM app_settings WHERE key = ANY($1::text[])",
                        list(delete_keys),
                    )

    # ── Backtest ──────────────────────────────────────────────────────────────

    async def create_backtest_run(self, run_id: str, from_date, to_date,
                                  params: Dict[str, Any]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO backtest_runs (run_id, from_date, to_date, status, params) "
                "VALUES ($1,$2,$3,'running',$4::jsonb)",
                run_id, from_date, to_date, json.dumps(params),
            )

    async def finish_backtest_run(self, run_id: str, summary: Dict[str, Any]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_runs SET status='done', summary=$2::jsonb, "
                "finished_at=NOW() WHERE run_id=$1",
                run_id, json.dumps(summary),
            )

    async def fail_backtest_run(self, run_id: str, error: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE backtest_runs SET status='error', error=$2, finished_at=NOW() "
                "WHERE run_id=$1",
                run_id, error,
            )

    async def save_backtest_trades(self, run_id: str, trades: list) -> None:
        if not trades:
            return
        rows = [
            (run_id, t.symbol, t.token, t.entry_time, t.entry_price,
             t.exit_time, t.exit_price, t.qty, t.stop_loss, t.target,
             t.outcome, t.gross_pnl, t.costs, t.net_pnl, t.r_multiple,
             t.direction, t.strike, t.option_type, t.expiry,
             t.entry_premium, t.exit_premium, t.iv_used)
            for t in trades
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO backtest_trades
                    (run_id, symbol, token, entry_time, entry_price, exit_time,
                     exit_price, quantity, stop_loss, target, outcome,
                     gross_pnl, costs, net_pnl, r_multiple,
                     direction, strike, option_type, expiry,
                     entry_premium, exit_premium, iv_used)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                        $16,$17,$18,$19,$20,$21,$22)
                """,
                rows,
            )

    @staticmethod
    def _decode_jsonb(d: Dict[str, Any], *keys: str) -> Dict[str, Any]:
        # asyncpg returns jsonb columns as raw strings unless a codec is set;
        # decode them so the API returns real objects, not JSON-in-a-string.
        for k in keys:
            v = d.get(k)
            if isinstance(v, str):
                d[k] = json.loads(v)
        return d

    async def get_backtest_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM backtest_runs WHERE run_id=$1", run_id)
        return self._decode_jsonb(dict(row), "params", "summary") if row else None

    async def delete_backtest_run(self, run_id: str) -> None:
        async with self._pool.acquire() as conn:
            # backtest_trades has ON DELETE CASCADE — trades deleted automatically
            await conn.execute("DELETE FROM backtest_runs WHERE run_id=$1", run_id)

    async def get_backtest_trades(self, run_id: str) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM backtest_trades WHERE run_id=$1 ORDER BY id", run_id
            )
        return [dict(r) for r in rows]

    async def list_backtest_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT run_id, from_date, to_date, status, summary, created_at "
                "FROM backtest_runs ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [self._decode_jsonb(dict(r), "summary") for r in rows]
