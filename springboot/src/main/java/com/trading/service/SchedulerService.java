package com.trading.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.trading.config.AppConfig;
import com.trading.engine.SupportResistanceEngine;
import com.trading.engine.TradeEngine;
import com.trading.model.*;
import com.trading.websocket.DashboardWebSocketHandler;
import jakarta.annotation.PostConstruct;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.*;

@Service
public class SchedulerService {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final AppState state = AppState.get();

    @Autowired private DashboardWebSocketHandler wsHandler;
    @Autowired private HistoricalDataService     historicalService;
    @Autowired private DatabaseService           dbService;
    @Autowired private TradeEngine               tradeEngine;

    @PostConstruct
    public void loadInitialData() {
        new Thread(() -> {
            System.out.println("Loading initial historical data...");
            loadHistorical();
        }, "init-data").start();
    }

    private void loadHistorical() {
        try {
            String interval = state.selectedInterval;
            // BN indicator candles (200-bar series for indicators)
            List<Candle> bnCandles = historicalService.fetchBNIndicatorCandles(interval);
            if (!bnCandles.isEmpty()) {
                synchronized (state.bnIndicatorCandles) {
                    state.bnIndicatorCandles.clear();
                    state.bnIndicatorCandles.addAll(bnCandles);
                }
                System.out.println("Loaded " + bnCandles.size() + " BN indicator candles");
            }
            // Display candles for all stocks (selected interval)
            Map<String, List<Candle>> data = historicalService.fetchHistorical(interval, state.numCandles, state.candleOffset);
            data.forEach((sym, candles) -> state.lastNCandles.put(sym, new ArrayList<>(candles)));
            System.out.println("Loaded historical candles for " + data.size() + " stocks");
            state.apiStatus = "API OK";

            // Load 5m and 15m candles (30 bars) for S/R — also seeds allIntervalCandles
            loadSRCandles("5m",  state.sr5m);
            loadSRCandles("15m", state.sr15m);

            // Load 1m display candles (5 bars) for multi-frame view
            loadIntervalCandles("1m");
            // Also seed the selected interval into allIntervalCandles if not 5m/15m
            if (!interval.equals("5m") && !interval.equals("15m")) {
                loadIntervalCandles(interval);
            }
        } catch (Exception e) {
            System.err.println("Initial load error: " + e.getMessage());
            state.apiStatus = "API Error";
        }
    }

    private void loadSRCandles(String interval, Map<String, AppState.SRLevels> dest) {
        try {
            Map<String, List<Candle>> srData = historicalService.fetchHistorical(interval, 30, 0);

            // Seed allIntervalCandles with the last 5 candles per stock from this fetch
            Map<String, List<Candle>> ivMap = state.allIntervalCandles
                .computeIfAbsent(interval, k -> new java.util.concurrent.ConcurrentHashMap<>());

            srData.forEach((sym, candles) -> {
                // S/R detection
                AppConfig.STOCKS.stream()
                    .filter(s -> s.symbol().equals(sym)).findFirst()
                    .ifPresent(s -> dest.put(s.name(), SupportResistanceEngine.detect(candles)));
                // Multi-frame display (last 5 candles)
                int from = Math.max(0, candles.size() - 5);
                ivMap.put(sym, new ArrayList<>(candles.subList(from, candles.size())));
            });
            System.out.println("Loaded SR+display for " + interval + " (" + dest.size() + " stocks)");
        } catch (Exception e) {
            System.err.println("SR load error (" + interval + "): " + e.getMessage());
        }
    }

    private void loadIntervalCandles(String interval) {
        try {
            Map<String, List<Candle>> data = historicalService.fetchHistorical(interval, 5, 0);
            Map<String, List<Candle>> ivMap = state.allIntervalCandles
                .computeIfAbsent(interval, k -> new java.util.concurrent.ConcurrentHashMap<>());
            data.forEach((sym, candles) -> ivMap.put(sym, new ArrayList<>(candles)));
            System.out.println("Loaded " + interval + " display candles (" + data.size() + " stocks)");
        } catch (Exception e) {
            System.err.println("Interval candles load error (" + interval + "): " + e.getMessage());
        }
    }

