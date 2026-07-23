from __future__ import annotations

"""
PostgreSQL persistence layer using an asyncpg connection pool.

Multi-user equity paper trading: every user has their own funds balance,
holdings (CNC/delivery, persist indefinitely), positions (MIS/intraday,
squared off at end of day), and order book. There is no process-wide
in-memory cache of this data (unlike the shared market-data candle store in
app/state.py) — Postgres is the source of truth, read per request.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import asyncpg

import app.config as cfg

_IST = ZoneInfo("Asia/Kolkata")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(32) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    funds         NUMERIC(14,2) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS instruments (
    token         VARCHAR(20) PRIMARY KEY,
    name          TEXT NOT NULL,           -- canonical server-side stockname
    display_name  TEXT NOT NULL,
    tradable      BOOLEAN NOT NULL DEFAULT TRUE,
    verified_at   TIMESTAMPTZ DEFAULT NOW()
);
-- 'EQUITY' | 'INDEX' — sourced from clientstatus's own `type` field (see
-- instrument_discovery.py). An INDEX has no delivery mechanism, so CNC is
-- rejected for it at order-placement time (app/engine/orders.py).
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS asset_type VARCHAR(10) NOT NULL DEFAULT 'EQUITY';
-- The market-data server's alphanumeric symbol code (clientstatus field 4,
-- e.g. 'KOTAKBANK' for token '1922'). The historical + WS endpoints match a
-- request by (stockname, symbol_code) — NOT by the numeric token — so this is
-- the identifier sent on every external call, while `token` stays the internal
-- primary key everything else (candles/ltp/positions) is keyed by. Nullable so
-- legacy/seed rows without a known code fall back to `token` (see market_data
-- / historical_data). NOTE the length: index codes are the full name string
-- ('NIFTY 50'), so this is wider than a bare ticker.
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS symbol_code VARCHAR(40);

CREATE TABLE IF NOT EXISTS orders (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    token         VARCHAR(20) NOT NULL,
    symbol        TEXT NOT NULL,
    side          VARCHAR(4)  NOT NULL,    -- BUY | SELL
    order_type    VARCHAR(6)  NOT NULL,    -- MARKET | LIMIT
    product       VARCHAR(4)  NOT NULL,    -- CNC | MIS
    qty           INTEGER NOT NULL,
    limit_price   NUMERIC(12,2),
    status        VARCHAR(10) NOT NULL DEFAULT 'PENDING',
    filled_price  NUMERIC(12,2),
    filled_at     TIMESTAMPTZ,
    reject_reason TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_pending_token ON orders(token) WHERE status = 'PENDING';
-- Exact amount this fill moved through the user's funds (signed: credit positive,
-- debit negative) — captured at fill time by the order engine, since for a
-- leveraged MIS fill it's the margin/refund+P&L amount, NOT qty*filled_price
-- (only true for CNC). NULL for pending/rejected/cancelled orders and for rows
-- filled before this column existed (Console/Journal cash-flow reporting falls
-- back to the qty*filled_price estimate for those legacy NULL rows).
ALTER TABLE orders ADD COLUMN IF NOT EXISTS funds_delta NUMERIC(12,2);

CREATE TABLE IF NOT EXISTS holdings (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    token      VARCHAR(20) NOT NULL,
    symbol     TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    avg_price  NUMERIC(12,2) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, token)
);
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS target_price NUMERIC(12,2);
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC(12,2);
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS target_qty INTEGER;
ALTER TABLE holdings ADD COLUMN IF NOT EXISTS stop_loss_qty INTEGER;
CREATE INDEX IF NOT EXISTS idx_holdings_token_trigger ON holdings(token)
    WHERE qty > 0 AND (target_price IS NOT NULL OR stop_loss_price IS NOT NULL);

CREATE TABLE IF NOT EXISTS positions (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    token         VARCHAR(20) NOT NULL,
    symbol        TEXT NOT NULL,
    side          VARCHAR(4) NOT NULL,     -- BUY (long) | SELL (short)
    qty           INTEGER NOT NULL,
    avg_price     NUMERIC(12,2) NOT NULL,
    status        VARCHAR(10) NOT NULL DEFAULT 'OPEN',
    exit_price    NUMERIC(12,2),
    realized_pnl  NUMERIC(12,2),
    opened_at     TIMESTAMPTZ DEFAULT NOW(),
    closed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_positions_user_status ON positions(user_id, status);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS target_price NUMERIC(12,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC(12,2);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS target_qty INTEGER;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS stop_loss_qty INTEGER;
-- Timestamp of the most recent exit event (partial reduce OR full close) — unlike
-- closed_at (only set on a full close), this is updated on every exit so "realized
-- today" reporting can tell a still-OPEN, partially-reduced position's booked P&L
-- apart from an older day's.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_exit_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_positions_token_trigger ON positions(token)
    WHERE status = 'OPEN' AND (target_price IS NOT NULL OR stop_loss_price IS NOT NULL);

-- Margin currently blocked against this position (MIS is leveraged; CNC never
-- shorts so holdings have no margin concept). Positions opened before this
-- column existed used the old cash-only rule, which moved funds DIFFERENTLY
-- by side: a BUY-to-open DEBITED qty*avg_price (so margin_used must be set to
-- +qty*avg_price, refunded in full on close); a SELL-to-open (a short) instead
-- CREDITED qty*avg_price up front, so margin_used must backfill to the
-- NEGATIVE of that amount — a close's "margin_used + pnl" refund formula then
-- nets out to just the ordinary exit-time cash flow instead of re-crediting
-- an amount the user was already paid once at entry.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS margin_used NUMERIC(12,2) NOT NULL DEFAULT 0;
UPDATE positions SET margin_used = CASE WHEN side = 'BUY' THEN qty * avg_price ELSE -(qty * avg_price) END
    WHERE status = 'OPEN' AND margin_used = 0;

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
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

    @property
    def pool(self) -> asyncpg.Pool:
        assert self._pool is not None, "DatabaseService.init() not called yet"
        return self._pool

    # ── Users ─────────────────────────────────────────────────────────────────

    async def create_user(self, username: str, password_hash: str, password_salt: str,
                          funds: float) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (username, password_hash, password_salt, funds)
                VALUES ($1, $2, $3, $4)
                RETURNING id, username, funds, created_at
                """,
                username, password_hash, password_salt, funds,
            )
        return dict(row)

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE username=$1", username)
        return dict(row) if row else None

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
        return dict(row) if row else None

    async def update_funds(self, user_id: int, delta: float) -> float:
        """Atomically adjust a user's funds by `delta` (may be negative); returns the new balance."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE users SET funds = funds + $2 WHERE id=$1 RETURNING funds",
                user_id, delta,
            )
        return float(row["funds"])

    async def set_funds(self, user_id: int, amount: float) -> float:
        """Set a user's funds to an absolute value (e.g. applying a new Starting Funds
        setting to the account that changed it) — unlike update_funds, not a delta."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE users SET funds = $2 WHERE id=$1 RETURNING funds",
                user_id, amount,
            )
        return float(row["funds"])

    # ── Instruments ───────────────────────────────────────────────────────────

    async def upsert_instruments(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        values = [(r["token"], r["name"], r["display_name"], r.get("tradable", True),
                  r.get("asset_type", "EQUITY"), r.get("symbol_code")) for r in rows]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO instruments (token, name, display_name, tradable, asset_type, symbol_code, verified_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (token) DO UPDATE
                    SET name=$2, display_name=$3, tradable=$4, asset_type=$5, symbol_code=$6, verified_at=NOW()
                """,
                values,
            )

    async def count_instruments(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM instruments WHERE tradable")

    async def get_tradable_instruments(self) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM instruments WHERE tradable ORDER BY display_name"
            )
        return [dict(r) for r in rows]

    async def get_instrument(self, token: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM instruments WHERE token=$1", token)
        return dict(row) if row else None

    # ── Orders ────────────────────────────────────────────────────────────────

    async def create_order(self, user_id: int, token: str, symbol: str, side: str,
                           order_type: str, product: str, qty: int,
                           limit_price: Optional[float]) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO orders (user_id, token, symbol, side, order_type, product,
                                    qty, limit_price, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PENDING')
                RETURNING *
                """,
                user_id, token, symbol, side, order_type, product, qty, limit_price,
            )
        return dict(row)

    async def fill_order(self, order_id: int, filled_price: float, funds_delta: float) -> Dict[str, Any]:
        """`funds_delta` is the exact signed amount this fill just moved through the
        user's funds (see execute_fill/_apply_mis_fill/square_off_position/
        exit_holding/eod_square_off_all_mis, each of which already computed it via
        update_funds) — persisted so Console/Journal reporting can read the true
        figure back instead of re-deriving qty*price, which is wrong for MIS."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE orders SET status='COMPLETE', filled_price=$2, filled_at=NOW(), funds_delta=$3
                WHERE id=$1 RETURNING *
                """,
                order_id, filled_price, funds_delta,
            )
        return dict(row)

    async def reject_order(self, order_id: int, reason: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status='REJECTED', reject_reason=$2 WHERE id=$1",
                order_id, reason,
            )

    async def cancel_order(self, order_id: int, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE orders SET status='CANCELLED' "
                "WHERE id=$1 AND user_id=$2 AND status='PENDING'",
                order_id, user_id,
            )
        return result.endswith("1")

    async def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        return dict(row) if row else None

    async def get_pending_orders_for_token(self, token: str) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE token=$1 AND status='PENDING' ORDER BY created_at",
                token,
            )
        return [dict(r) for r in rows]

    async def get_user_orders(self, user_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE user_id=$1 ORDER BY created_at DESC LIMIT $2",
                user_id, limit,
            )
        return [dict(r) for r in rows]

    # ── Reporting reads (Console / Journal) ────────────────────────────────────
    # Read-only, derived views over the same orders/positions tables the trading
    # engine writes — no separate ledger table, because every fill (incl. manual
    # exits and EOD square-offs) is already persisted as a COMPLETE order, so the
    # order book alone is a full record of cash movement and trade activity.

    async def get_user_journal(self, user_id: int, limit: int = 500) -> List[Dict[str, Any]]:
        """Every order event newest-first, ordered by EFFECTIVE time (fill time
        for fills, else placement time) so a resting LIMIT that fills later sits
        at its fill moment — the correct anchor for a running-balance ledger."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE user_id=$1 "
                "ORDER BY COALESCE(filled_at, created_at) DESC, id DESC LIMIT $2",
                user_id, limit,
            )
        return [dict(r) for r in rows]

    async def get_completed_orders(self, user_id: int, limit: int = 1000) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE user_id=$1 AND status='COMPLETE' "
                "ORDER BY filled_at DESC LIMIT $2",
                user_id, limit,
            )
        return [dict(r) for r in rows]

    async def get_trade_stats(self, user_id: int) -> Dict[str, Any]:
        """Whole-history order aggregates (not window-limited) for Console cards."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status='COMPLETE')                                    AS fills,
                  COALESCE(SUM(qty*filled_price) FILTER (WHERE status='COMPLETE'), 0)          AS turnover,
                  COALESCE(SUM(qty*filled_price) FILTER (WHERE status='COMPLETE'
                                                         AND side='BUY'), 0)                   AS buy_value,
                  COALESCE(SUM(qty*filled_price) FILTER (WHERE status='COMPLETE'
                                                         AND side='SELL'), 0)                  AS sell_value,
                  COUNT(*)                                                                     AS total_orders,
                  COUNT(*) FILTER (WHERE status='PENDING')                                     AS pending,
                  COUNT(*) FILTER (WHERE status='REJECTED')                                    AS rejected,
                  COUNT(*) FILTER (WHERE status='CANCELLED')                                   AS cancelled
                FROM orders WHERE user_id=$1
                """,
                user_id,
            )
        return dict(row)

    async def get_realized_pnl_total(self, user_id: int) -> Dict[str, Any]:
        """Sums realized_pnl across ALL positions, not just CLOSED ones — a partial
        exit (reduce_position) books realized_pnl onto a position that stays OPEN
        (qty remaining), and that booked P&L must count immediately, not only once
        the position is eventually fully closed out."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  COALESCE(SUM(realized_pnl), 0)                    AS realized,
                  COUNT(*) FILTER (WHERE realized_pnl IS NOT NULL)  AS closed,
                  COUNT(*) FILTER (WHERE realized_pnl > 0)          AS wins,
                  COUNT(*) FILTER (WHERE realized_pnl < 0)          AS losses
                FROM positions WHERE user_id=$1
                """,
                user_id,
            )
        return dict(row)

    async def get_realized_pnl_by_symbol(self, user_id: int) -> List[Dict[str, Any]]:
        """Same partial-exit fix as get_realized_pnl_total — a position with booked
        realized_pnl counts here even while still OPEN (qty partially reduced)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT symbol,
                       token,
                       COUNT(*)                       AS trades,
                       COALESCE(SUM(realized_pnl), 0) AS realized_pnl
                FROM positions
                WHERE user_id=$1 AND realized_pnl IS NOT NULL
                GROUP BY symbol, token
                ORDER BY realized_pnl DESC
                """,
                user_id,
            )
        return [dict(r) for r in rows]

    # ── Holdings (CNC / delivery) ────────────────────────────────────────────

    async def get_holding(self, user_id: int, token: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM holdings WHERE user_id=$1 AND token=$2", user_id, token
            )
        return dict(row) if row else None

    async def get_user_holdings(self, user_id: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM holdings WHERE user_id=$1 AND qty > 0 ORDER BY symbol", user_id
            )
        return [dict(r) for r in rows]

    async def upsert_holding_buy(self, user_id: int, token: str, symbol: str,
                                 qty: int, price: float) -> Dict[str, Any]:
        """Add `qty` shares at `price` to a holding, recomputing the weighted average cost."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO holdings (user_id, token, symbol, qty, avg_price, updated_at)
                VALUES ($1,$2,$3,$4,$5,NOW())
                ON CONFLICT (user_id, token) DO UPDATE
                    SET qty = holdings.qty + EXCLUDED.qty,
                        avg_price = ((holdings.avg_price * holdings.qty)
                                     + (EXCLUDED.avg_price * EXCLUDED.qty))
                                    / (holdings.qty + EXCLUDED.qty),
                        symbol = EXCLUDED.symbol,
                        updated_at = NOW()
                RETURNING *
                """,
                user_id, token, symbol, qty, price,
            )
        return dict(row)

    async def reduce_holding_sell(self, user_id: int, token: str, qty: int) -> Dict[str, Any]:
        """Reduce a holding's qty by `qty` (caller has already validated sufficient qty exists).
        If this fully liquidates the holding (qty -> 0), also clears target_price/
        stop_loss_price (and their qtys) — holdings rows are reused via ON CONFLICT upsert
        on a later BUY (unlike positions, which always INSERT a fresh row), so a stale
        trigger left in place would silently reactivate against a future, unrelated cost basis."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE holdings
                SET qty = qty - $3,
                    target_price    = CASE WHEN qty - $3 <= 0 THEN NULL ELSE target_price END,
                    target_qty      = CASE WHEN qty - $3 <= 0 THEN NULL ELSE target_qty END,
                    stop_loss_price = CASE WHEN qty - $3 <= 0 THEN NULL ELSE stop_loss_price END,
                    stop_loss_qty   = CASE WHEN qty - $3 <= 0 THEN NULL ELSE stop_loss_qty END,
                    updated_at = NOW()
                WHERE user_id=$1 AND token=$2 RETURNING *
                """,
                user_id, token, qty,
            )
        return dict(row)

    # ── Target / Stop-Loss triggers ──────────────────────────────────────────
    # target_qty/stop_loss_qty are each independently optional: NULL means "exit
    # the full open qty when that trigger fires" (backward-compatible default);
    # a number means "exit only this many, leave the rest open".

    async def set_position_triggers(self, position_id: int, user_id: int,
                                     target_price: Optional[float],
                                     stop_loss_price: Optional[float],
                                     target_qty: Optional[int] = None,
                                     stop_loss_qty: Optional[int] = None) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE positions SET target_price=$3, stop_loss_price=$4,
                                     target_qty=$5, stop_loss_qty=$6
                WHERE id=$1 AND user_id=$2 AND status='OPEN' RETURNING *
                """,
                position_id, user_id, target_price, stop_loss_price, target_qty, stop_loss_qty,
            )
        return dict(row) if row else None

    async def set_holding_triggers(self, user_id: int, token: str,
                                   target_price: Optional[float],
                                   stop_loss_price: Optional[float],
                                   target_qty: Optional[int] = None,
                                   stop_loss_qty: Optional[int] = None) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE holdings SET target_price=$3, stop_loss_price=$4,
                                    target_qty=$5, stop_loss_qty=$6, updated_at=NOW()
                WHERE user_id=$1 AND token=$2 AND qty > 0 RETURNING *
                """,
                user_id, token, target_price, stop_loss_price, target_qty, stop_loss_qty,
            )
        return dict(row) if row else None

    async def clear_position_trigger(self, position_id: int, which: str) -> None:
        """Clear only the ONE side ('target' or 'stop_loss') that just fired, leaving
        the other side (if set) active for the remaining open qty."""
        col_price, col_qty = (("target_price", "target_qty") if which == "target"
                              else ("stop_loss_price", "stop_loss_qty"))
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE positions SET {col_price}=NULL, {col_qty}=NULL WHERE id=$1",
                position_id,
            )

    async def clear_holding_trigger(self, user_id: int, token: str, which: str) -> None:
        col_price, col_qty = (("target_price", "target_qty") if which == "target"
                              else ("stop_loss_price", "stop_loss_qty"))
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE holdings SET {col_price}=NULL, {col_qty}=NULL WHERE user_id=$1 AND token=$2",
                user_id, token,
            )

    async def get_open_positions_with_triggers(self, token: str) -> List[Dict[str, Any]]:
        """OPEN positions (any user) on `token` with an active target/SL — called once per
        dirty token per tick, so this must stay cheap (see idx_positions_token_trigger)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM positions WHERE token=$1 AND status='OPEN'
                  AND (target_price IS NOT NULL OR stop_loss_price IS NOT NULL)
                """,
                token,
            )
        return [dict(r) for r in rows]

    async def get_holdings_with_triggers(self, token: str) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM holdings WHERE token=$1 AND qty > 0
                  AND (target_price IS NOT NULL OR stop_loss_price IS NOT NULL)
                """,
                token,
            )
        return [dict(r) for r in rows]

    # ── Positions (MIS / intraday) ───────────────────────────────────────────

    async def get_open_position(self, user_id: int, token: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM positions WHERE user_id=$1 AND token=$2 AND status='OPEN'",
                user_id, token,
            )
        return dict(row) if row else None

    async def get_user_positions(self, user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE user_id=$1 AND status=$2 ORDER BY opened_at DESC",
                    user_id, status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM positions WHERE user_id=$1 ORDER BY opened_at DESC", user_id
                )
        return [dict(r) for r in rows]

    async def get_position(self, position_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM positions WHERE id=$1", position_id)
        return dict(row) if row else None

    async def get_all_open_positions(self) -> List[Dict[str, Any]]:
        """All users' open MIS positions — used by the EOD square-off sweep."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM positions WHERE status='OPEN'")
        return [dict(r) for r in rows]

    async def open_position(self, user_id: int, token: str, symbol: str, side: str,
                            qty: int, price: float, margin_used: float = 0) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO positions (user_id, token, symbol, side, qty, avg_price, status, margin_used)
                VALUES ($1,$2,$3,$4,$5,$6,'OPEN',$7)
                RETURNING *
                """,
                user_id, token, symbol, side, qty, price, margin_used,
            )
        return dict(row)

    async def add_to_position(self, position_id: int, qty: int, price: float,
                              margin_delta: float = 0) -> Dict[str, Any]:
        """Add `qty` more to an existing OPEN position, recomputing the weighted average
        price and adding the fresh margin blocked for the added qty.

        The explicit ::int/::numeric casts on $2 are load-bearing, not decoration:
        $2 is reused both in a pure-integer context (qty + $2, twice) and inside a
        numeric multiplication ($3 * $2) — without a cast pinning its type up front,
        asyncpg/Postgres's parameter-type inference across those mixed occurrences
        silently produces a WRONG avg_price (empirically verified: 127.67 instead of
        the correct 128.04 for a real add-to-an-existing-position case), even though
        qty and margin_used come out correct. Do not remove these casts."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET qty = qty + $2::int,
                    avg_price = ((avg_price * qty) + ($3::numeric * $2::int)) / (qty + $2::int),
                    margin_used = margin_used + $4::numeric
                WHERE id=$1 RETURNING *
                """,
                position_id, qty, price, margin_delta,
            )
        return dict(row)

    async def reduce_position(self, position_id: int, qty: int, realized_pnl_delta: float,
                              margin_refund: float = 0) -> Dict[str, Any]:
        """Partially close a position by `qty`, accumulating realized P&L and releasing
        the proportional share of margin that was blocking it."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET qty = qty - $2,
                    realized_pnl = COALESCE(realized_pnl, 0) + $3,
                    margin_used = margin_used - $4,
                    last_exit_at = NOW()
                WHERE id=$1 RETURNING *
                """,
                position_id, qty, realized_pnl_delta, margin_refund,
            )
        return dict(row)

    async def close_position(self, position_id: int, exit_price: float,
                             realized_pnl_delta: float) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET status='CLOSED', qty=0, exit_price=$2,
                    realized_pnl = COALESCE(realized_pnl, 0) + $3,
                    margin_used=0, closed_at=NOW(), last_exit_at=NOW()
                WHERE id=$1 RETURNING *
                """,
                position_id, exit_price, realized_pnl_delta,
            )
        return dict(row)

    # ── App settings (runtime overrides — generic key/value store) ──────────

    async def get_app_settings(self) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
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
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM app_settings WHERE key = ANY($1::text[])", list(keys)
            )

    async def replace_app_settings(self, store: Dict[str, Any],
                                   delete_keys: List[str]) -> None:
        """
        Upsert + delete in ONE transaction — a settings save must be all-or-
        nothing, or a failure between the two writes leaves the DB persisting
        values that were never applied live (and a restart would silently
        change behavior).
        """
        if not store and not delete_keys:
            return
        rows = [(k, json.dumps(v)) for k, v in store.items()]
        async with self.pool.acquire() as conn:
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

    @staticmethod
    def _decode_jsonb(d: Dict[str, Any], *keys: str) -> Dict[str, Any]:
        # asyncpg returns jsonb columns as raw strings unless a codec is set;
        # decode them so the API returns real objects, not JSON-in-a-string.
        for k in keys:
            v = d.get(k)
            if isinstance(v, str):
                d[k] = json.loads(v)
        return d
