from datetime import datetime
from typing import Dict, List, Any
from zoneinfo import ZoneInfo

from app.models import Candle

IST = ZoneInfo("Asia/Kolkata")

def run_dynamic_zone_scan(
    candles_1d_dict: Dict[str, List[Candle]],
    candles_5m_dict: Dict[str, List[Candle]],
    token_to_name: Dict[str, str],
) -> List[Dict[str, Any]]:
    """
    Runs the dynamic zone scan for all stocks.
    
    daily_df: represented by candles_1d_dict
    intraday_5m_df: represented by candles_5m_dict
    """
    signals = []
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    
    for token, c5_list in candles_5m_dict.items():
        symbol = token_to_name.get(token)
        if not symbol:
            continue
            
        c1d_list = candles_1d_dict.get(token, [])
        if not c1d_list or not c5_list:
            continue
            
        # 1. Calculate Daily Ranges and ADR
        c1d_sorted = sorted(c1d_list, key=lambda c: c.start_time)
        c5_sorted = sorted(c5_list, key=lambda c: c.start_time)
        
        daily_ranges = [c.high - c.low for c in c1d_sorted]
        
        date_to_adr = {}
        date_to_daily_open = {}
        for i in range(len(c1d_sorted)):
            date_str = c1d_sorted[i].start_time[:10]
            date_to_daily_open[date_str] = c1d_sorted[i].open
            
            # ADR for date[i] uses the 10 days *before* it (shift(1)), so the
            # minimum required index is 10 (indices 0-9 are the prior 10 days).
            if i >= 10:
                adr_5  = sum(daily_ranges[i-5:i])  / 5.0
                adr_10 = sum(daily_ranges[i-10:i]) / 10.0
                date_to_adr[date_str] = (adr_5, adr_10)

        # Cover today (including live-market where today's daily candle may be
        # in-progress/incomplete). We derive ADR from the last 10 *closed* daily
        # candles, i.e. we exclude today if today's bar is already present.
        if len(c1d_sorted) >= 10:
            last_date = c1d_sorted[-1].start_time[:10]
            if last_date == today_ist:
                # Today's daily candle is live/incomplete — use the 10 closed days before it
                closed_ranges = daily_ranges[:-1]   # drop today's partial bar
                if len(closed_ranges) >= 10:
                    adr_5  = sum(closed_ranges[-5:])  / 5.0
                    adr_10 = sum(closed_ranges[-10:]) / 10.0
                    date_to_adr[today_ist] = (adr_5, adr_10)
            else:
                # Today's daily candle hasn't appeared yet — use the last 10 fully-closed bars
                adr_5  = sum(daily_ranges[-5:])  / 5.0
                adr_10 = sum(daily_ranges[-10:]) / 10.0
                date_to_adr[today_ist] = (adr_5, adr_10)
                
        # 2. Group 5-minute candles by Date
        candles_by_date: Dict[str, List[Candle]] = {}
        for c in c5_sorted:
            d_str = c.start_time[:10]
            candles_by_date.setdefault(d_str, []).append(c)
            
        # 3. For each day, isolate the first candle and check conditions
        for d_str, day_candles in sorted(candles_by_date.items()):
            if not day_candles:
                continue
                
            first_c = day_candles[0]
            
            # Check if we have ADR calculations for this date
            if d_str not in date_to_adr:
                continue
                
            adr_5, adr_10 = date_to_adr[d_str]
            
            # Use daily open from daily candle if available, otherwise fall back to first 5m open
            daily_open = date_to_daily_open.get(d_str, first_c.open)
            
            # Calculate Dynamic Zones
            adr_high_10 = daily_open + (adr_10 / 2.0)
            adr_low_10 = daily_open - (adr_10 / 2.0)
            adr_high_5 = daily_open + (adr_5 / 2.0)
            adr_low_5 = daily_open - (adr_5 / 2.0)
            
            # Scenario A: Open = Low AND High > Dynamic Range High Zone (using max zone ADR_10)
            is_bullish = (first_c.open == first_c.low) and (first_c.high > adr_high_10)
            
            # Scenario B: Open = High AND Low < Dynamic Range Low Zone (using bottom zone ADR_10)
            is_bearish = (first_c.open == first_c.high) and (first_c.low < adr_low_10)
            
            if is_bullish:
                signals.append({
                    "symbol": symbol,
                    "date": d_str,
                    "type": "bullish",
                    "open": round(first_c.open, 2),
                    "high": round(first_c.high, 2),
                    "low": round(first_c.low, 2),
                    "close": round(first_c.close, 2),
                    "adr_5": round(adr_5, 2),
                    "adr_10": round(adr_10, 2),
                    "adr_high_10": round(adr_high_10, 2),
                    "adr_low_10": round(adr_low_10, 2),
                })
            elif is_bearish:
                signals.append({
                    "symbol": symbol,
                    "date": d_str,
                    "type": "bearish",
                    "open": round(first_c.open, 2),
                    "high": round(first_c.high, 2),
                    "low": round(first_c.low, 2),
                    "close": round(first_c.close, 2),
                    "adr_5": round(adr_5, 2),
                    "adr_10": round(adr_10, 2),
                    "adr_high_10": round(adr_high_10, 2),
                    "adr_low_10": round(adr_low_10, 2),
                })
                
    return signals
