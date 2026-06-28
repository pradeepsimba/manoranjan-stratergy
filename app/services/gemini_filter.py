from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from typing import Dict, List

import app.config as cfg


async def analyse_stocks(stocknames: List[str]) -> Dict[str, str]:
    """
    Send the full stock list to Gemini for news-based intraday condition analysis.

    Each stock is classified as:
      BULLISH  — strong intraday upside potential today
      NEUTRAL  — mixed or no clear catalyst
      BEARISH  — downside risk or weak setup

    Returns {stockname: condition}.
    Falls back to {} on API failure so the caller can decide the fallback policy.
    """
    if not cfg.GEMINI_API_KEY:
        print("GEMINI_API_KEY not set — skipping Gemini analysis")
        return {}
    if not stocknames:
        return {}

    import google.generativeai as genai

    genai.configure(api_key=cfg.GEMINI_API_KEY)
    model = genai.GenerativeModel(cfg.GEMINI_MODEL)

    today      = date.today().strftime("%A, %d %B %Y")
    stock_list = ", ".join(stocknames)

    prompt = f"""Today is {today}. You are an intraday NSE equity screener.

Analyse the following {len(stocknames)} NSE stocks for intraday trading potential today.
Consider: breaking news, sector momentum, FII/DII activity, overnight gaps vs NIFTY,
earnings/results calendars, and technical setup context.

Stocks to analyse:
{stock_list}

Classify EVERY stock using exactly one of these labels:
  BULLISH  — clear intraday upside catalyst or strong momentum setup today
  NEUTRAL  — no clear directional edge today
  BEARISH  — negative news, sector weakness, or downside risk today

Return ONLY a valid JSON array. No markdown fences, no explanation, no extra text.
Format:
[
  {{"symbol": "WIPRO", "condition": "BULLISH", "reason": "IT sector rally, strong results beat"}},
  {{"symbol": "BAJAJ AUTO", "condition": "NEUTRAL", "reason": "No material catalyst today"}},
  ...
]

You MUST include ALL {len(stocknames)} stocks. Raw JSON only."""

    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        raw = response.text.strip()

        # Strip markdown code fences if Gemini wraps the response
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$",          "", raw)

        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError(f"Expected list, got {type(items).__name__}")

        conditions: Dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            sym  = str(item.get("symbol", "")).strip()
            cond = str(item.get("condition", "")).strip().upper()
            if sym and cond in {"BULLISH", "NEUTRAL", "BEARISH"}:
                conditions[sym] = cond

        bullish = sum(1 for c in conditions.values() if c == "BULLISH")
        print(f"Gemini analysis complete: {len(conditions)}/{len(stocknames)} rated, "
              f"{bullish} BULLISH")
        return conditions

    except Exception as e:
        print(f"Gemini analysis error: {e}")
        return {}
