"""
Throwaway dev smoke test — NOT shipped functionality.

Fetches real historical 5m data for BankNifty + the 11 BN stocks over a
recent window and runs it through the backtest replay (the same
evaluate_entry/evaluate_exit pure functions the live scheduler calls),
printing every fired signal and a plausibility summary. Run manually:

    python3 scripts/bn_smoke_test.py [days_back]

No Postgres needed: `load_backtest_data` normally reads BankNifty history
from OUR self-recorded `bn_index_bars` table (see database.py) — the market
server itself has no historical archive for the index, only "today" (see
scheduler.py's notes). This script substitutes a stub with no DB dependency
that just re-fetches "today" directly, so it only ever exercises ONE real
day until the actual app has been running long enough to have recorded more.
"""
import asyncio
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

import app.config as cfg
from app.backtest.data import load_backtest_data
from app.backtest.engine import simulate
from app.services.historical_data import _fetch_all


class _StubDB:
    """Stands in for the real self-recorded archive — see module docstring."""
    async def get_bn_index_bars(self, from_iso: str, to_iso: str):
        raw = await _fetch_all(
            [{"stockname": cfg.BN_INDEX_NAME, "stock_symbol": cfg.BN_INDEX_TOKEN}],
            [cfg.INTERVAL_5M], from_iso, to_iso,
        )
        return raw.get(cfg.BN_INDEX_TOKEN, {}).get(cfg.INTERVAL_5M, [])


async def main() -> None:
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    to_d = date.today()
    from_d = to_d - timedelta(days=days_back)

    print(f"Fetching BankNifty + {len(cfg.BN_ALL_STOCKS)} stocks, {from_d} .. {to_d} ...")
    bn_index, stocks = await load_backtest_data(_StubDB(), from_d, to_d)
    if bn_index is None:
        print("No BankNifty data returned — aborting.")
        return
    print(f"BankNifty bars: {len(bn_index.series)} | stocks loaded: {len(stocks)}/{len(cfg.BN_ALL_STOCKS)}")
    missing = set(cfg.BN_ALL_STOCKS) - {stocks[t].name for t in stocks if t in stocks}
    if missing:
        print(f"WARNING missing stock data for: {sorted(missing)}")

    trades, equity, ndays = simulate(bn_index, stocks, from_d, to_d, cfg.SLIPPAGE_BPS)
    print(f"\nDays replayed: {ndays}")
    print(f"Trades fired: {len(trades)}  ({len(trades)/max(ndays,1):.2f}/day)\n")

    for t in trades:
        print(f"  {t.entry_time} {t.direction:4s} {t.option_type} {t.strike:>6} "
              f"entry_prem={t.entry_premium:7.2f} exit_prem={t.exit_premium:7.2f} "
              f"outcome={t.outcome:6s} net=₹{t.net_pnl:+8.2f} iv={t.iv_used}")

    if trades:
        net = sum(t.net_pnl for t in trades)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        print(f"\nTotal net P&L: ₹{net:+.2f} | Win rate: {wins}/{len(trades)} "
              f"({wins/len(trades)*100:.1f}%)")
        ivs = [t.iv_used for t in trades if t.iv_used is not None]
        if ivs:
            print(f"IV range used: {min(ivs):.3f} .. {max(ivs):.3f} "
                  f"(bounds: [{cfg.BN_IV_MIN}, {cfg.BN_IV_MAX}])")
        stages = {}
        # sl_stage isn't persisted on BTTrade — outcome/stop_loss vs target tells us TARGET/STOP/EOD split
        for t in trades:
            stages[t.outcome] = stages.get(t.outcome, 0) + 1
        print(f"Outcome breakdown: {stages}")
    else:
        print("No trades fired — check gate thresholds / date range (or genuinely a quiet period).")


if __name__ == "__main__":
    asyncio.run(main())
