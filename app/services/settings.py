from __future__ import annotations

"""
Dynamic settings registry + persistence.

SPEC declares every runtime-editable tunable: display metadata, type, bounds,
and whether it may be overridden per-backtest-run ("bt"). Values themselves
live in app.config (defaults + runtime overrides); this module validates user
input, expands virtual "HH:MM" time settings into their HOUR/MIN config pairs,
and persists overrides to the app_settings table so they survive restarts.

Add a new tunable by adding its default to app.config._DEFAULTS AND an entry
here — nothing else is required for it to appear on the Settings page.
"""

import re
from typing import Any, Dict, List, Optional

import app.config as cfg

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Keys in app_settings that are NOT config overrides (e.g. day-scoped watchlist
# edits). Prefixed with "_" and skipped by the settings loader.
INTERNAL_PREFIX = "_"
WATCHLIST_OVERRIDES_KEY = "_WATCHLIST_OVERRIDES"


def _s(key: str, label: str, type_: str, group: str, *,
       min_: Optional[float] = None, max_: Optional[float] = None,
       step: Optional[float] = None, help_: str = "", bt: bool = True,
       parts: Optional[tuple] = None,
       choices: Optional[list] = None,
       cond: Optional[str] = None) -> Dict[str, Any]:
    # cond = the COND_* toggle key this value belongs to; the Settings UI nests
    # it under that condition instead of showing it in a separate group.
    return {"key": key, "label": label, "type": type_, "group": group,
            "min": min_, "max": max_, "step": step, "help": help_,
            "bt": bt, "parts": parts, "choices": choices, "cond": cond}


SPEC: List[Dict[str, Any]] = [
    # ── AI pre-market screen (live only) ─────────────────────────────────────
    _s("GEMINI_ENABLED", "Gemini screen enabled", "bool", "AI Pre-market Screen",
       help_="Off = skip the AI screen and trade the capped full high-volume list.", bt=False),
    _s("GEMINI_MODEL", "Gemini model id", "str", "AI Pre-market Screen",
       help_="Must be a real google-genai model id; a bad id silently disables the screen.", bt=False),
    _s("GEMINI_MODE", "Screen mode", "choice", "AI Pre-market Screen",
       choices=cfg.GEMINI_MODES, bt=False,
       help_="bullish = trade ONLY the symbols Gemini calls bullish (whitelist) · "
             "exclude_risky = trade the whole universe EXCEPT the symbols Gemini "
             "flags as risky today (blacklist). Both respect the cap below."),
    _s("GEMINI_MAX_STOCKS", "Max tradeable stocks", "int", "AI Pre-market Screen",
       min_=1, max_=2000, help_="Hard cap on the tradeable list in BOTH screen "
       "modes — in exclude_risky it is what stops 'everything that isn't risky' "
       "from becoming thousands of symbols. The WS feed chunks safely across "
       "connections, but every TRADEABLE symbol gets a full TA-Lib scan on each "
       "tick cycle, so raising this far past a few hundred needs CPU headroom "
       "(watch for 'Scan pool timed out' in the logs).", bt=False),

    # ── Session timings ───────────────────────────────────────────────────────
    _s("PREMARKET_TIME", "Pre-market screen", "time", "Session Timings",
       parts=("PREMARKET_HOUR", "PREMARKET_MIN"), bt=False,
       help_="When the watchlist fetch + Gemini screen run."),
    _s("MARKET_OPEN_TIME", "Market open / data load", "time", "Session Timings",
       parts=("MARKET_OPEN_HOUR", "MARKET_OPEN_MIN"), bt=False,
       help_="Historical load + WebSocket subscribe."),
    _s("SCAN_START_TIME", "Entry scanning starts", "time", "Session Timings",
       parts=("SCAN_START_HOUR", "SCAN_START_MIN"),
       help_="No entries before this time (also used by the backtest)."),
    _s("CUTOFF_TIME", "Entry cutoff", "time", "Session Timings",
       parts=("CUTOFF_HOUR", "CUTOFF_MIN"),
       help_="No new entries after this; exits keep running (also used by the backtest)."),
    _s("SESSION_END_TIME", "Session end / square-off", "time", "Session Timings",
       parts=("SESSION_END_HOUR", "SESSION_END_MIN"), bt=False,
       help_="EOD square-off and daily reset."),

    # ── Risk & capital ────────────────────────────────────────────────────────
    _s("RISK_MODE", "Risk basis", "choice", "Risk & Capital",
       choices=cfg.RISK_MODES,
       help_="fixed_amount = risk ₹X per trade · capital_pct = risk X% of "
             "account capital per trade. Stop placement (swing low) is the "
             "same in both — only the share count changes."),
    _s("RISK_PER_TRADE", "Risk per trade ₹", "float", "Risk & Capital",
       min_=1, max_=100_000_000, step=50,
       help_="Qty = risk ÷ stop distance (used when Risk basis = fixed_amount)."),
    _s("RISK_CAPITAL_PERCENT", "Risk % of capital", "float", "Risk & Capital",
       min_=0.1, max_=100, step=0.1,
       help_="A true percentage: 10 = a stop-out loses 10% of account capital "
             "(₹10 per ₹100). Used when Risk basis = capital_pct."),
    _s("ACCOUNT_BALANCE", "Account capital ₹", "float", "Risk & Capital",
       min_=1_000, max_=100_000_000, step=1000),
    _s("INTRADAY_LEVERAGE", "Intraday leverage ×", "int", "Risk & Capital",
       min_=1, max_=10),
    _s("MAX_CONCURRENT_POSITIONS", "Max open positions", "int", "Risk & Capital",
       min_=1, max_=20),
    _s("DAILY_LOSS_LIMIT", "Daily loss limit ₹", "float", "Risk & Capital",
       min_=100, max_=10_000_000, step=100, help_="No new entries once daily P&L breaches −limit."),

    # ── Strategy parameters ───────────────────────────────────────────────────
    # `cond=...` links a value to an Entry-Condition toggle so the UI shows it
    # inline under that condition. Ones with no cond are global (sizing/lookback).
    _s("RSI_MODE", "RSI rule", "choice", "Strategy", cond="COND_RSI",
       choices=["above_or_rising", "above", "below"],
       help_="above_or_rising = RSI > level OR rising · above = RSI > level · below = RSI < level (oversold)."),
    _s("RSI_OVERSOLD", "RSI level", "int", "Strategy", min_=5, max_=95, cond="COND_RSI",
       help_="The RSI level the rule compares against (your \"30\")."),
    _s("RSI_PERIOD", "RSI period", "int", "Strategy", min_=5, max_=50, cond="COND_RSI"),
    _s("RSI_RISING_BARS", "RSI rising bars", "int", "Strategy", min_=1, max_=10, cond="COND_RSI"),

    _s("ADX_THRESHOLD", "ADX threshold", "float", "Strategy", min_=5, max_=50, step=0.5, cond="COND_ADX"),
    _s("ADX_PERIOD", "ADX period", "int", "Strategy", min_=5, max_=50, cond="COND_ADX"),

    _s("MACD_CROSS_BARS", "MACD cross window (bars)", "int", "Strategy", min_=1, max_=10, cond="COND_MACD_CROSS"),
    _s("MACD_FAST", "MACD fast period", "int", "Strategy", min_=2, max_=100, cond="COND_MACD_CROSS"),
    _s("MACD_SLOW", "MACD slow period", "int", "Strategy", min_=3, max_=200, cond="COND_MACD_CROSS"),
    _s("MACD_SIGNAL", "MACD signal period", "int", "Strategy", min_=2, max_=100, cond="COND_MACD_CROSS"),

    _s("VOLUME_MULTIPLIER", "Volume surge ×", "float", "Strategy", min_=1.0, max_=10, step=0.1, cond="COND_VOLUME_SURGE"),
    _s("VOLUME_MA_PERIOD", "Volume MA period", "int", "Strategy", min_=5, max_=100, cond="COND_VOLUME_SURGE"),

    _s("SWING_LOW_BARS", "Support lookback bars", "int", "Strategy", min_=3, max_=50, cond="COND_NEAR_SUPPORT"),
    _s("SUPPORT_TOUCH_PCT", "Support proximity (fraction)", "float", "Strategy",
       min_=0.001, max_=0.10, step=0.001, cond="COND_NEAR_SUPPORT",
       help_="0.015 = within 1.5% above the swing low."),

    _s("DEPTH_MIN_RATIO", "Min order-book buy ratio", "float", "Strategy",
       min_=0.0, max_=1.0, step=0.05, cond="COND_DEPTH",
       help_="Live only — backtests have no order book."),

    _s("MIN_SL_OFFSET", "Min stop distance ₹", "float", "Strategy", min_=0.5, max_=10_000, step=0.5),
    _s("SL_PCT", "Stop-loss % of entry", "float", "Strategy", min_=0, max_=99, step=0.1,
       help_="A true percentage: 10 = stop 10% below entry (scales with the "
             "stock's price). 0 = structural stop at the swing low."),
    _s("RR_RATIO", "Reward : risk ratio", "float", "Strategy", min_=0.1, max_=100, step=0.1),
    _s("TALIB_LOOKBACK", "Indicator lookback bars", "int", "Strategy", min_=60, max_=290,
       help_="Tail fed to TA-Lib; must stay under the 300-bar candle buffer."),

    # ── Entry-condition toggles ───────────────────────────────────────────────
    _s("COND_NEAR_SUPPORT", "Near support", "bool", "Entry Conditions"),
    _s("COND_BULLISH_PATTERN", "Bullish candle pattern", "bool", "Entry Conditions"),
    _s("COND_ADX", "ADX trend strength", "bool", "Entry Conditions"),
    _s("COND_RSI", "RSI ok", "bool", "Entry Conditions"),
    _s("COND_MACD_CROSS", "MACD bullish cross", "bool", "Entry Conditions"),
    _s("COND_VOLUME_SURGE", "Volume surge", "bool", "Entry Conditions"),
    _s("COND_ABOVE_VWAP", "Price above VWAP", "bool", "Entry Conditions"),
    _s("COND_DEPTH", "Order-book depth bullish", "bool", "Entry Conditions",
       help_="Live only — the backtest always passes this."),

    # ── Custom entry rules (rendered by the dedicated builder UI) ────────────
    _s("CUSTOM_ENTRY_RULES", "Custom entry rules", "rules", "Entry Conditions",
       help_="OR-of-ANDs rule sets over any indicator; 'and' adds to the fixed "
             "conditions, 'replace' swaps them out (trend gates still apply)."),

    # ── Trend-gate toggles ────────────────────────────────────────────────────
    _s("GATE_STOCK_DAILY", "Stock daily green", "bool", "Trend Gates"),
    _s("GATE_STOCK_HOURLY", "Stock hourly green", "bool", "Trend Gates"),
    _s("GATE_NIFTY_DAILY", "NIFTY daily green", "bool", "Trend Gates"),
    _s("GATE_NIFTY_VWAP", "NIFTY above VWAP", "bool", "Trend Gates"),

    # ── Engine (live only) ───────────────────────────────────────────────────
    _s("TICK_EVAL_INTERVAL_MS", "Tick evaluation interval ms", "int", "Engine",
       min_=0, max_=5000, bt=False, help_="0 = run as fast as the loop allows."),
    _s("FULL_SCAN_INTERVAL_S", "Full-watchlist scan interval s", "int", "Engine",
       min_=30, max_=3600, bt=False),

    # ── Order-book scalper ────────────────────────────────────────────────────
    # A second strategy on the same tick loop (app/engine/scalper.py). EVERY key
    # here is bt=False: a backtest replays historical candles, which carry no
    # order book or tape at all, so none of this can be per-run overridden —
    # forward-test it with SCALP_DRY_RUN instead (see CLAUDE.md).
    _s("SCALP_ENABLED", "Scalper enabled", "bool", "Scalper", bt=False,
       help_="Master switch. Off = no book parsing, no tape, no scalp signals "
             "(zero cost on the tick path)."),
    _s("SCALP_DRY_RUN", "Dry run (log only)", "bool", "Scalper", bt=False,
       help_="On = evaluate and log every signal but place NO order. Leave on "
             "until the signal log looks right — this is the forward-test mode."),
    _s("SCALP_WARMUP_TIME", "Warm-up start (scan only)", "time", "Scalper",
       parts=("SCALP_WARMUP_HOUR", "SCALP_WARMUP_MIN"), bt=False,
       help_="Scanning starts; execution does NOT (avoids the opening auction)."),
    _s("SCALP_MORNING_TIME", "Morning window start", "time", "Scalper",
       parts=("SCALP_MORNING_HOUR", "SCALP_MORNING_MIN"), bt=False,
       help_="First window that may execute."),
    _s("SCALP_MIDDAY_TIME", "Midday window start", "time", "Scalper",
       parts=("SCALP_MIDDAY_HOUR", "SCALP_MIDDAY_MIN"), bt=False),
    _s("SCALP_AFTERNOON_TIME", "Afternoon window start", "time", "Scalper",
       parts=("SCALP_AFTERNOON_HOUR", "SCALP_AFTERNOON_MIN"), bt=False),
    _s("SCALP_SQUAREOFF_TIME", "Square-off / stop scanning", "time", "Scalper",
       parts=("SCALP_SQUAREOFF_HOUR", "SCALP_SQUAREOFF_MIN"), bt=False,
       help_="Cancels pending intents and flattens every scalp position."),
    _s("SCALP_MIDDAY_ENABLED", "Trade the midday window", "bool", "Scalper", bt=False,
       help_="Off = scanner paused through the midday chop (diagnostics only)."),
    _s("SCALP_RATIO_MORNING", "Required W-OBI ratio — morning", "float", "Scalper",
       min_=1.0, max_=50, step=0.1, bt=False,
       help_="Weighted bid depth ÷ weighted ask depth an entry must clear."),
    _s("SCALP_RATIO_MIDDAY", "Required W-OBI ratio — midday", "float", "Scalper",
       min_=1.0, max_=50, step=0.1, bt=False,
       help_="Deliberately stricter: midday imbalances mean-revert more often."),
    _s("SCALP_RATIO_AFTERNOON", "Required W-OBI ratio — afternoon", "float", "Scalper",
       min_=1.0, max_=50, step=0.1, bt=False),

    # ── Scalper: book + tape filters ──────────────────────────────────────────
    _s("SCALP_OBI_WEIGHTS", "Level weights", "str", "Scalper Filters", bt=False,
       help_="Comma-separated, nearest touch first (e.g. 1.0,0.8,0.6,0.4,0.2). "
             "Fewer entries = a shallower book is considered."),
    _s("SCALP_MIN_LEVELS", "Min levels per side", "int", "Scalper Filters",
       min_=1, max_=5, bt=False,
       help_="Both sides need this many parsed levels before the book is judged."),
    _s("SCALP_MIN_ORDER_COUNT", "Min bid-side order count", "int", "Scalper Filters",
       min_=0, max_=100_000, bt=False,
       help_="Aggregate orders across the depth below — many small orders are "
             "real interest; few large ones are one participant."),
    _s("SCALP_ORDER_COUNT_DEPTH", "Order-count depth (levels)", "int", "Scalper Filters",
       min_=1, max_=5, bt=False),
    _s("SCALP_SPOOF_DEPTH", "Anti-spoof depth (levels)", "int", "Scalper Filters",
       min_=1, max_=5, bt=False,
       help_="Check levels 1..N for a single-ticket wall."),
    _s("SCALP_SPOOF_MIN_SHARE", "Anti-spoof qty share", "float", "Scalper Filters",
       min_=0.05, max_=1.0, step=0.05, bt=False,
       help_="A level with orders==1 holding at least this share of displayed "
             "bid quantity is treated as a spoof and rejected (0.5 = 50%)."),
    _s("SCALP_REQUIRE_ORDER_DATA", "Require order-count data", "bool", "Scalper Filters",
       bt=False,
       help_="On = refuse to trade when the feed publishes no per-level order "
             "counts (fail-closed). Off = skip the count/spoof filters instead."),
    _s("SCALP_TAPE_WINDOW_S", "Tape window (s)", "float", "Scalper Filters",
       min_=1, max_=60, step=0.5, bt=False),
    _s("SCALP_TAPE_MAXLEN", "Tape prints retained", "int", "Scalper Filters",
       min_=5, max_=500, bt=False,
       help_="Per symbol. Must comfortably cover the tape window at your tick rate."),
    _s("SCALP_MIN_TAPE_TRADES", "Min tape prints", "int", "Scalper Filters",
       min_=1, max_=100, bt=False),
    _s("SCALP_MIN_TAPE_QTY", "Min aggressive buy qty", "float", "Scalper Filters",
       min_=0, max_=10_000_000, step=100, bt=False,
       help_="Shares bought AT the ask inside the tape window."),
    _s("SCALP_MIN_TAPE_BUY_RATIO", "Min tape buy ratio", "float", "Scalper Filters",
       min_=0.0, max_=1.0, step=0.05, bt=False,
       help_="Aggressive buy ÷ (buy + sell) volume. 0.6 = 60% of directional flow."),
    _s("SCALP_REQUIRE_ASK_HIT", "Require a print at the ask", "bool", "Scalper Filters",
       bt=False, help_="Confirms someone is actively paying up right now."),
    _s("SCALP_ASK_HIT_WINDOW_S", "Ask-hit window (s)", "float", "Scalper Filters",
       min_=0.5, max_=30, step=0.5, bt=False),
    _s("SCALP_MAX_BOOK_AGE_S", "Max book age (s)", "float", "Scalper Filters",
       min_=0.5, max_=60, step=0.5, bt=False,
       help_="A stale book is the most dangerous input here — the socket can "
             "stay connected while the depth feed goes quiet."),
    _s("SCALP_MAX_SPREAD_PCT", "Max spread %", "float", "Scalper Filters",
       min_=0.01, max_=5, step=0.01, bt=False,
       help_="Percent of price. The round trip pays the spread twice."),
    _s("SCALP_MAX_SLIPPAGE_PCT", "Max slippage %", "float", "Scalper Filters",
       min_=0.01, max_=5, step=0.01, bt=False,
       help_="Tolerated gap between the signal LTP and the projected fill."),
    _s("SCALP_ENTRY_AT_ASK", "Enter at the ask", "bool", "Scalper Filters", bt=False,
       help_="On = price the entry at the offer (a market buy crosses the "
             "spread). Off = price it at the last trade, which flatters fills."),

    # ── Scalper: sizing & risk ────────────────────────────────────────────────
    _s("SCALP_ALLOC_PCT", "Capital per trade %", "float", "Scalper Risk",
       min_=1, max_=100, step=1, bt=False,
       help_="% of account equity of OWN funds per scalp, before intraday "
             "leverage and capped by capital the open book hasn't committed."),
    _s("SCALP_RISK_MODE", "Risk basis", "choice", "Scalper Risk",
       choices=cfg.RISK_MODES, bt=False,
       help_="fixed_amount = risk ₹X per scalp · capital_pct = risk X% of equity."),
    _s("SCALP_RISK_PER_TRADE", "Risk per trade ₹", "float", "Scalper Risk",
       min_=1, max_=100_000_000, step=25, bt=False),
    _s("SCALP_RISK_CAPITAL_PERCENT", "Risk % of capital", "float", "Scalper Risk",
       min_=0.05, max_=100, step=0.05, bt=False,
       help_="A true percentage: 0.5 = a stop-out loses 0.5% of equity."),
    _s("SCALP_SL_PCT", "Stop-loss % of fill", "float", "Scalper Risk",
       min_=0.01, max_=10, step=0.01, bt=False),
    _s("SCALP_MIN_SL_OFFSET", "Min stop distance ₹", "float", "Scalper Risk",
       min_=0.01, max_=1000, step=0.05, bt=False,
       help_="Floor, so a low-priced stock can't get a sub-tick stop."),
    _s("SCALP_RR_RATIO", "Reward : risk ratio", "float", "Scalper Risk",
       min_=0.1, max_=20, step=0.1, bt=False, help_="1.5 = a 1:1.5 R:R scalp."),
    _s("SCALP_COST_BUFFER_MULT", "Cost buffer ×", "float", "Scalper Risk",
       min_=1.0, max_=20, step=0.1, bt=False,
       help_="Gross P&L at target must exceed round-trip costs × this, or the "
             "trade is refused as cost-dominated."),
    _s("SCALP_MAX_CONCURRENT_POSITIONS", "Max open scalps", "int", "Scalper Risk",
       min_=1, max_=20, bt=False,
       help_="Counted over scalp positions only — total open positions are "
             "bounded by this PLUS the core strategy's own cap."),
    _s("SCALP_MAX_TRADES_PER_SYMBOL", "Max trades per symbol", "int", "Scalper Risk",
       min_=1, max_=50, bt=False,
       help_="Unlike the core strategy, a scalp symbol may be re-entered; this "
             "caps the churn."),
    _s("SCALP_MAX_TRADES_PER_DAY", "Max scalps per day", "int", "Scalper Risk",
       min_=1, max_=500, bt=False),
    _s("SCALP_REENTRY_COOLDOWN_S", "Re-entry cooldown (s)", "float", "Scalper Risk",
       min_=0, max_=3600, step=5, bt=False),
    _s("SCALP_DAILY_LOSS_LIMIT", "Scalp daily loss limit ₹", "float", "Scalper Risk",
       min_=100, max_=10_000_000, step=100, bt=False,
       help_="Applies to realized scalp P&L only; the account-wide daily loss "
             "limit also stops the scalper."),
    _s("SCALP_MAX_HOLD_S", "Max hold (s) — time stop", "float", "Scalper Risk",
       min_=10, max_=7200, step=10, bt=False,
       help_="A scalp that hasn't reached target or stop by then is flattened; "
             "dead trades tie up capital and margin."),

    # ── Delivery mode (positional backtests: mode="delivery" and "1d", which is
    # always positional) — independent stop/target/risk/leverage/toggle profile
    # for overnight holds. Shadows the plain keys only inside a positional
    # replay (app.backtest.engine._delivery_overrides); live and intraday
    # backtests never read these. ──────────────────────────────────────────────
    _s("DELIVERY_MIN_SL_OFFSET", "Min stop distance ₹ (delivery)", "float", "Delivery Mode",
       min_=0.5, max_=10_000, step=0.5, help_="Structural stop floor for overnight/multi-day holds."),
    _s("DELIVERY_SL_PCT", "Stop-loss % of entry (delivery)", "float", "Delivery Mode",
       min_=0, max_=99, step=0.1,
       help_="A true percentage: 10 = stop 10% below entry — REQUIRED for "
             "capital_pct risk to reach its full % at 1× leverage (a ₹-fixed "
             "stop is a tiny fraction of a high-priced stock). 0 = swing-low stop."),
    _s("DELIVERY_RR_RATIO", "Reward : risk ratio (delivery)", "float", "Delivery Mode",
       min_=0.1, max_=100, step=0.1),
    _s("DELIVERY_RISK_MODE", "Risk basis (delivery)", "choice", "Delivery Mode",
       choices=cfg.RISK_MODES,
       help_="fixed_amount = risk ₹X per trade · capital_pct = risk X% of "
             "capital per trade (stop stays at the swing low)."),
    _s("DELIVERY_RISK_PER_TRADE", "Risk per trade ₹ (delivery)", "float", "Delivery Mode",
       min_=1, max_=100_000_000, step=50),
    _s("DELIVERY_RISK_CAPITAL_PERCENT", "Risk % of capital (delivery)", "float", "Delivery Mode",
       min_=0.1, max_=100, step=0.1,
       help_="A true percentage: 10 = a stop-out loses 10% of capital "
             "(₹10 per ₹100). Used when Risk basis = capital_pct."),
    _s("DELIVERY_MAX_CONCURRENT_POSITIONS", "Max open positions (delivery)", "int", "Delivery Mode",
       min_=1, max_=20),
    _s("DELIVERY_DAILY_LOSS_LIMIT", "Run loss limit ₹ (delivery)", "float", "Delivery Mode",
       min_=100, max_=10_000_000, step=100,
       help_="Run-level loss stop — positional mode has no daily reset."),
    _s("DELIVERY_LEVERAGE", "Leverage × (delivery)", "int", "Delivery Mode",
       min_=1, max_=10, help_="CNC/delivery margin — usually 1x, unlike intraday's 5x."),

    _s("DELIVERY_COND_NEAR_SUPPORT", "Near support (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_COND_BULLISH_PATTERN", "Bullish candle pattern (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_COND_ADX", "ADX trend strength (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_COND_RSI", "RSI ok (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_COND_MACD_CROSS", "MACD bullish cross (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_COND_VOLUME_SURGE", "Volume surge (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_COND_ABOVE_VWAP", "Price above VWAP (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_GATE_STOCK_DAILY", "Stock daily green (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_GATE_STOCK_HOURLY", "Stock hourly green (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_GATE_NIFTY_DAILY", "NIFTY daily green (delivery)", "bool", "Delivery Mode"),
    _s("DELIVERY_GATE_NIFTY_VWAP", "NIFTY above VWAP (delivery)", "bool", "Delivery Mode"),

    _s("DELIVERY_COST_BROKERAGE_PCT", "Brokerage % (delivery)", "float", "Delivery Mode",
       min_=0, max_=0.01, step=0.0001, help_="CNC brokerage — 0 at discount brokers."),
    _s("DELIVERY_COST_BROKERAGE_CAP", "Brokerage cap ₹/order (delivery)", "float", "Delivery Mode",
       min_=0, max_=1000, step=1, help_="Per-order brokerage cap for delivery — the intraday "
       "cap otherwise silently under-caps a large delivery position's brokerage."),
    _s("DELIVERY_COST_STT", "STT (delivery, both legs)", "float", "Delivery Mode",
       min_=0, max_=0.01, step=0.0001,
       help_="Delivery STT is 0.1% on BOTH buy and sell (intraday: 0.025% sell only)."),
    _s("DELIVERY_COST_STAMP", "Stamp duty (delivery)", "float", "Delivery Mode",
       min_=0, max_=0.01, step=0.00001),
    _s("DELIVERY_COST_DP", "DP charge ₹/sell (delivery)", "float", "Delivery Mode",
       min_=0, max_=100, step=0.01, help_="Flat depository charge per sell."),

    # ── Backtest & costs ──────────────────────────────────────────────────────
    _s("BACKTEST_TIMEFRAME", "Backtest timeframe", "choice", "Backtest & Costs",
       choices=cfg.BACKTEST_TIMEFRAMES,
       help_="Bar interval a backtest replays (intraday only; per-run overridable on the form)."),
    _s("BACKTEST_MODE", "Backtest mode", "choice", "Backtest & Costs",
       choices=cfg.BACKTEST_MODES,
       help_="intraday = EOD square-off, days independent; delivery = positional "
             "(overnight holds, square-off at range end). 1d bars are always positional."),
    _s("BACKTEST_WARMUP_DAYS", "Backtest warmup days", "int", "Backtest & Costs",
       min_=3, max_=30),
    _s("SLIPPAGE_BPS", "Slippage (bps)", "float", "Backtest & Costs", min_=0, max_=100, step=0.5),
    _s("COST_BROKERAGE_PCT", "Brokerage % (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.0001),
    _s("COST_BROKERAGE_CAP", "Brokerage cap ₹/order", "float", "Backtest & Costs",
       min_=0, max_=100),
    _s("COST_STT_SELL", "STT sell-side (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00005),
    _s("COST_STT_BUY", "STT buy-side (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00005,
       help_="0 for intraday; delivery mode shadows this with DELIVERY_COST_STT."),
    _s("COST_DP_SELL", "DP charge ₹/sell", "float", "Backtest & Costs",
       min_=0, max_=100, step=0.01,
       help_="Flat per-sell depository charge — 0 for intraday."),
    _s("COST_TXN_CHARGE", "Exchange txn (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00001),
    _s("COST_GST", "GST (fraction)", "float", "Backtest & Costs", min_=0, max_=1, step=0.01),
    _s("COST_STAMP_BUY", "Stamp duty buy-side (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.01, step=0.00001),
    _s("COST_SEBI", "SEBI fee (fraction)", "float", "Backtest & Costs",
       min_=0, max_=0.001, step=0.000001),
]

_BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SPEC}
GROUP_ORDER = ["AI Pre-market Screen", "Session Timings", "Risk & Capital",
               "Strategy", "Entry Conditions", "Trend Gates", "Engine",
               "Scalper", "Scalper Filters", "Scalper Risk",
               "Delivery Mode", "Backtest & Costs"]

# cfg-attr key → (spec, role) where role is "value" | "hour" | "min" — lets the
# loader validate raw stored attrs (incl. expanded time parts) one by one.
_ATTR_SPEC: Dict[str, tuple] = {}
for _spec in SPEC:
    if _spec["type"] == "time":
        _ATTR_SPEC[_spec["parts"][0]] = (_spec, "hour")
        _ATTR_SPEC[_spec["parts"][1]] = (_spec, "min")
    else:
        _ATTR_SPEC[_spec["key"]] = (_spec, "value")

# Import-time consistency check: every SPEC entry must map to a real dynamic
# config default and every default must be editable — catches the "added a
# tunable in only one place" drift at startup instead of as a silent bug.
_defaults_keys = set(cfg.dynamic_defaults())
_spec_attr_keys = set(_ATTR_SPEC)
if _spec_attr_keys != _defaults_keys:
    raise RuntimeError(
        "settings SPEC / config._DEFAULTS drift — "
        f"missing from SPEC: {sorted(_defaults_keys - _spec_attr_keys)}, "
        f"unknown in SPEC: {sorted(_spec_attr_keys - _defaults_keys)}"
    )

# Session times must stay ordered or the phase driver / backtest window breaks.
_TIME_ORDER = ("PREMARKET", "MARKET_OPEN", "SCAN_START", "CUTOFF", "SESSION_END")
# The scalper's windows form their OWN independent chain (see
# app/engine/scalper._WINDOWS): its profile picks the last window whose start
# time has passed, so an out-of-order boundary would make a window unreachable —
# e.g. a midday start before the morning start silently deletes the morning
# window instead of erroring.
_SCALP_TIME_ORDER = ("SCALP_WARMUP", "SCALP_MORNING", "SCALP_MIDDAY",
                     "SCALP_AFTERNOON", "SCALP_SQUAREOFF")
_TIME_CHAINS = (_TIME_ORDER, _SCALP_TIME_ORDER)
_TIME_LABEL = {"PREMARKET": "pre-market", "MARKET_OPEN": "market open",
               "SCAN_START": "scan start", "CUTOFF": "entry cutoff",
               "SESSION_END": "session end",
               "SCALP_WARMUP": "scalp warm-up", "SCALP_MORNING": "scalp morning",
               "SCALP_MIDDAY": "scalp midday", "SCALP_AFTERNOON": "scalp afternoon",
               "SCALP_SQUAREOFF": "scalp square-off"}
# Points that must be STRICTLY before the next one — a zero-width window is
# useless (scan start == cutoff never scans; afternoon == square-off never trades).
_STRICT_BEFORE_NEXT = frozenset({"SCAN_START", "SCALP_AFTERNOON"})


# ── Value coercion / validation ───────────────────────────────────────────────

def _coerce(spec: Dict[str, Any], raw: Any) -> Any:
    key, typ = spec["key"], spec["type"]
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        if raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise ValueError(f"{key}: expected true/false")

    if typ == "str":
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{key}: expected a non-empty string")
        return raw.strip()

    if typ == "choice":
        val = raw.strip() if isinstance(raw, str) else raw
        if val not in (spec["choices"] or []):
            raise ValueError(f"{key}: must be one of {spec['choices']}")
        return val

    if typ == "rules":
        # Structural whitelist validation lives beside the evaluator so the
        # two can't drift. (Import here: settings→conditions has no cycle.)
        from app.engine.conditions import validate_rules
        return validate_rules(raw)

    if typ == "time":
        if not isinstance(raw, str) or not _TIME_RE.match(raw.strip()):
            raise ValueError(f"{key}: expected \"HH:MM\" (24h)")
        h, m = raw.strip().split(":")
        return int(h), int(m)

    # int / float — reject bools explicitly: float(True) == 1.0, so a client
    # type mix-up (true sent for a numeric key) would otherwise silently set
    # the value to 1/0 (e.g. INTRADAY_LEVERAGE: true → 1× leverage, in-bounds).
    if isinstance(raw, bool):
        raise ValueError(f"{key}: expected a number")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: expected a number") from None
    if typ == "int":
        if val != int(val):
            raise ValueError(f"{key}: expected an integer")
        val = int(val)
    if spec["min"] is not None and val < spec["min"]:
        raise ValueError(f"{key}: must be ≥ {spec['min']}")
    if spec["max"] is not None and val > spec["max"]:
        raise ValueError(f"{key}: must be ≤ {spec['max']}")
    return val


def expand_changes(changes: Dict[str, Any], *, bt_only: bool = False) -> Dict[str, Any]:
    """
    Validate {spec_key: value} user input and return {cfg_attr: value},
    expanding virtual time settings into their HOUR/MIN pairs.
    Raises ValueError with a user-facing message on any bad key/value.
    """
    out: Dict[str, Any] = {}
    for key, raw in changes.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"unknown setting: {key}")
        if bt_only and not spec["bt"]:
            raise ValueError(f"{key} cannot be overridden per backtest run")
        val = _coerce(spec, raw)
        if spec["type"] == "time":
            out[spec["parts"][0]], out[spec["parts"][1]] = val
        else:
            out[key] = val
    return out


# cfg attrs whose value affects an indicator's minimum-bar requirement — the
# self-heal and reset guards drop/validate this whole set together.
INDICATOR_PERIOD_KEYS = ("MACD_FAST", "MACD_SLOW", "MACD_SIGNAL", "MACD_CROSS_BARS",
                         "RSI_PERIOD", "RSI_RISING_BARS", "ADX_PERIOD", "TALIB_LOOKBACK")


def validate_indicator_periods(attr_changes: Dict[str, Any]) -> None:
    """
    Cross-field guards so a period/lookback combo can't leave an indicator
    all-NaN — which (with its condition enabled) silently blocks EVERY entry in
    both live and backtest, with no error. Checked against the effective config
    (changes over current). No-op unless a relevant key changed.

      • MACD: fast < slow (fast≥slow → TA-Lib NaN), and lookback ≥ slow + signal
        + cross-window (MACD needs slow+signal-1 bars for a first value).
      • ADX:  lookback ≥ 2·period + 1 (ADX converges around 2·period bars).
      • RSI:  lookback ≥ period + rising-bars + 1 (need that many valid values
        to test "rose N bars").
    """
    if not any(k in attr_changes for k in INDICATOR_PERIOD_KEYS):
        return

    def eff(k: str) -> int:
        return attr_changes.get(k, getattr(cfg, k))

    lookback = eff("TALIB_LOOKBACK")
    fast, slow, signal = eff("MACD_FAST"), eff("MACD_SLOW"), eff("MACD_SIGNAL")
    if fast >= slow:
        raise ValueError(
            f"MACD fast period ({fast}) must be less than the slow period ({slow})")

    need = {
        "MACD": slow + signal + eff("MACD_CROSS_BARS"),
        "ADX":  2 * eff("ADX_PERIOD") + 1,
        "RSI":  eff("RSI_PERIOD") + eff("RSI_RISING_BARS") + 1,
    }
    worst = max(need, key=need.get)
    if lookback < need[worst]:
        raise ValueError(
            f"indicator lookback ({lookback}) is too small — {worst} needs "
            f"≥ {need[worst]} bars; raise TALIB_LOOKBACK or lower the period(s)")


# Backwards-compatible alias (older call sites / tests).
validate_macd_periods = validate_indicator_periods


def validate_obi_weights(attr_changes: Dict[str, Any]) -> None:
    """
    Validate the scalper's level-weight vector at SAVE time.

    orderbook.parse_weights deliberately falls back to the defaults on anything
    unparseable (a bad value must never crash the tick loop), which means a typo
    would otherwise be silently ignored — the user would see their weights
    "saved" while the engine used 1.0,0.8,0.6,0.4,0.2. This is where the typo
    gets reported instead. No-op unless the key changed.
    """
    if "SCALP_OBI_WEIGHTS" not in attr_changes:
        return
    raw   = attr_changes["SCALP_OBI_WEIGHTS"]
    parts = [p for p in str(raw).replace(" ", "").split(",") if p != ""]
    if not parts or len(parts) > 5:
        raise ValueError("Level weights: expected 1–5 comma-separated numbers "
                         "(e.g. 1.0,0.8,0.6,0.4,0.2)")
    vals = []
    for p in parts:
        try:
            vals.append(float(p))
        except ValueError:
            raise ValueError(f"Level weights: {p!r} is not a number") from None
    if any(v < 0 for v in vals):
        raise ValueError("Level weights: must be non-negative")
    if not any(v > 0 for v in vals):
        # All-zero weights make both weighted depths 0, so the ratio is
        # undefined and the scalper could never fire a single signal.
        raise ValueError("Level weights: at least one weight must be > 0")


def _coerce_attr(key: str, raw: Any) -> Any:
    """
    Validate one raw cfg-attr value (as stored in the DB) against its SPEC.
    Time settings are stored expanded as *_HOUR/*_MIN ints, so they are
    validated through _coerce with a synthetic int spec (one validation path,
    consistent error messages) instead of the "HH:MM" string coercion.
    """
    spec, role = _ATTR_SPEC[key]
    if role == "value":
        return _coerce(spec, raw)
    hi = 23 if role == "hour" else 59
    return _coerce({"key": key, "type": "int", "min": 0, "max": hi}, raw)


def time_attr_keys(chain: tuple) -> List[str]:
    """The cfg attrs backing one ordered time chain (for targeted self-healing)."""
    return [f"{p}_{part}" for p in chain for part in ("HOUR", "MIN")]


def _validate_chain(attr_changes: Dict[str, Any], points: tuple) -> None:
    if not any(k in attr_changes for p in points
               for k in (f"{p}_HOUR", f"{p}_MIN")):
        return

    def eff(attr: str) -> int:
        return attr_changes.get(attr, getattr(cfg, attr))

    minutes = [eff(f"{p}_HOUR") * 60 + eff(f"{p}_MIN") for p in points]
    for i in range(len(points) - 1):
        strict = points[i] in _STRICT_BEFORE_NEXT   # zero-width window is useless
        if minutes[i] > minutes[i + 1] or (strict and minutes[i] == minutes[i + 1]):
            raise ValueError(
                f"session times out of order: {_TIME_LABEL[points[i]]} must be "
                f"{'before' if strict else 'at or before'} {_TIME_LABEL[points[i + 1]]}"
            )


def validate_time_order(attr_changes: Dict[str, Any],
                        points: Optional[tuple] = None) -> None:
    """
    Cross-field guard: with `attr_changes` applied on top of the current config,
    each ordered time chain must stay ordered.

      • the live session chain enforces
            premarket ≤ market open ≤ scan start < cutoff ≤ session end
      • the scalper chain enforces
            warm-up ≤ morning ≤ midday ≤ afternoon < square-off

    `points=None` (the default, used by save / startup-load / reset) validates
    EVERY chain; passing an explicit chain validates just that one — the backtest
    passes points=("SCAN_START","CUTOFF") since those are the only times a replay
    uses, and comparing against live-only settings would falsely reject valid
    runs. No-op for a chain none of whose points changed. Raises ValueError
    naming the violated pair.
    """
    for chain in (_TIME_CHAINS if points is None else (points,)):
        _validate_chain(attr_changes, chain)


def _attr_keys(spec: Dict[str, Any]) -> List[str]:
    return list(spec["parts"]) if spec["type"] == "time" else [spec["key"]]


def _read_value(spec: Dict[str, Any], source: Dict[str, Any]) -> Any:
    if spec["type"] == "time":
        h, m = spec["parts"]
        return f"{source[h]:02d}:{source[m]:02d}"
    return source[spec["key"]]


# ── Introspection for GET /api/settings ───────────────────────────────────────

def describe() -> Dict[str, Any]:
    from app.engine.conditions import RULE_FIELDS, RULE_OPS   # no import cycle
    defaults = cfg.dynamic_defaults()
    current = {k: getattr(cfg, k) for k in defaults}
    groups: Dict[str, list] = {g: [] for g in GROUP_ORDER}
    for spec in SPEC:
        value   = _read_value(spec, current)
        default = _read_value(spec, defaults)
        entry = {
            "key":        spec["key"],
            "label":      spec["label"],
            "type":       spec["type"],
            "help":       spec["help"],
            "min":        spec["min"],
            "max":        spec["max"],
            "step":       spec["step"],
            "choices":    spec["choices"],
            "cond":       spec["cond"],
            "bt":         spec["bt"],
            "value":      value,
            "default":    default,
            "overridden": value != default,
        }
        if spec["type"] == "rules":
            # Ship the builder's vocabulary with the setting so the UI can
            # render field/op pickers without a second endpoint.
            entry["fields"] = [{"key": k, "label": lbl, "kind": kind}
                               for k, (lbl, kind, _) in RULE_FIELDS.items()]
            entry["ops"] = list(RULE_OPS)
        groups.setdefault(spec["group"], []).append(entry)
    return {"groups": [{"name": g, "settings": groups[g]}
                       for g in GROUP_ORDER if groups.get(g)]}


# ── Persistence glue ──────────────────────────────────────────────────────────

async def load_and_apply(db) -> None:
    """
    Startup: apply stored overrides from the app_settings table. Every value
    is re-validated against SPEC — a corrupt/out-of-range row (manual edit,
    schema drift) is skipped with a warning instead of poisoning the engine
    (e.g. PREMARKET_HOUR=99 would crash the phase driver's time math).
    """
    try:
        stored = await db.get_app_settings()
    except Exception as e:
        print(f"Settings load failed (using defaults): {e}")
        return
    valid: Dict[str, Any] = {}
    for k, v in stored.items():
        if k.startswith(INTERNAL_PREFIX) or k not in _ATTR_SPEC:
            continue
        try:
            valid[k] = _coerce_attr(k, v)
        except (ValueError, TypeError) as e:
            print(f"Settings: ignoring invalid stored override {k}={v!r} ({e})")

    # Cross-field self-heal: individually-valid rows can still form an
    # inverted session-time chain (partial manual edit / historical bug).
    # Fall back to the DEFAULT times rather than brick the trading day. Each
    # chain heals INDEPENDENTLY — a bad scalper window must not also reset the
    # live session's timings (or vice versa).
    for chain in _TIME_CHAINS:
        try:
            validate_time_order(valid, chain)
        except ValueError as e:
            dropped = [k for k in time_attr_keys(chain) if k in valid]
            for k in dropped:
                valid.pop(k, None)
            print(f"Settings: stored session times invalid ({e}) — "
                  f"dropped {dropped}, using defaults for that chain")

    # Same self-heal for an unparseable stored weight vector.
    try:
        validate_obi_weights(valid)
    except ValueError as e:
        valid.pop("SCALP_OBI_WEIGHTS", None)
        print(f"Settings: stored scalp level weights invalid ({e}) — using default")

    # Same self-heal for a stored indicator period/lookback combo that would
    # leave an indicator all-NaN. Drop ALL indicator-period overrides back to
    # defaults (which are internally consistent), not just the MACD ones.
    try:
        validate_indicator_periods(valid)
    except ValueError as e:
        dropped = [k for k in INDICATOR_PERIOD_KEYS if k in valid]
        for k in dropped:
            valid.pop(k, None)
        print(f"Settings: stored indicator periods invalid ({e}) — "
              f"dropped {dropped}, using defaults")

    if valid:
        cfg.set_runtime_overrides(valid)
        print(f"Settings: applied {len(valid)} stored overrides")


async def apply_and_persist(db, changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate + apply {spec_key: value} changes, persist overrides, and drop
    stored rows for values set back to their default (so future default
    changes in code flow through). Returns the fresh describe() payload.
    """
    attr_changes = expand_changes(changes)
    validate_time_order(attr_changes)
    validate_indicator_periods(attr_changes)
    validate_obi_weights(attr_changes)

    defaults   = cfg.dynamic_defaults()
    store      = {k: v for k, v in attr_changes.items() if v != defaults[k]}
    at_default = [k for k, v in attr_changes.items() if v == defaults[k]]

    # Persist FIRST (atomically — upsert + delete in one transaction), then
    # apply. Whatever the outcome, live behavior matches what a restart
    # would restore: DB failure → nothing persisted, nothing applied.
    await db.replace_app_settings(store, at_default)
    cfg.set_runtime_overrides(store)
    cfg.clear_runtime_overrides(at_default)

    # Inert-field warnings: saving a risk value whose Risk basis makes it a
    # no-op is the classic silent trap ("I set 10% but the backtest ignores
    # it") — say so in the save confirmation instead of letting it pass.
    warnings: List[str] = []
    if "RISK_CAPITAL_PERCENT" in changes and cfg.RISK_MODE != "capital_pct":
        warnings.append("Risk % of capital has NO effect until Risk basis = capital_pct")
    if "RISK_PER_TRADE" in changes and cfg.RISK_MODE != "fixed_amount":
        warnings.append("Risk per trade ₹ has NO effect while Risk basis = capital_pct")
    if "DELIVERY_RISK_CAPITAL_PERCENT" in changes and cfg.DELIVERY_RISK_MODE != "capital_pct":
        warnings.append("Delivery Risk % has NO effect until Risk basis (delivery) = capital_pct")
    if "DELIVERY_RISK_PER_TRADE" in changes and cfg.DELIVERY_RISK_MODE != "fixed_amount":
        warnings.append("Delivery Risk ₹ has NO effect while Risk basis (delivery) = capital_pct")
    if "SCALP_RISK_CAPITAL_PERCENT" in changes and cfg.SCALP_RISK_MODE != "capital_pct":
        warnings.append("Scalp Risk % has NO effect until Scalper Risk basis = capital_pct")
    if "SCALP_RISK_PER_TRADE" in changes and cfg.SCALP_RISK_MODE != "fixed_amount":
        warnings.append("Scalp Risk ₹ has NO effect while Scalper Risk basis = capital_pct")
    # Arming the scalper is the one change here that starts placing orders — say
    # so plainly in the save confirmation rather than let it pass silently.
    if ("SCALP_ENABLED" in changes or "SCALP_DRY_RUN" in changes):
        if cfg.SCALP_ENABLED and not cfg.SCALP_DRY_RUN:
            warnings.append("Scalper is now ARMED — it will place paper orders "
                            "on live order-book signals")
        elif cfg.SCALP_ENABLED:
            warnings.append("Scalper enabled in DRY RUN — signals are logged, "
                            "no orders are placed")

    out = describe()
    if warnings:
        out["warnings"] = warnings
    return out


async def reset(db, keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """Reset the given spec keys (or ALL settings) to defaults."""
    if keys is None:
        attr_keys = [k for s in SPEC for k in _attr_keys(s)]
    else:
        attr_keys = []
        for key in keys:
            spec = _BY_KEY.get(key)
            if spec is None:
                raise ValueError(f"unknown setting: {key}")
            attr_keys.extend(_attr_keys(spec))

    # A PARTIAL reset must honor the same cross-field guards as a save: e.g.
    # resetting only CUTOFF back to default while SCAN_START stays overridden
    # could invert the window, or resetting one MACD/period key could leave an
    # indicator all-NaN. Model the post-reset state as {reset key: default} over
    # current config and run both guards (they no-op if no relevant key touched).
    # (A full reset is always valid — defaults are internally consistent.)
    defaults = cfg.dynamic_defaults()
    post_reset = {k: defaults[k] for k in attr_keys}
    validate_time_order(post_reset)
    validate_indicator_periods(post_reset)
    validate_obi_weights(post_reset)

    await db.delete_app_settings(attr_keys)
    cfg.clear_runtime_overrides(attr_keys)
    return describe()
