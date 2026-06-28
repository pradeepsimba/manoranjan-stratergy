from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Dict, List, Optional

import aiosqlite

from app.models import Trade

DB_PATH = "trading.db"


class DatabaseService:
    def __init__(self) -> None:
        self._db:             Optional[aiosqlite.Connection] = None
        self._stock_queue:    asyncio.Queue                  = asyncio.Queue(maxsize=200_000)
        self._trade_cache:    List[Trade]                    = []
        self._cache_date:     Optional[str]                  = None
        self._writer_task:    Optional[asyncio.Task]         = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA cache_size=10000")
        await self._db.execute("PRAGMA temp_store=MEMORY")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                stockname TEXT, time TEXT, ltp REAL, qty REAL
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stocks_time      ON stocks(time)")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stocks_name_time ON stocks(stockname,time)")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                type          TEXT, price REAL, time TEXT,
                confidence    TEXT, pnl   REAL, optionPremium REAL
            )
        """)
        await self._db.commit()
        await self._refresh_trade_cache()
        self._writer_task = asyncio.create_task(self._batch_stock_writer())

    async def close(self) -> None:
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        await self._flush_stock_queue()
        if self._db:
            await self._db.close()

    # ── Stock records ─────────────────────────────────────────────────────────

    def add_stock_record(self, stockname: str, time: str, ltp: float, qty: float) -> None:
        """Non-blocking: called from the tick handler; actual INSERT is batched async."""
        try:
            self._stock_queue.put_nowait((stockname, time, ltp, qty))
        except asyncio.QueueFull:
            pass

    async def _batch_stock_writer(self) -> None:
        while True:
            try:
                item  = await asyncio.wait_for(self._stock_queue.get(), timeout=0.2)
                batch = [item]
                while not self._stock_queue.empty() and len(batch) < 200:
                    batch.append(self._stock_queue.get_nowait())
                await self._db.executemany(
                    "INSERT INTO stocks(stockname,time,ltp,qty) VALUES(?,?,?,?)", batch)
                await self._db.commit()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Batch write error: {e}")

    async def _flush_stock_queue(self) -> None:
        batch: list = []
        while not self._stock_queue.empty():
            try:
                batch.append(self._stock_queue.get_nowait())
            except Exception:
                break
        if batch and self._db:
            await self._db.executemany(
                "INSERT INTO stocks(stockname,time,ltp,qty) VALUES(?,?,?,?)", batch)
            await self._db.commit()

    async def prune_old_stock_data(self) -> None:
        today = date.today().isoformat()
        cur   = await self._db.execute("DELETE FROM stocks WHERE time < ?", (today,))
        await self._db.commit()
        if cur.rowcount:
            print(f"Pruned {cur.rowcount} old stock records")

    # ── Trades ────────────────────────────────────────────────────────────────

    async def save_trade(self, t: Trade) -> None:
        await self._db.execute(
            "INSERT INTO trades(type,price,time,confidence,pnl,optionPremium)"
            " VALUES(?,?,?,?,?,?)",
            (t.type, t.price, t.time, t.confidence, t.pnl, t.option_premium),
        )
        await self._db.commit()
        today = date.today().isoformat()
        if self._cache_date != today:
            self._trade_cache.clear()
            self._cache_date = today
        if t.time and t.time.startswith(today):
            self._trade_cache.append(t)

    def get_today_trades(self) -> List[Trade]:
        """Sync read from in-memory cache — safe to call from any context."""
        today = date.today().isoformat()
        if self._cache_date != today:
            # cache is stale; return what we have — refresh happens on next save
            return list(self._trade_cache)
        return list(self._trade_cache)

    async def get_all_trades(self) -> List[Trade]:
        async with self._db.execute("SELECT * FROM trades ORDER BY id ASC") as cur:
            return [self._row_to_trade(r) for r in await cur.fetchall()]

    async def clear_all_trades(self) -> None:
        await self._db.execute("DELETE FROM trades")
        await self._db.commit()
        self._trade_cache.clear()
        self._cache_date = date.today().isoformat()

    async def _refresh_trade_cache(self) -> None:
        today = date.today().isoformat()
        async with self._db.execute(
            "SELECT * FROM trades WHERE time LIKE ? ORDER BY id ASC",
            (today + "%",),
        ) as cur:
            self._trade_cache = [self._row_to_trade(r) for r in await cur.fetchall()]
        self._cache_date = today

    @staticmethod
    def _row_to_trade(row: aiosqlite.Row) -> Trade:
        t               = Trade()
        t.id            = row["id"]
        t.type          = row["type"]
        t.price         = row["price"]
        t.time          = row["time"]
        t.confidence    = row["confidence"]
        t.pnl           = row["pnl"]
        t.option_premium = row["optionPremium"]
        return t

    # ── Big Trades ────────────────────────────────────────────────────────────

    async def get_big_trades_data(self, interval: str) -> Dict[str, List[Dict[str, Any]]]:
        interval_min = {"3m": 3, "5m": 5, "15m": 15}.get(interval, 1)
        today        = date.today().isoformat()
        async with self._db.execute(
            "SELECT stockname, time, qty, ltp FROM stocks"
            " WHERE time LIKE ? AND qty > 0 ORDER BY time DESC LIMIT 5000",
            (today + "%",),
        ) as cur:
            rows = await cur.fetchall()

        buckets: Dict[str, Dict[str, list]] = {}
        for row in rows:
            sname = row["stockname"]
            if not sname or sname == "BANKNIFTY":
                continue
            qty    = float(row["qty"] or 0)
            ltp    = float(row["ltp"] or 0)
            bucket = _to_bucket_time(row["time"], interval_min)
            if not bucket:
                continue
            stock_bkt = buckets.setdefault(sname, {})
            if bucket not in stock_bkt:
                stock_bkt[bucket] = [qty, ltp]
            else:
                stock_bkt[bucket][0] += qty

        result: Dict[str, List[Dict[str, Any]]] = {}
        for sname, time_bkts in buckets.items():
            sorted_rows = sorted(time_bkts.items(), key=lambda x: x[0], reverse=True)[:10]
            result[sname] = [
                {"time": t, "qty": int(vals[0]), "ltp": vals[1]}
                for t, vals in sorted_rows
            ]
        return result

    async def audit_stock_qty_storage(self, limit: int = 200) -> Dict[str, Any]:
        async with self._db.execute(
            "SELECT stockname, time, qty FROM stocks ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        tracked = {
            "HDFC BANK", "ICICI BANK", "AXIS BANK",
            "STATE BANK OF INDIA", "KOTAK MAHINDRA BANK", "INDUSIND BANK",
        }
        today = date.today().isoformat()
        scanned = qty_rows = tracked_qty = tracked_today = 0
        samples: List[str] = []
        for row in rows:
            scanned  += 1
            sname     = row["stockname"]
            t         = row["time"]
            has_qty   = row["qty"] and row["qty"] > 0
            is_tracked = sname in tracked
            is_today  = t and t.startswith(today)
            if has_qty:                       qty_rows     += 1
            if is_tracked and has_qty:        tracked_qty  += 1
            if is_tracked and is_today and has_qty: tracked_today += 1
            if len(samples) < 5 and (is_tracked or has_qty):
                samples.append(f"{sname} | {t} | Qty: {row['qty']}")
        return {
            "scanned":         scanned,
            "qtyRows":         qty_rows,
            "trackedQty":      tracked_qty,
            "trackedTodayQty": tracked_today,
            "sampleText":      " | ".join(samples) if samples else "No recent stock rows found.",
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_bucket_time(time_str: str, interval_min: int) -> Optional[str]:
    try:
        if not time_str or len(time_str) < 5:
            return None
        if len(time_str) >= 16 and time_str[10] in (" ", "T"):
            t = time_str[11:16]
        else:
            t = time_str[:5]
        hh, mm = int(t[:2]), int(t[3:5])
        return f"{hh:02d}:{(mm // interval_min) * interval_min:02d}"
    except Exception:
        return None