    // Push dashboard state to all browser WebSocket clients every second
    @Scheduled(fixedRate = 1000)
    public void pushDashboard() {
        try {
            String json = buildDashboardJson();
            wsHandler.broadcast(json);
        } catch (Exception e) {
            System.err.println("Push error: " + e.getMessage());
        }
    }

    // Reload historical data every 5 minutes
    @Scheduled(fixedRate = 300_000)
    public void refreshHistorical() {
        new Thread(this::loadHistorical, "hist-refresh").start();
    }

    // Refresh big trades cache every 5 seconds
    @Scheduled(fixedRate = 5000)
    public void refreshBigTrades() {
        try {
            Map<String, List<Map<String, Object>>> btData = dbService.getBigTradesData(state.selectedInterval);
            Map<String, Object> btAudit = dbService.auditStockQtyStorage(200);
            Map<String, Object> snap = new LinkedHashMap<>();
            snap.put("data",  btData);
            snap.put("audit", btAudit);
            state.bigTradesSnapshot = snap;
        } catch (Exception e) {
            System.err.println("BigTrades refresh error: " + e.getMessage());
        }
    }

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

        // Active trade
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

        // Pending signal
        if (state.pendingSignal != null)
            payload.put("pendingSignal", Map.of("type", state.pendingSignal.type(), "reason", state.pendingSignal.reason()));
        else
            payload.put("pendingSignal", null);

        // BN Indicators
        BNIndicators ind = state.bnIndicators;
        if (ind != null) {
            Map<String, Object> indMap = buildIndicatorMap(ind);
            payload.put("bnIndicators", indMap);
        }

        // Entry diagnostics — expanded to drive the Entry Loop Monitor
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

            // Momentum
            if (diag.momentum() != null)
                diagMap.put("momentum", Map.of("ok", diag.momentum().ok(), "reason", diag.momentum().reason()));

            // Full BN indicators for the entry loop rows
            if (diag.bnInd() != null)
                diagMap.put("bnInd", buildIndicatorMap(diag.bnInd()));

            // BN candle open/close
            if (diag.bnCandle() != null) {
                Candle bn = diag.bnCandle();
                diagMap.put("bn", Map.of("open", bn.open, "close", bn.close, "startTime", bn.startTime));
            }

            // Leader stocks sub-table
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

        // ── Multi-frame stock candle table (1m / 5m / 15m side-by-side) ──────────────
        payload.put("stocksMultiFrame",  buildMultiFrameStocks());
        payload.put("multiFrameCounts",  buildMultiFrameCounts());

