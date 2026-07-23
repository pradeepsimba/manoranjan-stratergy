from __future__ import annotations

"""
Pre-market AI screen using the modern Google Gen AI SDK (google-genai).

Sends the full high-volume stock list to Gemini with live Google Search
grounding and a strict JSON-array response schema. Returns the subset of
symbols Gemini judges likely to show intraday bullish momentum today.

On any failure (no key, network drop, grounding error, malformed output) the
wrapper returns an empty list, which the scheduler treats as "trade the full
client-status list" — the architecture's built-in safe fallback.
"""

import asyncio
import json
import re
from datetime import date
from typing import List, Optional

import app.config as cfg

# Non-greedy pattern: stops at the FIRST ] so multiple arrays in one response
# (e.g. from citations) don't merge into a single wrong match.
_JSON_ARRAY = re.compile(r"\[.*?\]", re.DOTALL)

# Without an explicit timeout, a network stall on Google's side never raises -
# it just blocks forever. The module's whole safety design ("on any failure,
# return [] and fall back to the full watchlist") only fires on an exception,
# so a hang here means the pre-market screen never completes AND permanently
# ties up one thread of the shared default ThreadPoolExecutor that
# asyncio.to_thread() draws from (the same pool historical_data.py's JSON
# decode/candle-parsing offloads use to keep the event loop from stalling).
_TIMEOUT_MS = 60_000

# At full-universe scale (thousands of stock names in one prompt) this is less about hitting
# Gemini's context window (it wouldn't, even at 10,000+ names) and more that asking a model to
# reason over that many tickers in a single pass degrades per-symbol attention/answer quality.
# Batched sequentially (not concurrently) below - this is the once-a-day, non-latency-critical
# pre-market screen, and this codebase is deliberately conservative about concurrent load against
# external APIs elsewhere too.
GEMINI_BATCH_SIZE = 1000


def _find_json_array(text: str, known: Optional[set] = None) -> list:
    """Return the answer JSON string-array from a grounded model response.

    Grounded responses can contain several `[...]` string arrays — the answer,
    but also source-domain lists or (if the model editorializes) a second
    "bearish"/avoid array. When `known` (the input symbol set, uppercased) is
    given: the FIRST candidate containing a known symbol wins. The prompt asks
    for the bullish list first, so neither a trailing bearish array (however
    long) nor a sources list can displace it — and a genuine 1-2 symbol answer
    still wins over anything after it. Safe against a preamble echo of the
    prompt's example because the example uses placeholder names
    ("SYMBOL1"/"SYMBOL2") that can never match `known` — keep it that way.
    Falls back to the last valid string-array (skips citation arrays like [1]).
    """
    candidates = []
    for candidate in _JSON_ARRAY.findall(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed):
            candidates.append(parsed)
    if known:
        for parsed in candidates:
            if any(s.strip().upper() in known for s in parsed):
                return parsed
    if candidates:
        return candidates[-1]
    raise ValueError("no valid JSON string-array in grounded response")


async def analyse_stocks(stocknames: List[str]) -> List[str]:
    """
    Async entry point. Runs the (synchronous) grounded screen in a worker
    thread so the event loop is never blocked.

    Returns a list of BULLISH NSE symbols, or [] to trigger the full-list
    fallback.
    """
    if not cfg.GEMINI_API_KEY:
        print("GEMINI_API_KEY not set — skipping Gemini filter (full-list fallback)")
        return []
    if not stocknames:
        return []

    if len(stocknames) <= GEMINI_BATCH_SIZE:
        return await asyncio.to_thread(_grounded_screen, stocknames)

    # Sequential batches, not asyncio.gather - see GEMINI_BATCH_SIZE's comment. One batch's
    # failure returns [] for just that batch (partial screen), not the whole run.
    bullish: List[str] = []
    total_batches = (len(stocknames) + GEMINI_BATCH_SIZE - 1) // GEMINI_BATCH_SIZE
    for i in range(0, len(stocknames), GEMINI_BATCH_SIZE):
        batch_num = i // GEMINI_BATCH_SIZE + 1
        chunk = stocknames[i:i + GEMINI_BATCH_SIZE]
        result = await asyncio.to_thread(_grounded_screen, chunk)
        print(f"Gemini batch {batch_num}/{total_batches}: {len(result)} bullish of {len(chunk)}")
        bullish.extend(result)

    # Dedup while preserving order, THEN cap on the merged list - capping per-batch first would
    # bias the final result toward whichever batch happened to run first.
    seen = set()
    deduped = []
    for symbol in bullish:
        if symbol not in seen:
            seen.add(symbol)
            deduped.append(symbol)
    return deduped[:cfg.GEMINI_MAX_STOCKS]


def _grounded_screen(stocknames: List[str]) -> List[str]:
    """
    Synchronous grounded screen. Wrapped end-to-end in try/except: any error
    logs and returns [] so the caller falls back to the full watchlist safely.
    """
    try:
        from google import genai
        from google.genai import types

        # Auto-reads GEMINI_API_KEY / GOOGLE_API_KEY from the environment.
        client = genai.Client()

        today      = date.today().strftime("%A, %d %B %Y")
        stock_list = ", ".join(stocknames)
        prompt = (
            f"Today is {today}. Use Google Search to review the latest pre-market "
            f"news, results announcements, sector momentum, and overnight global "
            f"cues for Indian (NSE) equities.\n\n"
            f"From the following NSE stocks, return ONLY the symbols most likely to "
            f"show INTRADAY BULLISH momentum today, using each symbol exactly as "
            f"given. Respond with ONLY a JSON array of strings, e.g. "
            f'["SYMBOL1","SYMBOL2"]. No prose, no markdown.\n\n'
            f"Stocks: {stock_list}"
        )

        # Google-Search grounding. NOTE: the API rejects a response_schema /
        # response_mime_type when a search tool is present, so we ask for a JSON
        # array in the prompt and extract it from the grounded text below.
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=1.0,
            http_options=types.HttpOptions(timeout=_TIMEOUT_MS),
        )

        response = client.models.generate_content(
            model=cfg.GEMINI_MODEL,
            contents=prompt,
            config=config,
        )

        raw     = (response.text or "").strip()
        symbols = _find_json_array(raw, {s.strip().upper() for s in stocknames})

        clean = [
            s.strip().upper()
            for s in symbols
            if isinstance(s, str) and s.strip()
        ][: cfg.GEMINI_MAX_STOCKS]

        print(f"Gemini grounded screen: {len(clean)} BULLISH of {len(stocknames)}")
        return clean

    except Exception as e:
        print(f"Gemini grounded screen failed ({e}) — full-list fallback")
        return []
