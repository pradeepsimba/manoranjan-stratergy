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
from typing import List

import app.config as cfg

# Pulls the first JSON array out of a grounded text response (which may wrap it
# in markdown fences or surround it with prose / citations).
_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


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
    return await asyncio.to_thread(_grounded_screen, stocknames)


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
            f'["RELIANCE","TCS"]. No prose, no markdown.\n\n'
            f"Stocks: {stock_list}"
        )

        # Google-Search grounding. NOTE: the API rejects a response_schema /
        # response_mime_type when a search tool is present, so we ask for a JSON
        # array in the prompt and extract it from the grounded text below.
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=1.0,
        )

        response = client.models.generate_content(
            model=cfg.GEMINI_MODEL,
            contents=prompt,
            config=config,
        )

        raw   = (response.text or "").strip()
        match = _JSON_ARRAY.search(raw)
        if not match:
            raise ValueError("no JSON array in grounded response")
        symbols = json.loads(match.group(0))
        if not isinstance(symbols, list):
            raise ValueError(f"Expected JSON array, got {type(symbols).__name__}")

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