        // Stock candles table — last 3 candles with diff per candle (matching screenshot)
        List<Map<String, Object>> stocks = new ArrayList<>();
        for (AppConfig.Stock s : AppConfig.STOCKS) {
            List<Candle> candles = state.lastNCandles.get(s.symbol());
            if (candles == null || candles.isEmpty()) continue;

            // Build last-3 candle diff list: [latest, previous, prevprev]
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

        // Candle-column summary counts (g/r/n) across all stocks, per position
        int[] g = {0,0,0}, r = {0,0,0}, n = {0,0,0};
        String[] colTimes = {"","",""};
        for (Map<String, Object> row : stocks) {
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> c3 = (List<Map<String, Object>>) row.get("c3");
            for (int i = 0; i < 3 && i < c3.size(); i++) {
                double d = ((Number) c3.get(i).get("diff")).doubleValue();
                if (d > 0) g[i]++; else if (d < 0) r[i]++; else n[i]++;
                if (colTimes[i].isEmpty()) colTimes[i] = (String) c3.get(i).get("time");
            }
        }
        List<Map<String, Object>> candleCounts = new ArrayList<>();
        String[] labels = {"Latest", "Previous", "PrevPrev"};
        for (int i = 0; i < 3; i++)
            candleCounts.add(Map.of("label", labels[i], "time", colTimes[i],
                                    "green", g[i], "red", r[i], "neutral", n[i]));

        payload.put("stocks", stocks);
        payload.put("candleCounts", candleCounts);

        // S/R levels — per stock for 5m and 15m
        List<Map<String, Object>> srList = new ArrayList<>();
        for (AppConfig.Stock s : AppConfig.STOCKS) {
            AppState.SRLevels lvl5  = state.sr5m.get(s.name());
            AppState.SRLevels lvl15 = state.sr15m.get(s.name());
            if (lvl5 == null && lvl15 == null) continue;
            Map<String, Object> sr = new LinkedHashMap<>();
            sr.put("name", s.name());
            sr.put("s5sup",  lvl5  != null ? lvl5.supports()    : List.of());
            sr.put("s5res",  lvl5  != null ? lvl5.resistances()  : List.of());
            sr.put("s15sup", lvl15 != null ? lvl15.supports()   : List.of());
            sr.put("s15res", lvl15 != null ? lvl15.resistances() : List.of());
            srList.add(sr);
        }
        payload.put("srLevels", srList);

        // Today's trades
        List<Trade> trades = dbService.getTodayTrades();
        payload.put("trades", trades);

        // Big trades (from 5s cache)
        if (state.bigTradesSnapshot != null)
            payload.put("bigTrades", state.bigTradesSnapshot);

        return MAPPER.writeValueAsString(payload);
    }

    private static final List<String> DISPLAY_INTERVALS = List.of("1m", "5m", "15m");

    /** Per-stock rows: name, symbol, frames{1m:[{time,diff},{time,diff}], 5m:…, 15m:…}, buyQty, sellQty */
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
                for (int pos = 0; pos < 2; pos++) { // Latest (pos=0) and Previous (pos=1)
                    int idx = candles != null ? candles.size() - 1 - pos : -1;
                    if (idx >= 0) {
                        Candle c = candles.get(idx);
                        double diff = Math.round((c.close - c.open) * 100.0) / 100.0;
                        String t = c.startTime != null && c.startTime.length() >= 16
                            ? c.startTime.substring(11, 16) : "";
                        Map<String, Object> cell = new LinkedHashMap<>();
                        cell.put("time", t);
                        cell.put("diff", diff);
                        cells.add(cell);
                    } else {
                        Map<String, Object> cell = new LinkedHashMap<>();
                        cell.put("time", "");
                        cell.put("diff", 0.0);
                        cell.put("missing", true);
                        cells.add(cell);
                    }
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

    /** Per-interval column summaries: {1m:[{label,time,green,red,neutral},{…}], 5m:…, 15m:…} */
    private Map<String, List<Map<String, Object>>> buildMultiFrameCounts() {
        Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
        for (String iv : DISPLAY_INTERVALS) {
            Map<String, List<Candle>> ivMap = state.allIntervalCandles.get(iv);
            int[] g = {0, 0}, r = {0, 0}, ne = {0, 0};
            String[] times = {"", ""};

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
                            // Use BANKNIFTY candle time for the column header
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
            String[] labels = {"Latest", "Previous"};
            for (int pos = 0; pos < 2; pos++) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("label",   labels[pos]);
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
        if (ind.emaStack != null)
            m.put("emaStack", Map.of(
                "ema20", ind.emaStack.ema20, "ema50", ind.emaStack.ema50,
                "bullish", ind.emaStack.bullish, "bearish", ind.emaStack.bearish));
        // Also expose as "ema" for the BN indicator panel
        if (ind.emaStack != null)
            m.put("ema", Map.of(
                "ema20", ind.emaStack.ema20, "ema50", ind.emaStack.ema50,
                "bullish", ind.emaStack.bullish, "bearish", ind.emaStack.bearish));
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
