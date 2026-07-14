"""
Throwaway dev tool — NOT shipped functionality (same convention as the old
bn_smoke_test.py). Run manually to (re-)discover the tradable instrument
universe against the market-data server's live catalog and persist verified
symbols into the `instruments` table, without starting the whole app.

Usage: python scripts/discover_instruments.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app.services.database import DatabaseService
from app.services.instrument_discovery import discover_and_verify


async def main() -> None:
    db = DatabaseService()
    await db.init()
    try:
        rows = await discover_and_verify()
        await db.upsert_instruments(rows)
        print(f"Persisted {len(rows)} tradable instruments.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
