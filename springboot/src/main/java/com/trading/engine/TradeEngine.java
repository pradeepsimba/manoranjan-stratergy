package com.trading.engine;

import com.trading.config.AppConfig;
import com.trading.model.*;
import com.trading.service.DatabaseService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.*;

@Service
public class TradeEngine {

    private final AppState state = AppState.get();

    @Autowired
    private DatabaseService dbService;

    // ── Market hours ──────────────────────────────────────────────────────────────

    public boolean isMarketOpen() {
        LocalTime now   = nowIST().toLocalTime();
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

    public static LocalDateTime nowIST() {
        return LocalDateTime.now(ZoneId.of("Asia/Kolkata"));
    }

    // ── Leader momentum ───────────────────────────────────────────────────────────

    public LeaderResult leadersMomentum() {
        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName)).findFirst().orElse(null);
            if (stock == null) continue;
            List<Candle> candles = state.lastNCandles.get(stock.symbol());
            if (candles == null || candles.isEmpty())
                return new LeaderResult("Nobuysell", "Leader candle missing: " + stockName);
        }
        List<String> buyLeaders = new ArrayList<>(), sellLeaders = new ArrayList<>();
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
        if (buyLeaders.size() >= AppConfig.SAME_DIRECTION_REQUIRED)
            return new LeaderResult("BUY",
                String.join("+", buyLeaders.subList(0, AppConfig.SAME_DIRECTION_REQUIRED)) + " aligned");
        if (sellLeaders.size() >= AppConfig.SAME_DIRECTION_REQUIRED)
            return new LeaderResult("SELL",
                String.join("+", sellLeaders.subList(0, AppConfig.SAME_DIRECTION_REQUIRED)) + " aligned");
        return new LeaderResult("Nobuysell", "No leader match");
    }

    // ── Entry check (runs on indicator thread pool) ────────────────────────────────

    @Async("indicatorExecutor")
    public void checkTradeEntryAsync() {
        try { checkTradeEntry(); } catch (Exception e) {
            System.err.println("Entry check error: " + e.getMessage());
        }
    }

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
        boolean rangeOk = sideRange != null && sideRange >= 12;
        IndicatorEngine.MomentumResult mom = IndicatorEngine.strongMomentum(bnCandles, interval);
        LeaderResult leaderSig = leadersMomentum();
        boolean sigOk = leaderSig.signal.equals("BUY") || leaderSig.signal.equals("SELL");

        int green = 0, red = 0, strongQty = 0;
        List<AppState.StockStat> stockStats = new ArrayList<>();
        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName)).findFirst().orElse(null);
            if (stock == null) continue;
            List<Candle> sc = state.lastNCandles.get(stock.symbol());
            Candle c = (sc != null && !sc.isEmpty()) ? sc.get(sc.size() - 1) : null;
            if (c == null) continue;
            if (c.close > c.open) green++; else if (c.close < c.open) red++;
            double qty       = state.latestMinuteQty.getOrDefault(stockName, 0.0);
            double threshold = effectiveThreshold(stockName, interval);
            if (qty >= threshold) strongQty++;
            stockStats.add(new AppState.StockStat(stockName, c, qty, threshold));
        }

        boolean dirOk         = Math.max(green, red) >= AppConfig.SAME_DIRECTION_REQUIRED;
        boolean sqOk          = strongQty >= AppConfig.SAME_DIRECTION_REQUIRED;
        boolean candleCloseOk = bn != null && isCandleClosed(bn.startTime, interval);
        boolean alreadyTraded = bn != null && bn.startTime.substring(0, 16).equals(state.lastTradeCandle);

        BNIndicators bnInd = IndicatorEngine.checkBNIndicators();
        state.bnIndicators = bnInd;

        boolean macdMet = bnInd.macdDir != null && !bnInd.macdDir.equals("—") && !bnInd.macdDir.equals("NEUTRAL");
        boolean emaMet  = bnInd.emaStack != null && (bnInd.emaStack.bullish || bnInd.emaStack.bearish);
        boolean gateOk  = bnInd.bullish || bnInd.bearish;

        // Compute time at which current candle closes
        String candleCloseTime = null;
        if (bn != null && bn.startTime != null && !bn.startTime.isEmpty()) {
            try {
                LocalDateTime start = LocalDateTime.parse(bn.startTime.substring(0, 19),
                    DateTimeFormatter.ISO_LOCAL_DATE_TIME);
                int mins = switch (interval) { case "3m" -> 3; case "5m" -> 5; case "15m" -> 15; default -> 1; };
                candleCloseTime = start.plusMinutes(mins).format(DateTimeFormatter.ofPattern("HH:mm"));
            } catch (Exception ignored) {}
        }

        AppState.EntryDiagnostics diag = new AppState.EntryDiagnostics(
            marketOpen, timeOk, noActiveTrade, cooldownMs, sideRange, candleCloseOk,
            leaderSig.signal, leaderSig.reason, green, red, strongQty,
            alreadyTraded, bnInd, bn, stockStats,
            new AppState.MomResult(mom.ok(), mom.reason()),
            candleCloseTime
        );
        state.entryDiagnostics = diag;

        boolean allOk = marketOpen && timeOk && noActiveTrade
            && cooldownMs >= 60_000 && rangeOk && mom.ok()
            && sigOk && dirOk && sqOk && candleCloseOk
            && !alreadyTraded && macdMet && emaMet && gateOk;

        boolean allButClose = marketOpen && timeOk && noActiveTrade
            && cooldownMs >= 60_000 && rangeOk && mom.ok()
            && sigOk && dirOk && sqOk && !alreadyTraded && macdMet && emaMet && gateOk;

        if (allButClose && !candleCloseOk)
            state.pendingSignal = new AppState.PendingSignal(leaderSig.signal, leaderSig.reason);
        else
            state.pendingSignal = null;

        if (allOk && state.activeTrade == null) {
            // Use live LTP for entry — more current than stale candle close price
            double entryPrice = state.bnLTP > 0 ? state.bnLTP : bn.close;
            if (bnInd.bullish && leaderSig.signal.equals("BUY"))
                enterTrade("BUY",  entryPrice, bn.startTime.substring(0, 16), "AUTO");
            else if (bnInd.bearish && leaderSig.signal.equals("SELL"))
                enterTrade("SELL", entryPrice, bn.startTime.substring(0, 16), "AUTO");
        }
        return diag;
    }

    private boolean isCandleClosed(String startTime, String interval) {
        if (startTime == null || startTime.isEmpty()) return false;
        try {
            LocalDateTime start = LocalDateTime.parse(startTime.substring(0, 19),
                DateTimeFormatter.ISO_LOCAL_DATE_TIME);
            int mins = switch (interval) { case "3m" -> 3; case "5m" -> 5; case "15m" -> 15; default -> 1; };
            return nowIST().isAfter(start.plusMinutes(mins));
        } catch (Exception e) { return false; }
    }

    // ── Position sizing ───────────────────────────────────────────────────────────

    // 1% of available funds as risk budget; min 1 lot, max 10 lots
    private int calcNumLots() {
        double riskBudget = state.availableFunds * 0.01;
        double riskPerLot = AppConfig.STOPLOSS * AppConfig.LOT_SIZE; // 18 * 30 = 540
        int lots = (int) Math.floor(riskBudget / riskPerLot);
        return Math.max(1, Math.min(lots, 10));
    }

    // ── Enter trade ───────────────────────────────────────────────────────────────

    public void enterTrade(String type, double price, String candleTime, String confidence) {
        if (state.activeTrade != null) return;
        int numLots = calcNumLots();
        state.activeTrade     = new ActiveTrade(type, price, candleTime, confidence, numLots);
        state.lastTradeCandle = candleTime;
        state.signalLocked    = true;

        Trade t = new Trade();
        t.type       = type;
        t.price      = price;
        t.time       = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        t.confidence = confidence;
        t.pnl        = 0;
        dbService.saveTrade(t);
        System.out.printf("ENTRY %s @ %.2f [%s] conf=%s lots=%d%n", type, price, candleTime, confidence, numLots);
    }

    // ── Exit check ────────────────────────────────────────────────────────────────

    public void checkExit() {
        if (state.activeTrade == null) return;
        List<Candle> bnCandles = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        if (bnCandles == null || bnCandles.isEmpty()) return;
        // Use live LTP; fall back to candle close if LTP not yet received
        double price = state.bnLTP > 0 ? state.bnLTP : bnCandles.get(bnCandles.size() - 1).close;
        ActiveTrade at = state.activeTrade;
        double pnl;
        if (at.type.equals("BUY")) {
            pnl = price - at.entry;
            if (pnl >= AppConfig.TRAIL_TRIGGER)
                at.currentSL = Math.max(at.currentSL, price - AppConfig.TRAIL_DISTANCE);
            else if (pnl >= AppConfig.BREAKEVEN_TRIGGER)
                at.currentSL = Math.max(at.currentSL, at.entry);
            if (price >= at.entry + AppConfig.TARGET || price <= at.currentSL) exitTrade(price, pnl);
        } else {
            pnl = at.entry - price;
            if (pnl >= AppConfig.TRAIL_TRIGGER)
                at.currentSL = Math.min(at.currentSL, price + AppConfig.TRAIL_DISTANCE);
            else if (pnl >= AppConfig.BREAKEVEN_TRIGGER)
                at.currentSL = Math.min(at.currentSL, at.entry);
            if (price <= at.entry - AppConfig.TARGET || price >= at.currentSL) exitTrade(price, pnl);
        }
    }

    public void exitTrade(double exitPrice, double pnlPts) {
        if (state.activeTrade == null) return;
        pnlPts = Math.round(pnlPts * 100.0) / 100.0;
        int    numLots = state.activeTrade.numLots;
        double pnlRs   = Math.round(pnlPts * numLots * AppConfig.LOT_SIZE * 100.0) / 100.0;

        Trade t = new Trade();
        t.type       = state.activeTrade.type + "_EXIT";
        t.price      = exitPrice;
        t.time       = LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"));
        t.confidence = state.activeTrade.confidence;
        t.pnl        = pnlRs;   // stored in rupees
        dbService.saveTrade(t);
        state.availableFunds += pnlRs;
        state.activeTrade   = null;
        state.lastExitTime  = System.currentTimeMillis();
        state.signalLocked  = false;
        state.pendingSignal = null;
        System.out.printf("EXIT @ %.2f PnL=%.2f pts = ₹%.2f (%d lots)%n", exitPrice, pnlPts, pnlRs, numLots);
    }

    // ── Manual entry/exit ─────────────────────────────────────────────────────────

    public void manualEntry(String type, double price) {
        if (state.activeTrade != null) return;
        enterTrade(type, price, nowIST().toString().substring(0, 16), "MANUAL");
    }

    public void manualExit() {
        if (state.activeTrade == null) return;
        List<Candle> bnCandles = state.lastNCandles.get(AppConfig.INDEX_SYMBOL);
        if (bnCandles == null || bnCandles.isEmpty()) return;
        double price = bnCandles.get(bnCandles.size() - 1).close;
        double pnl   = state.activeTrade.type.equals("BUY") ? price - state.activeTrade.entry
                                                             : state.activeTrade.entry - price;
        exitTrade(price, pnl);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────────

    public static int effectiveThreshold(String stock, String interval) {
        Integer base = AppConfig.STOCK_QTY_THRESHOLD.get(stock);
        if (base == null) return 9999;
        double mult = switch (interval) { case "3m" -> 1.5; case "5m" -> 2.0; case "15m" -> 6.0; default -> 1.0; };
        return (int)(base * mult);
    }

    public record LeaderResult(String signal, String reason) {}
}
