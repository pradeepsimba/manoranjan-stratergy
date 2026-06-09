package org.example.hellofx;

import java.time.LocalDateTime;
import java.time.LocalTime;
import java.util.*;

/**
 * Implements the full entry/exit trading logic from the HTML reference.
 */
public class TradeEngine {

    private final AppState   state = AppState.get();
    private Runnable onTradeChanged;
    private Runnable onDashboardRefresh;

    public TradeEngine(Runnable onTradeChanged, Runnable onDashboardRefresh) {
        this.onTradeChanged    = onTradeChanged;
        this.onDashboardRefresh = onDashboardRefresh;
    }

    // ── Market hours ──────────────────────────────────────────────────────────

    public boolean isMarketOpen() {
        LocalTime now = nowIST().toLocalTime();
        LocalTime open  = LocalTime.of(AppConfig.MARKET_OPEN_HOUR,  AppConfig.MARKET_OPEN_MIN);
        LocalTime close = LocalTime.of(AppConfig.MARKET_CLOSE_HOUR, AppConfig.MARKET_CLOSE_MIN);
        return !now.isBefore(open) && !now.isAfter(close);
    }

    public boolean isInTimeWindow() {
        LocalTime now   = nowIST().toLocalTime();
        LocalTime start = LocalTime.of(AppConfig.ENTRY_START_HOUR, AppConfig.ENTRY_START_MIN);
        LocalTime end   = LocalTime.of(AppConfig.ENTRY_END_HOUR,   AppConfig.ENTRY_END_MIN);
        return !now.isBefore(start) && now.isBefore(end);
    }

    static LocalDateTime nowIST() {
        return LocalDateTime.now(java.time.ZoneId.of("Asia/Kolkata"));
    }

    // ── Leader momentum ───────────────────────────────────────────────────────

