package com.trading.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.trading.config.AppConfig;
import com.trading.engine.SupportResistanceEngine;
import com.trading.engine.TradeEngine;
import com.trading.model.*;
import com.trading.websocket.DashboardWebSocketHandler;
import jakarta.annotation.PostConstruct;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.*;

@Service
public class SchedulerService {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final AppState state = AppState.get();

    @Autowired private DashboardWebSocketHandler wsHandler;
    @Autowired private HistoricalDataService     historicalService;
    @Autowired private DatabaseService           dbService;
    @Autowired private TradeEngine               tradeEngine;

    @Autowired
    @Qualifier("historyExecutor")
    private Executor historyExecutor;

    @PostConstruct
    public void loadInitialData() {
        new Thread(() -> {
            System.out.println("Loading initial historical data in parallel...");
            loadHistorical();
        }, "init-data").start();
    }

    // ── Historical data loading ───────────────────────────────────────────────────

    private void loadHistorical() {
        try {
            String interval = state.selectedInterval;

            // Fan out all 5 API calls simultaneously
            var bnFuture   = CompletableFuture.supplyAsync(() -> historicalService.fetchBNIndicatorCandles(interval), historyExecutor);
            var mainFuture = CompletableFuture.supplyAsync(() -> historicalService.fetchHistorical(interval, state.numCandles, state.candleOffset), historyExecutor);
            var sr5Future  = CompletableFuture.supplyAsync(() -> historicalService.fetchHistorical("5m",  30, 0), historyExecutor);
            var sr15Future = CompletableFuture.supplyAsync(() -> historicalService.fetchHistorical("15m", 30, 0), historyExecutor);
            var min1Future = CompletableFuture.supplyAsync(() -> historicalService.fetchHistorical("1m",   5, 0), historyExecutor);

            CompletableFuture.allOf(bnFuture, mainFuture, sr5Future, sr15Future, min1Future).join();

            List<Candle> bnCandles = bnFuture.join();
            if (!bnCandles.isEmpty()) {
                synchronized (state.bnIndicatorCandles) {
                    state.bnIndicatorCandles.clear();
                    state.bnIndicatorCandles.addAll(bnCandles);
                }
                System.out.println("Loaded " + bnCandles.size() + " BN indicator candles");
            }

            Map<String, List<Candle>> mainData = mainFuture.join();
            mainData.forEach((sym, candles) -> state.lastNCandles.put(sym, new ArrayList<>(candles)));
            System.out.println("Loaded historical candles for " + mainData.size() + " stocks");

            processSRData(sr5Future.join(),  state.sr5m,  "5m");
            processSRData(sr15Future.join(), state.sr15m, "15m");

            var ivMap1m = state.allIntervalCandles.computeIfAbsent("1m", k -> new ConcurrentHashMap<>());
            min1Future.join().forEach((sym, candles) -> ivMap1m.put(sym, new ArrayList<>(candles)));
            System.out.println("Loaded 1m display candles");

            // Also seed selected interval if it's not one already fetched
            if (!interval.equals("5m") && !interval.equals("15m") && !interval.equals("1m")) {
                var ivMap = state.allIntervalCandles.computeIfAbsent(interval, k -> new ConcurrentHashMap<>());
                mainData.forEach((sym, candles) -> {
                    int from = Math.max(0, candles.size() - 5);
                    ivMap.put(sym, new ArrayList<>(candles.subList(from, candles.size())));
                });
            }

            state.apiStatus = "API OK";
        } catch (Exception e) {
            System.err.println("Historical load error: " + e.getMessage());
            state.apiStatus = "API Error";
        }
    }

    private void processSRData(Map<String, List<Candle>> srData,
                                Map<String, AppState.SRLevels> dest,
                                String interval) {
        var ivMap = state.allIntervalCandles.computeIfAbsent(interval, k -> new ConcurrentHashMap<>());
        srData.forEach((sym, candles) -> {
            AppConfig.STOCKS.stream().filter(s -> s.symbol().equals(sym)).findFirst()
                .ifPresent(s -> dest.put(s.name(), SupportResistanceEngine.detect(candles)));
            int from = Math.max(0, candles.size() - 5);
            ivMap.put(sym, new ArrayList<>(candles.subList(from, candles.size())));
        });
        System.out.println("SR+display loaded for " + interval + " (" + dest.size() + " stocks)");
    }

    // ── Scheduled tasks ───────────────────────────────────────────────────────────

