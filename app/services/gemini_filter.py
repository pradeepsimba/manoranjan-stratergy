from __future__ import annotations

import json
import re
from datetime import date
from typing import List

import app.config as cfg


async def fetch_gemini_shortlist() -> List[str]:
    """
    Call Gemini to get a pre-market AI shortlist of 15–40 NSE stocks likely
    to show intraday bullish momentum today.

    Returns a list of NSE trading symbols (e.g. ["RELIANCE", "TCS", ...]).
    Falls back to an empty list if the API key is missing or the call fails.
    """
    if not cfg.GEMINI_API_KEY:
        print("GEMINI_API_KEY not set — skipping AI filter, using full universe")
        return []

    import asyncio
    import google.generativeai as genai

    genai.configure(api_key=cfg.GEMINI_API_KEY)
    model = genai.GenerativeModel(cfg.GEMINI_MODEL)

    today = date.today().strftime("%A, %d %B %Y")
    prompt = f"""Today is {today}. You are a pre-market NSE equity screener.

Analyse current market conditions — recent news catalysts, sector momentum,
overnight relative strength vs NIFTY 50, volume gap setups, and breakout
candidates — and return {cfg.GEMINI_MIN_STOCKS}–{cfg.GEMINI_MAX_STOCKS} NSE
equity symbols most likely to exhibit intraday bullish momentum today.

Rules:
- Only NSE Cash/Equity segment (no ETFs, no indices, no F&O-only instruments).
- Stock price must be above ₹100.
- Average daily volume must be at least 1,000,000 shares.
- Prefer stocks with news or sector tailwinds today.

Return ONLY a valid JSON array of NSE trading symbols in uppercase with no suffix.
Example format: ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK"]
No markdown, no explanation, no extra text — just the raw JSON array."""

    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        raw = response.text.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        symbols: List[str] = json.loads(raw)
        if not isinstance(symbols, list):
            raise ValueError("Gemini returned non-list response")

        # Sanitise symbols
        clean = [
            s.strip().upper().replace("-EQ", "").replace(".NS", "")
            for s in symbols
            if isinstance(s, str) and s.strip()
        ]
        clean = clean[:cfg.GEMINI_MAX_STOCKS]

        print(f"Gemini shortlist ({len(clean)} stocks): {clean}")
        return clean

    except Exception as e:
        print(f"Gemini filter error: {e}")
        return []
