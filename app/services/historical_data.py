from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo

import httpx

import app.config as cfg
from app.models import Candle
from app.state import get_state

IST = ZoneInfo("Asia/Kolkata")

# One shared client (verify=False — remote API uses self-signed cert)
_CLIENT_KWARGS = dict(verify=False, timeout=30.0)


def _parse_candles(arr: list) -> List[Candle]:
    return [
        Candle(
            start_time=n.get("start_time", ""),
            open=n.get("open",   0.0),
            close=n.get("close", 0.0),
            high=n.get("high",   0.0),
            low=n.get("low",     0.0),
            volume=n.get("volume", 0.0),
        )
        for n in arr
    ]


def _calculate_dates(interval: str, num_candles: int, offset: int):
    mins    = {"3m": 3, "5m": 5, "15m": 15}.get(interval, 1)
    now     = datetime.now(IST)
    snapped = (now.minute // mins) * mins
    end     = now.replace(minute=snapped, second=0, microsecond=0)
    cap     = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if end > cap:
        end = cap
    start = end - timedelta(minutes=(num_candles + offset) * mins)
    fmt   = "%Y-%m-%dT%H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


async def fetch_historical(
    interval:   str,
    num_candles: int,
    offset:      int,
) -> Dict[str, List[Candle]]:
    result: Dict[str, List[Candle]] = {}
    try:
        from_date, to_date = _calculate_dates(interval, num_candles, offset)
        url     = cfg.API_URL_TEMPLATE.format(cfg.API_HOST, from_date, to_date)
        payload = [
            {"stockname": s.name, "stock_symbol": s.symbol, "intervals": [interval]}
            for s in cfg.STOCKS
        ]
        async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        key  = f"{interval} data"
        for stock_node in data:
            symbol  = stock_node.get("stock_symbol", "")
            cn      = stock_node.get(key, [])
            if not isinstance(cn, list):
                continue
            candles = _parse_candles(cn)
            total   = num_candles + offset
            if offset == 0:
                window = candles[-num_candles:] if len(candles) > num_candles else candles
            else:
                from_idx = max(0, len(candles) - total)
                to_idx   = max(0, len(candles) - offset)
                window   = candles[from_idx:to_idx]
            result[symbol] = list(window)
        get_state().api_status = "API OK"
    except Exception as e:
        get_state().api_status = f"API Error: {e}"
        print(f"fetchHistorical error: {e}")
    return result


async def fetch_bn_indicator_candles(interval: str) -> List[Candle]:
    try:
        today     = date.today()
        from_date = (today - timedelta(days=7)).isoformat()
        to_date   = (today + timedelta(days=1)).isoformat()
        url       = cfg.API_URL_TEMPLATE.format(cfg.API_HOST, from_date, to_date)
        payload   = [{"stockname": "BANKNIFTY", "stock_symbol": "26009",
                       "intervals": [interval]}]
        async with httpx.AsyncClient(**_CLIENT_KWARGS) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
        if isinstance(data, list) and data:
            cn = data[0].get(f"{interval} data", [])
            if isinstance(cn, list):
                return _parse_candles(cn)
    except Exception as e:
        print(f"fetchBNIndicatorCandles error: {e}")
    return []