    @Scheduled(fixedRate = 1000)
    public void pushDashboard() {
        try {
            String json = buildDashboardJson();
            wsHandler.broadcast(json);
        } catch (Exception e) {
            System.err.println("Push error: " + e.getMessage());
        }
    }

    @Scheduled(fixedRate = 300_000)
    public void refreshHistorical() {
        new Thread(this::loadHistorical, "hist-refresh").start();
    }

    @Scheduled(fixedRate = 5000)
    public void refreshBigTrades() {
        try {
            Map<String, List<Map<String, Object>>> btData = dbService.getBigTradesData(state.selectedInterval);
            Map<String, Object> snap = new LinkedHashMap<>();
            snap.put("data",  btData);
            state.bigTradesSnapshot = snap;
        } catch (Exception e) {
            System.err.println("BigTrades refresh error: " + e.getMessage());
        }
    }

    // Prune yesterday's stock rows every evening at 20:00 IST (market is closed)
    @Scheduled(cron = "0 0 20 * * MON-FRI", zone = "Asia/Kolkata")
    public void pruneOldData() {
        dbService.pruneOldStockData();
    }

    // ── Dashboard JSON builder ────────────────────────────────────────────────────

    private String buildDashboardJson() throws Exception {
        Map<String, Object> payload = new LinkedHashMap<>();

        String clock = LocalDateTime.now(ZoneId.of("Asia/Kolkata"))
            .format(DateTimeFormatter.ofPattern("HH:mm:ss"));
        payload.put("type",      "STATE_UPDATE");
        payload.put("clock",     clock);
        payload.put("wsStatus",  state.wsStatus);
        payload.put("apiStatus", state.apiStatus);
        payload.put("interval",  state.selectedInterval);
        payload.put("funds",     state.availableFunds);
        payload.put("signal",    state.globalSignal);

        if (state.activeTrade != null) {
            ActiveTrade at = state.activeTrade;
            double ltp = state.bnLTP > 0 ? state.bnLTP
                : state.lastNCandles.getOrDefault(AppConfig.INDEX_SYMBOL, List.of())
                    .stream().reduce((a, b) -> b).map(c -> c.close).orElse(at.entry);
            double pnlPts = at.type.equals("BUY") ? ltp - at.entry : at.entry - ltp;
            double pnlRs  = Math.round(pnlPts * at.numLots * AppConfig.LOT_SIZE * 100.0) / 100.0;
            payload.put("activeTrade", Map.of(
                "type",       at.type,
                "entry",      at.entry,
                "entryTime",  at.entryTime,
                "confidence", at.confidence,
                "currentSL",  at.currentSL,
                "numLots",    at.numLots,
                "ltp",        ltp,
                "pnl",        Math.round(pnlPts * 100.0) / 100.0,
                "pnlRs",      pnlRs
            ));
        } else {
            payload.put("activeTrade", null);
        }

        if (state.pendingSignal != null)
            payload.put("pendingSignal", Map.of(
                "type", state.pendingSignal.type(), "reason", state.pendingSignal.reason()));
        else
            payload.put("pendingSignal", null);

        BNIndicators ind = state.bnIndicators;
        if (ind != null) payload.put("bnIndicators", buildIndicatorMap(ind));

        AppState.EntryDiagnostics diag = state.entryDiagnostics;
        if (diag != null) {
            Map<String, Object> diagMap = new LinkedHashMap<>();
            diagMap.put("marketOpen",          diag.marketOpen());
            diagMap.put("timeWindowOk",        diag.timeWindowOk());
            diagMap.put("noActiveTrade",       diag.noActiveTrade());
            diagMap.put("cooldownMs",          diag.cooldownMs());
            diagMap.put("sidewaysRange",       diag.sidewaysRange());
            diagMap.put("candleCloseOk",       diag.candleCloseOk());
            diagMap.put("candleCloseTime",     diag.candleCloseTime());
            diagMap.put("leaderSignal",        diag.leaderSignalType());
            diagMap.put("leaderReason",        diag.leaderSignalReason());
            diagMap.put("green",               diag.green());
            diagMap.put("red",                 diag.red());
            diagMap.put("strongQty",           diag.strongQty());
            diagMap.put("alreadyTradedCandle", diag.alreadyTradedCandle());
            diagMap.put("gateOk",              diag.bnInd() != null && (diag.bnInd().bullish || diag.bnInd().bearish));
            diagMap.put("time",                clock);
            if (diag.momentum() != null)
                diagMap.put("momentum", Map.of("ok", diag.momentum().ok(), "reason", diag.momentum().reason()));
            if (diag.bnInd() != null)
                diagMap.put("bnInd", buildIndicatorMap(diag.bnInd()));
            if (diag.bnCandle() != null) {
                Candle bn = diag.bnCandle();
                diagMap.put("bn", Map.of("open", bn.open, "close", bn.close, "startTime", bn.startTime));
            }
            if (diag.stocks() != null) {
                List<Map<String, Object>> stockStats = new ArrayList<>();
                for (AppState.StockStat ss : diag.stocks()) {
                    Map<String, Object> sm = new LinkedHashMap<>();
                    sm.put("stock",     ss.stock());
                    sm.put("qty",       ss.qty());
                    sm.put("threshold", ss.threshold());
                    if (ss.candle() != null)
                        sm.put("candle", Map.of("open", ss.candle().open, "close", ss.candle().close));
                    stockStats.add(sm);
                }
                diagMap.put("stocks", stockStats);
            }
            payload.put("entryDiag", diagMap);
        }

        payload.put("stocksMultiFrame", buildMultiFrameStocks());
        payload.put("multiFrameCounts", buildMultiFrameCounts());

        // Legacy stock-candle table (kept for backward compatibility with JS)
        List<Map<String, Object>> stocks = new ArrayList<>();
        for (AppConfig.Stock s : AppConfig.STOCKS) {
            List<Candle> candles = state.lastNCandles.get(s.symbol());
            if (candles == null || candles.isEmpty()) continue;
            List<Map<String, Object>> c3 = new ArrayList<>();
            int from = Math.max(0, candles.size() - 3);
            for (int i = candles.size() - 1; i >= from; i--) {
                Candle cc = candles.get(i);
                double d = Math.round((cc.close - cc.open) * 100.0) / 100.0;
                String t = cc.startTime != null && cc.startTime.length() >= 16
                    ? cc.startTime.substring(11, 16) : "";
                c3.add(Map.of("time", t, "diff", d));
            }
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("name",    s.name());
            row.put("symbol",  s.symbol());
            row.put("c3",      c3);
            row.put("buyQty",  state.latestBuyQty.getOrDefault(s.name(), 0L));
            row.put("sellQty", state.latestSellQty.getOrDefault(s.name(), 0L));
            stocks.add(row);
        }
        int[] g = {0,0,0}, r = {0,0,0}, ne = {0,0,0};
        String[] colTimes = {"","",""};
        for (Map<String, Object> row : stocks) {
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> c3 = (List<Map<String, Object>>) row.get("c3");
            for (int i = 0; i < 3 && i < c3.size(); i++) {
                double d = ((Number) c3.get(i).get("diff")).doubleValue();
                if (d > 0) g[i]++; else if (d < 0) r[i]++; else ne[i]++;
                if (colTimes[i].isEmpty()) colTimes[i] = (String) c3.get(i).get("time");
            }
        }
        List<Map<String, Object>> candleCounts = new ArrayList<>();
        String[] labels = {"Latest", "Previous", "PrevPrev"};
        for (int i = 0; i < 3; i++)
            candleCounts.add(Map.of("label", labels[i], "time", colTimes[i],
                                    "green", g[i], "red", r[i], "neutral", ne[i]));
        payload.put("stocks",       stocks);
        payload.put("candleCounts", candleCounts);

        // S/R levels
        List<Map<String, Object>> srList = new ArrayList<>();
        for (AppConfig.Stock s : AppConfig.STOCKS) {
            AppState.SRLevels lvl5  = state.sr5m.get(s.name());
            AppState.SRLevels lvl15 = state.sr15m.get(s.name());
            if (lvl5 == null && lvl15 == null) continue;
            Map<String, Object> sr = new LinkedHashMap<>();
            sr.put("name",   s.name());
            sr.put("s5sup",  lvl5  != null ? lvl5.supports()    : List.of());
            sr.put("s5res",  lvl5  != null ? lvl5.resistances()  : List.of());
            sr.put("s15sup", lvl15 != null ? lvl15.supports()   : List.of());
            sr.put("s15res", lvl15 != null ? lvl15.resistances() : List.of());
            srList.add(sr);
        }
        payload.put("srLevels", srList);

        // Today's trades — served from in-memory cache (no DB query)
        payload.put("trades", dbService.getTodayTrades());

        if (state.bigTradesSnapshot != null)
            payload.put("bigTrades", state.bigTradesSnapshot);

        return MAPPER.writeValueAsString(payload);
    }

