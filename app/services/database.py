from __future__ import annotations

"""
PostgreSQL persistence layer using asyncpg connection pool.
Stores every executed position with full indicator context, scan log,
and daily P&L summary.
"""

import json
from datetime import date
from typing import Any, Dict, List, Optional

import asyncpg

import app.config as cfg
from app.models import IndicatorResult, Position, PositionStatus, TrendGate

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

CREATE TABLE IF NOT EXISTS scan_log (
    id          SERIAL PRIMARY KEY,
    logged_at   TIMESTAMPTZ DEFAULT NOW(),
    symbol      VARCHAR(20),
    bar_time    VARCHAR(10),
    action      VARCHAR(20),
    reason      TEXT,
    indicators  JSONB
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
"""


class DatabaseService:
    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        self._pool = await asyncpg.create_pool(cfg.POSTGRES_DSN, min_size=2, max_size=10)
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
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status='CLOSED', exit_price=$1, exit_time=$2, pnl=$3
                WHERE symbol=$4 AND status='OPEN'
                """,
                exit_price, exit_time, pnl, symbol,
            )

    async def get_today_positions(self) -> List[Dict[str, Any]]:
        today = date.today().isoformat()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM positions WHERE created_at::date = $1 ORDER BY id",
                today,
            )
        return [dict(r) for r in rows]

    async def get_all_positions(self) -> List[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM positions ORDER BY id DESC LIMIT 500")
        return [dict(r) for r in rows]

    # ── Scan log ──────────────────────────────────────────────────────────────

    async def log_scan(self, symbol: str, bar_time: str,
                       action: str, reason: str,
                       indicators: Optional[Dict] = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO scan_log (symbol, bar_time, action, reason, indicators) "
                "VALUES ($1,$2,$3,$4,$5)",
                symbol, bar_time, action, reason,
                json.dumps(indicators) if indicators else None,
            )

    # ── Daily stats ───────────────────────────────────────────────────────────

    async def upsert_daily_stats(
        self,
        total_trades: int,
        winning_trades: int,
        total_pnl: float,
        gemini_shortlist: List[str],
    ) -> None:
        today = date.today()
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
