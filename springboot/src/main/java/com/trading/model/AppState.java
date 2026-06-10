package com.trading.model;

import com.trading.config.AppConfig;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

public final class AppState {

    private static final AppState INSTANCE = new AppState();
    public static AppState get() { return INSTANCE; }
    private AppState() {}

    // symbol → ordered list of candles (oldest first, newest last) — selected interval only
    public final Map<String, List<Candle>> lastNCandles = new ConcurrentHashMap<>();

    // Multi-frame candle store: interval → symbol → candles (oldest first, newest last, max 5)
    public final Map<String, Map<String, List<Candle>>> allIntervalCandles = new ConcurrentHashMap<>();

    // BankNifty candles for indicator calculations (longer series)
    public final List<Candle> bnIndicatorCandles = Collections.synchronizedList(new ArrayList<>());

    public volatile String  selectedInterval = "5m";
    public volatile int     numCandles       = 3;
    public volatile int     candleOffset     = 0;

    public volatile String  currentCandleTime = null;
    public volatile boolean signalLocked      = false;

    public volatile ActiveTrade  activeTrade     = null;
    public volatile String       lastTradeCandle = null;
    public volatile long         lastExitTime    = 0;
    public volatile PendingSignal pendingSignal  = null;

    public volatile double availableFunds = AppConfig.DEFAULT_FUNDS;

    // latest per-stock minute qty (total) and buy/sell split
    public final Map<String, Double> latestMinuteQty  = new ConcurrentHashMap<>();
    public final Map<String, Long>   latestBuyQty     = new ConcurrentHashMap<>();
    public final Map<String, Long>   latestSellQty    = new ConcurrentHashMap<>();

    // S/R levels
    public final Map<String, SRLevels> srLevels = new ConcurrentHashMap<>();

    public volatile String apiStatus = "—";
    public volatile String wsStatus  = "—";

    // Latest BankNifty live tick price — used for entry (more current than candle close)
    public volatile double bnLTP = 0;

    public volatile BNIndicators bnIndicators = null;
    public volatile String globalSignal      = "NEUTRAL";

    // Big Trades cached snapshot (replaced atomically every 5s)
    public volatile Object bigTradesSnapshot = null;

    public volatile EntryDiagnostics entryDiagnostics = null;

    // S/R levels per stock for 5m and 15m
    public final Map<String, SRLevels> sr5m  = new ConcurrentHashMap<>();
    public final Map<String, SRLevels> sr15m = new ConcurrentHashMap<>();

    public record PendingSignal(String type, String reason) {}
    public record SRLevels(List<Double> supports, List<Double> resistances) {}
    public record MomResult(boolean ok, String reason) {}
    public record EntryDiagnostics(
        boolean marketOpen, boolean timeWindowOk, boolean noActiveTrade,
        long cooldownMs, Double sidewaysRange, boolean candleCloseOk,
        String leaderSignalType, String leaderSignalReason,
        int green, int red, int strongQty,
        boolean alreadyTradedCandle, BNIndicators bnInd,
        Candle bnCandle, List<StockStat> stocks,
        MomResult momentum, String candleCloseTime
    ) {}
    public record StockStat(String stock, Candle candle, double qty, double threshold) {}
}