    public LeaderResult leadersMomentum() {
        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName)).findFirst().orElse(null);
            if (stock == null) continue;
            List<Candle> candles = state.lastNCandles.get(stock.symbol());
            if (candles == null || candles.isEmpty()) {
                return new LeaderResult("Nobuysell", "Leader candle missing: " + stockName);
            }
        }

        List<String> buyLeaders  = new ArrayList<>();
        List<String> sellLeaders = new ArrayList<>();

        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName)).findFirst().orElse(null);
            if (stock == null) continue;
            List<Candle> candles = state.lastNCandles.get(stock.symbol());
            if (candles == null || candles.isEmpty()) continue;
            Candle c = candles.get(candles.size() - 1);
            if (c.close > c.open) buyLeaders.add(stockName);
            else if (c.close < c.open) sellLeaders.add(stockName);
        }

        if (buyLeaders.size() >= AppConfig.SAME_DIRECTION_REQUIRED) {
            String reason = String.join(" + ", buyLeaders.subList(0, AppConfig.SAME_DIRECTION_REQUIRED));
            return new LeaderResult("BUY", reason + " aligned");
        }
        if (sellLeaders.size() >= AppConfig.SAME_DIRECTION_REQUIRED) {
            String reason = String.join(" + ", sellLeaders.subList(0, AppConfig.SAME_DIRECTION_REQUIRED));
            return new LeaderResult("SELL", reason + " aligned");
        }
        return new LeaderResult("Nobuysell", "No leader match");
    }

    // ── Qty multiplier ────────────────────────────────────────────────────────

    public static double qtyMultiplier(String interval) {
        return switch (interval) {
            case "3m"  -> 1.5;
            case "5m"  -> 2.0;
            case "15m" -> 6.0;
            default    -> 1.0;
        };
    }

    public static int effectiveThreshold(String stock, String interval) {
        Integer base = AppConfig.STOCK_QTY_THRESHOLD.get(stock);
        if (base == null) return 9999;
        return (int)(base * qtyMultiplier(interval));
    }

    // ── Entry check ───────────────────────────────────────────────────────────

    public synchronized AppState.EntryDiagnostics checkTradeEntry() {

        String interval = state.selectedInterval;

        List<Candle> bnCandles = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        Candle bn = (bnCandles != null && !bnCandles.isEmpty())
            ? bnCandles.get(bnCandles.size() - 1) : null;

        boolean marketOpen    = isMarketOpen();
        boolean timeOk        = isInTimeWindow();
        boolean noActiveTrade = (state.activeTrade == null);
        long    cooldownMs    = System.currentTimeMillis() - state.lastExitTime;

        Double sideRange = IndicatorEngine.sidewaysRange(bnCandles);
        boolean sidewaysOk = sideRange != null && sideRange >= 12;

        IndicatorEngine.MomentumResult mom = IndicatorEngine.strongMomentum(bnCandles, interval);

        LeaderResult leaderSig = leadersMomentum();
        boolean sigOk = leaderSig.signal.equals("BUY") || leaderSig.signal.equals("SELL");

        // Count green/red/strongQty across 6 leader stocks
        int green = 0, red = 0, strongQty = 0;
        List<AppState.StockStat> stockStats = new ArrayList<>();
        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName)).findFirst().orElse(null);
            if (stock == null) continue;
            List<Candle> sc = state.lastNCandles.get(stock.symbol());
            Candle c = (sc != null && !sc.isEmpty()) ? sc.get(sc.size() - 1) : null;
            if (c == null) continue;
            if (c.close > c.open) green++;
            else if (c.close < c.open) red++;
            double qty       = state.latestMinuteQty.getOrDefault(stockName, 0.0);
            double threshold = effectiveThreshold(stockName, interval);
            if (qty >= threshold) strongQty++;
            stockStats.add(new AppState.StockStat(stockName, c, qty, threshold));
        }

        boolean dirOk  = Math.max(green, red) >= AppConfig.SAME_DIRECTION_REQUIRED;
        boolean sqOk   = strongQty >= AppConfig.SAME_DIRECTION_REQUIRED;

        // Candle close check
        boolean candleCloseOk = bn != null && isCandleClosed(bn.startTime, interval);
        boolean alreadyTraded = bn != null
            && bn.startTime.substring(0, 16).equals(state.lastTradeCandle);

        BNIndicators bnInd = IndicatorEngine.checkBNIndicators();
        state.bnIndicators = bnInd;

        boolean macdMet = bnInd.macdDir != null && !bnInd.macdDir.equals("—") && !bnInd.macdDir.equals("NEUTRAL");
        boolean emaMet  = bnInd.emaStack != null && (bnInd.emaStack.bullish || bnInd.emaStack.bearish);
        boolean gateOk  = bnInd.bullish || bnInd.bearish;

        AppState.EntryDiagnostics diag = new AppState.EntryDiagnostics(
            marketOpen, timeOk, noActiveTrade, cooldownMs, sideRange, candleCloseOk,
            leaderSig.signal, leaderSig.reason, green, red, strongQty,
            alreadyTraded, bnInd, bn, false, stockStats
        );
        state.entryDiagnostics = diag;

        // All conditions must pass
        boolean allOk = marketOpen && timeOk && noActiveTrade
            && cooldownMs >= 60_000 && sidewaysOk && mom.ok()
            && sigOk && dirOk && sqOk && candleCloseOk
            && !alreadyTraded && macdMet && emaMet && gateOk;

        // Set pending signal if all except candle close
        boolean allButClose = marketOpen && timeOk && noActiveTrade
            && cooldownMs >= 60_000 && sidewaysOk && mom.ok()
            && sigOk && dirOk && sqOk && !alreadyTraded && macdMet && emaMet && gateOk;
        if (allButClose && !candleCloseOk) {
            state.pendingSignal = new AppState.PendingSignal(leaderSig.signal, leaderSig.reason);
        } else if (allOk) {
            state.pendingSignal = null;
        } else {
            state.pendingSignal = null;
        }

        // Use the already-evaluated bnInd snapshot — no re-read that could race
        if (allOk && state.activeTrade == null) {
            if (bnInd.bullish && leaderSig.signal.equals("BUY")) {
                enterTrade("BUY",  bn.close, bn.startTime.substring(0, 16), "AUTO");
            } else if (bnInd.bearish && leaderSig.signal.equals("SELL")) {
                enterTrade("SELL", bn.close, bn.startTime.substring(0, 16), "AUTO");
            } else {
                System.out.printf("allOk=true but gate(%s)/leader(%s) mismatch — skipping entry%n",
                    bnInd.bullish ? "BULL" : bnInd.bearish ? "BEAR" : "NEUTRAL", leaderSig.signal);
            }
        }

        return diag;
    }

    private boolean isCandleClosed(String startTime, String interval) {
        if (startTime == null || startTime.isEmpty()) return false;
        try {
            LocalDateTime candleStart = LocalDateTime.parse(startTime.substring(0, 19),
                java.time.format.DateTimeFormatter.ISO_LOCAL_DATE_TIME);
            int mins = switch (interval) {
                case "3m"  -> 3;
                case "5m"  -> 5;
                case "15m" -> 15;
                default    -> 1;
            };
            // A candle is closed once the next candle has started (nowIST >= start + interval)
            LocalDateTime candleEnd = candleStart.plusMinutes(mins);
            return nowIST().isAfter(candleEnd);
        } catch (Exception e) { return false; }
    }

    // ── Enter trade ───────────────────────────────────────────────────────────

    public void enterTrade(String type, double price, String candleTime, String confidence) {
        if (state.activeTrade != null) return;
        state.activeTrade    = new ActiveTrade(type, price, candleTime, confidence);
        state.lastTradeCandle = candleTime;
        state.signalLocked   = true;

        Trade t = new Trade();
        t.type       = type;
        t.price      = price;
        t.time       = java.time.LocalDateTime.now().format(
                           java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        t.confidence = confidence;
        t.pnl        = 0;
        DatabaseService.saveTrade(t);

        // Start ATM tracking
        OptionsEngine.startATMTracking(type, price);

        System.out.printf("ENTRY %s @ %.2f [%s] conf=%s%n", type, price, candleTime, confidence);
        if (onTradeChanged != null) onTradeChanged.run();
    }

    // ── Exit check ────────────────────────────────────────────────────────────

    public void checkExit() {
        if (state.activeTrade == null) return;
        List<Candle> bnCandles = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        if (bnCandles == null || bnCandles.isEmpty()) return;
        Candle bn = bnCandles.get(bnCandles.size() - 1);
        double price = bn.close;
        ActiveTrade at = state.activeTrade;

        double pnl;
        if (at.type.equals("BUY")) {
            pnl = price - at.entry;
            double targetPrice = at.entry + AppConfig.TARGET;
            if (pnl >= AppConfig.TRAIL_TRIGGER) {
                double trailSL = price - AppConfig.TRAIL_DISTANCE;
                at.currentSL = Math.max(at.currentSL, trailSL);
            } else if (pnl >= AppConfig.BREAKEVEN_TRIGGER) {
                at.currentSL = Math.max(at.currentSL, at.entry);
            }
            if (price >= targetPrice || price <= at.currentSL) exitTrade(price, pnl);
        } else {
            pnl = at.entry - price;
            double targetPrice = at.entry - AppConfig.TARGET;
            if (pnl >= AppConfig.TRAIL_TRIGGER) {
                double trailSL = price + AppConfig.TRAIL_DISTANCE;
                at.currentSL = Math.min(at.currentSL, trailSL);
            } else if (pnl >= AppConfig.BREAKEVEN_TRIGGER) {
                at.currentSL = Math.min(at.currentSL, at.entry);
            }
            if (price <= targetPrice || price >= at.currentSL) exitTrade(price, pnl);
        }
    }

    public void exitTrade(double exitPrice, double pnl) {
        if (state.activeTrade == null) return;
        pnl = Math.round(pnl * 100.0) / 100.0;

        Trade t = new Trade();
        t.type          = state.activeTrade.type + "_EXIT";
        t.price         = exitPrice;
        t.time          = java.time.LocalDateTime.now().format(
                              java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        t.confidence    = state.activeTrade.confidence;
        t.pnl           = pnl;
        AtmOption opt   = state.atmOption;
        t.optionPremium = opt != null ? opt.currentPremium : null;
        DatabaseService.saveTrade(t);

        // Update funds
        if (opt != null) {
            state.availableFunds += opt.pnlRs();
        } else {
            state.availableFunds += pnl;
        }

        state.activeTrade = null;
        state.lastExitTime = System.currentTimeMillis();
        state.signalLocked = false;
        state.pendingSignal = null;
        OptionsEngine.stopATMTracking();

        System.out.printf("EXIT @ %.2f PnL=%.2f%n", exitPrice, pnl);
        if (onTradeChanged != null) onTradeChanged.run();
    }

    // ── Manual entry/exit ─────────────────────────────────────────────────────

    public void manualEntry(String type, double price) {
        if (state.activeTrade != null) return;
        String time = LocalDateTime.now().toString();
        enterTrade(type, price, time.substring(0, 16), "MANUAL");
    }

    public void manualExit() {
        if (state.activeTrade == null) return;
        List<Candle> bnCandles = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        if (bnCandles == null || bnCandles.isEmpty()) return;
        double price = bnCandles.get(bnCandles.size() - 1).close;
        double pnl   = state.activeTrade.type.equals("BUY")
            ? price - state.activeTrade.entry
            : state.activeTrade.entry - price;
        exitTrade(price, pnl);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    public record LeaderResult(String signal, String reason) {}
}
