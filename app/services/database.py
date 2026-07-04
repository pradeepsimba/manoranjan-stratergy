from __future__ import annotations

"""
PostgreSQL persistence layer using asyncpg connection pool.
Stores every executed position with full indicator context, scan log,
and daily P&L summary.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

import asyncpg

import app.config as cfg
from app.models import IndicatorResult, Position, TrendGate

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

-- Add indicator columns to existing tables (idempotent)
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS rsi            NUMERIC(6,2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS adx            NUMERIC(6,2);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS candle_pattern VARCHAR(50);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS macd           NUMERIC(12,4);
ALTER TABLE backtest_trades ADD COLUMN IF NOT EXISTS support_level  NUMERIC(12,2);
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

    # ── Positions ─────────────────────────────────────────────────────────────

    async def save_position(self, pos: Position) -> None:
        ind = pos.indicators or IndicatorResult()
        gate = pos.trend or TrendGate()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO positions
                    (symbol, token, entry_price, entry_time, quantity,
                     stop_loss, target, sl_offset, target_offset, order_id,
                     status, rsi, macd_line, adx, plus_di, minus_di,
                     vwap, candle_pattern, daily_green, hourly_green)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                """,
                pos.symbol, pos.token,
                pos.entry_price, pos.entry_time,
                pos.quantity, pos.stop_loss, pos.target,
                pos.sl_offset, pos.target_offset,
                pos.order_id, pos.status.value,
                ind.rsi, ind.macd_line, ind.adx,
                ind.plus_di, ind.minus_di, ind.vwap,
                ind.candle_pattern,
                gate.daily_green, gate.hourly_green,
            )

    async def update_position_exit(self, symbol: str, exit_price: float,
                                   exit_time: str, pnl: float) -> None:
        # Scope to TODAY's row (IST calendar date, matching get_today_positions
        # and the expression index). A rolling NOW()-1day window would also
        # close yesterday's orphaned OPEN row for the same symbol with today's
        # exit price — the UPDATE has no row limit.
        today = datetime.now(_IST).date()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status='CLOSED', exit_price=$1, exit_time=$2, pnl=$3
                WHERE symbol=$4 AND status='OPEN'
                AND (created_at AT TIME ZONE 'Asia/Kolkata')::date = $5
                """,
                exit_price, exit_time, pnl, symbol, today,
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

    # ── Daily stats ───────────────────────────────────────────────────────────

    async def upsert_daily_stats(
        self,
        total_trades: int,
        winning_trades: int,
        total_pnl: float,
        gemini_shortlist: List[str],
    ) -> None:
        # IST calendar date — the trading day, regardless of the host timezone.
        today = datetime.now(_IST).date()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_stats
                    (stat_date, total_trades, winning_trades, total_pnl, gemini_shortlist)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (stat_date) DO UPDATE
                    SET total_trades=$2, winning_trades=$3,
                        total_pnl=$4, gemini_shortlist=$5
                """,
                today, total_trades, winning_trades,
                total_pnl, json.dumps(gemini_shortlist),
            )

    # ── App settings (runtime overrides + internal key-value state) ──────────

    async def get_app_settings(self) -> Dict[str, Any]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM app_settings")
        out: Dict[str, Any] = {}
        for r in rows:
            v = r["value"]
            out[r["key"]] = json.loads(v) if isinstance(v, str) else v
        return out

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
             t.entry_rsi, t.entry_adx, t.entry_pattern,
             t.entry_macd, t.entry_support)
            for t in trades
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO backtest_trades
                    (run_id, symbol, token, entry_time, entry_price, exit_time,
                     exit_price, quantity, stop_loss, target, outcome,
                     gross_pnl, costs, net_pnl, r_multiple,
                     rsi, adx, candle_pattern, macd, support_level)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                        $16,$17,$18,$19,$20)
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
