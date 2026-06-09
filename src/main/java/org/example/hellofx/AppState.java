package org.example.hellofx;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Singleton holding all shared mutable state for the trading dashboard.
 * All fields accessed from multiple threads; UI fields updated via Platform.runLater().
 */
public final class AppState {

    private static final AppState INSTANCE = new AppState();
    public static AppState get() { return INSTANCE; }
    private AppState() {}

    // ── Candle state ─────────────────────────────────────────────────────────
    // symbol → ordered list of candles (oldest at index 0, newest at end)
    public final Map<String, List<Candle>> lastNCandles = new ConcurrentHashMap<>();

    // BankNifty candles used exclusively for indicator calculations (longer series).
    // Use Collections.synchronizedList — accessed from WebSocket thread AND indicator engine.
    public final List<Candle> bnIndicatorCandles = Collections.synchronizedList(new ArrayList<>());

    // Recent BN tick prices for vol estimation (ring buffer, max 60).
    public final List<Double> bnTickPrices = Collections.synchronizedList(new ArrayList<>());

    // ── Config/interval state ─────────────────────────────────────────────────
    public volatile String selectedInterval = "5m";
    public volatile int    numCandles       = 3;
    public volatile int    candleOffset     = 0;   // candleCount in HTML

    // ── Signal state ─────────────────────────────────────────────────────────
    public volatile String  currentCandleTime = null;
    public volatile boolean signalLocked      = false;

    // ── Trade state ──────────────────────────────────────────────────────────
    public volatile ActiveTrade activeTrade    = null;
    public volatile String      lastTradeCandle = null;
    public volatile long        lastExitTime    = 0;

    // Pending signal: detected but waiting for candle close
    public volatile PendingSignal pendingSignal = null;

    // ── Options state ─────────────────────────────────────────────────────────
    public volatile AtmOption atmOption = null;
    public volatile Double    manualIV  = null;

    // ── Funds ─────────────────────────────────────────────────────────────────
    public volatile double availableFunds = AppConfig.DEFAULT_FUNDS;

    // ── Latest per-stock minute qty (for big-trades panel) ────────────────────
    public final Map<String, Double> latestMinuteQty = new ConcurrentHashMap<>();

    // ── S/R levels (populated by BreakoutEngine) ──────────────────────────────
    public final Map<String, SRLevels> srLevels = new ConcurrentHashMap<>();

    // ── Connection status ─────────────────────────────────────────────────────
    public volatile String apiStatus = "—";
    public volatile String wsStatus  = "—";

    // ── BN indicators (latest computed) ───────────────────────────────────────
    public volatile BNIndicators bnIndicators = null;

    // ── Global signal ─────────────────────────────────────────────────────────
    public volatile String globalSignal     = "NEUTRAL";
    public volatile String globalSignalColor = "#777";

    // ── Entry loop diagnostics ────────────────────────────────────────────────
    public volatile EntryDiagnostics entryDiagnostics = null;

    // ── Breakout result ───────────────────────────────────────────────────────
    public volatile String breakoutText = "No Breakout Detected";

    // Helper records
    public record PendingSignal(String type, String reason) {}
    public record SRLevels(List<Double> supports, List<Double> resistances) {}
    public record EntryDiagnostics(
        boolean marketOpen, boolean timeWindowOk, boolean noActiveTrade,
        long cooldownMs, Double sidewaysRange, boolean candleCloseOk,
        String leaderSignalType, String leaderSignalReason,
        int green, int red, int strongQty,
        boolean alreadyTradedCandle, BNIndicators bnInd,
        Candle bnCandle, boolean testMode,
        List<StockStat> stocks
    ) {}
    public record StockStat(String stock, Candle candle, double qty, double threshold) {}
}