    private static final List<String> DISPLAY_INTERVALS = List.of("1m", "5m", "15m");

    private List<Map<String, Object>> buildMultiFrameStocks() {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (AppConfig.Stock s : AppConfig.STOCKS) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("name",   s.name());
            row.put("symbol", s.symbol());
            Map<String, List<Map<String, Object>>> frames = new LinkedHashMap<>();
            for (String iv : DISPLAY_INTERVALS) {
                Map<String, List<Candle>> ivMap = state.allIntervalCandles.get(iv);
                List<Candle> candles = ivMap != null ? ivMap.get(s.symbol()) : null;
                List<Map<String, Object>> cells = new ArrayList<>();
                for (int pos = 0; pos < 2; pos++) {
                    int idx = candles != null ? candles.size() - 1 - pos : -1;
                    Map<String, Object> cell = new LinkedHashMap<>();
                    if (idx >= 0) {
                        Candle c = candles.get(idx);
                        cell.put("time", c.startTime != null && c.startTime.length() >= 16
                            ? c.startTime.substring(11, 16) : "");
                        cell.put("diff", Math.round((c.close - c.open) * 100.0) / 100.0);
                    } else {
                        cell.put("time", "");
                        cell.put("diff", 0.0);
                        cell.put("missing", true);
                    }
                    cells.add(cell);
                }
                frames.put(iv, cells);
            }
            row.put("frames",  frames);
            row.put("buyQty",  state.latestBuyQty.getOrDefault(s.name(), 0L));
            row.put("sellQty", state.latestSellQty.getOrDefault(s.name(), 0L));
            rows.add(row);
        }
        return rows;
    }

    private Map<String, List<Map<String, Object>>> buildMultiFrameCounts() {
        Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
        for (String iv : DISPLAY_INTERVALS) {
            Map<String, List<Candle>> ivMap = state.allIntervalCandles.get(iv);
            int[] g = {0,0}, r = {0,0}, ne = {0,0};
            String[] times = {"",""};
            if (ivMap != null) {
                for (AppConfig.Stock s : AppConfig.STOCKS) {
                    List<Candle> candles = ivMap.get(s.symbol());
                    for (int pos = 0; pos < 2; pos++) {
                        int idx = candles != null ? candles.size() - 1 - pos : -1;
                        if (idx >= 0) {
                            Candle c = candles.get(idx);
                            if (c.close > c.open) g[pos]++;
                            else if (c.close < c.open) r[pos]++;
                            else ne[pos]++;
                            if (times[pos].isEmpty() && AppConfig.INDEX_SYMBOL.equals(s.symbol())
                                    && c.startTime != null && c.startTime.length() >= 16)
                                times[pos] = c.startTime.substring(11, 16);
                        } else {
                            ne[pos]++;
                        }
                    }
                }
            }
            List<Map<String, Object>> cols = new ArrayList<>();
            String[] colLabels = {"Latest", "Previous"};
            for (int pos = 0; pos < 2; pos++) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("label",   colLabels[pos]);
                m.put("time",    times[pos]);
                m.put("green",   g[pos]);
                m.put("red",     r[pos]);
                m.put("neutral", ne[pos]);
                cols.add(m);
            }
            result.put(iv, cols);
        }
        return result;
    }

    private Map<String, Object> buildIndicatorMap(BNIndicators ind) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("rsi",     ind.rsi);
        m.put("macdDir", ind.macdDir);
        m.put("macdVal", ind.macdVal);
        m.put("bull",    ind.bull);
        m.put("bear",    ind.bear);
        m.put("bullish", ind.bullish);
        m.put("bearish", ind.bearish);
        if (ind.emaStack != null) {
            var ema = Map.of("ema20", ind.emaStack.ema20, "ema50", ind.emaStack.ema50,
                             "bullish", ind.emaStack.bullish, "bearish", ind.emaStack.bearish);
            m.put("emaStack", ema);
            m.put("ema",      ema);
        }
        if (ind.leaderPat != null) {
            List<Map<String, String>> matches = new ArrayList<>();
            for (BNIndicators.PatternMatch pm : ind.leaderPat.matches)
                matches.add(Map.of("stock", pm.stock, "pattern", pm.pattern));
            m.put("leaderPat", Map.of(
                "bullCount", ind.leaderPat.bullCount,
                "bearCount", ind.leaderPat.bearCount,
                "matches",   matches));
        }
        return m;
    }
}
