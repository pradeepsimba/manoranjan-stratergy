"""
Throwaway dev tool — NOT shipped functionality (same status as
scripts/discover_instruments.py).

Checks the market-data server's 5-minute candles for the `open`-field defect
found on 2026-09-02: the server reports each bar's `open` as the PREVIOUS
bar's close, and the day's first bar's `open` as the PREVIOUS DAY's close,
instead of the first price actually traded inside that bar's window.

That breaks anything comparing close-vs-open (red/green candle tests — see
app/services/scanner.py) and additionally corrupts `high`/`low` whenever the
carried-over value falls outside the bar's true range.

Run it before and after fixing the server:

    python scripts/verify_candle_opens.py

Exit code 0 = all three checks pass (data looks correct).
Exit code 1 = the defect is still present.
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import app.config as cfg

IST = ZoneInfo("Asia/Kolkata")

# A handful of liquid names is enough to expose a systematic aggregation bug.
SAMPLE = [
    ("Hyundai Motor India", "HYUNDAI"),
    ("Punjab National Bank", "PNB"),
    ("Bosch", "BOSCHLTD"),
    ("Steel Authority of India", "SAIL"),
    ("Federal Bank", "FEDERALBNK"),
]

# Tolerances: prices are compared as floats coming off a JSON feed.
EPS = 1e-9
# A couple of bars legitimately open exactly at the previous close (thin
# trading, or a bar with a single trade), so only flag a systematic pattern.
STITCHED_RATIO_LIMIT = 0.25   # >25% of bars == previous close is not plausible


def _ctx() -> ssl.SSLContext:
    # Self-signed cert on the market-data server — same posture as the app
    # (see app/services/historical_data.py).
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch(days_back: int = 3) -> List[Dict[str, Any]]:
    today = datetime.now(IST).date()
    from_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%dT09:15:00")
    to_date = today.strftime("%Y-%m-%dT15:30:00")
    url = cfg.API_URL_TEMPLATE.format(cfg.API_HOST, from_date, to_date)
    payload = [
        {"stockname": name, "stock_symbol": code, "intervals": [cfg.INTERVAL_5M]}
        for name, code in SAMPLE
    ]
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ctx(), timeout=180) as resp:
        data = json.load(resp)
    return data if isinstance(data, list) else []


def _check(node: Dict[str, Any]) -> List[str]:
    """Returns a list of failure strings for one instrument (empty == clean)."""
    name = node.get("stockname", "?")
    bars = node.get(f"{cfg.INTERVAL_5M} data", [])
    if not bars:
        return [f"{name}: no candles returned"]

    failures: List[str] = []

    # 1. Does each bar's open just echo the previous bar's close?
    stitched = sum(
        1 for i in range(1, len(bars))
        if abs(bars[i]["open"] - bars[i - 1]["close"]) < EPS
    )
    ratio = stitched / max(len(bars) - 1, 1)
    if ratio > STITCHED_RATIO_LIMIT:
        failures.append(
            f"{name}: {stitched}/{len(bars) - 1} bars ({ratio:.0%}) have open == previous bar's close"
        )

    # 2. Does each day's FIRST bar open at the previous day's close?
    days = sorted({b["start_time"][:10] for b in bars})
    prev_last_close = None
    for day in days:
        day_bars = [b for b in bars if b["start_time"].startswith(day)]
        first = day_bars[0]
        if prev_last_close is not None and abs(first["open"] - prev_last_close) < EPS:
            failures.append(
                f"{name}: {day} first bar opens at {first['open']} == previous day's close"
            )
        prev_last_close = day_bars[-1]["close"]

    # 3. Is the open pinned to the bar's own high/low? That's the signature of a
    # carried-over value landing outside the bar's true traded range.
    pinned = sum(
        1 for b in bars
        if abs(b["open"] - b["high"]) < EPS or abs(b["open"] - b["low"]) < EPS
    )
    pinned_ratio = pinned / len(bars)
    if pinned_ratio > STITCHED_RATIO_LIMIT:
        failures.append(
            f"{name}: {pinned}/{len(bars)} bars ({pinned_ratio:.0%}) have open sitting exactly at the bar's high or low"
        )

    return failures


def main() -> int:
    print(f"Checking {cfg.API_HOST} 5m candle `open` values for {len(SAMPLE)} instruments…\n")
    try:
        data = _fetch()
    except Exception as e:
        print(f"Fetch failed: {e}")
        return 1

    if not data:
        print("Server returned no data — check stockname/symbol_code spelling "
              "(the server matches by stockname TEXT, see CLAUDE.md).")
        return 1

    all_failures: List[str] = []
    for node in data:
        failures = _check(node)
        label = node.get("stockname", "?")
        if failures:
            print(f"[FAIL] {label}")
            for f in failures:
                print(f"       - {f}")
        else:
            print(f"[ OK ] {label}")
        all_failures.extend(failures)

    print()
    if all_failures:
        print(f"{len(all_failures)} problem(s) found — the `open` field is still "
              f"derived rather than the first trade inside each bar's window.")
        return 1
    print("All checks passed — `open` values look like real first-traded prices.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
