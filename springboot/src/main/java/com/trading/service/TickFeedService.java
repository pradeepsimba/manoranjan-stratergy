package com.trading.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trading.config.AppConfig;
import com.trading.engine.TradeEngine;
import com.trading.model.AppState;
import com.trading.model.Candle;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.*;
import java.util.concurrent.CompletionStage;

/**
 * Connects to the upstream WebSocket tick feed and updates AppState.
 * Uses Java's built-in HttpClient WebSocket (no extra library needed).
 */
@Service
public class TickFeedService {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final AppState state = AppState.get();

    @Autowired private TradeEngine tradeEngine;
    @Autowired private DatabaseService dbService;

    private WebSocket ws;
    private volatile boolean running = false;

    @PostConstruct
    public void start() {
        running = true;
        new Thread(this::connect, "ws-feed").start();
    }

    @PreDestroy
    public void stop() {
        running = false;
        if (ws != null) {
            try { ws.sendClose(WebSocket.NORMAL_CLOSURE, "shutdown"); } catch (Exception ignored) {}
        }
    }

    private void connect() {
        try {
            HttpClient client = HttpClient.newHttpClient();
            ws = client.newWebSocketBuilder()
                .buildAsync(URI.create(AppConfig.WS_URL), new Listener())
                .join();
        } catch (Exception e) {
            state.wsStatus = "WS Error: " + e.getMessage();
            scheduleReconnect();
        }
    }

    private void scheduleReconnect() {
        if (!running) return;
        new Thread(() -> {
            try { Thread.sleep(5000); } catch (InterruptedException ignored) {}
            if (running) connect();
        }, "ws-reconnect").start();
    }

    private class Listener implements WebSocket.Listener {
        private final StringBuilder buf = new StringBuilder();

        @Override
        public void onOpen(WebSocket webSocket) {
            state.wsStatus = "WS Connected";
            try {
                // Subscribe to 1m, 5m, 15m simultaneously for multi-frame candle view
                List<Map<String, String>> filters = new ArrayList<>();
                for (AppConfig.Stock s : AppConfig.STOCKS) {
                    for (String iv : List.of("1m", "5m", "15m")) {
                        Map<String, String> f = new LinkedHashMap<>();
                        f.put("stock_symbol", s.symbol());
                        f.put("stockname",    s.name());
                        f.put("interval",     iv);
                        filters.add(f);
                    }
                }
                Map<String, Object> init = new LinkedHashMap<>();
                init.put("type",       "LIVE_FEED_INIT");
                init.put("filters",    filters);
                init.put("latestOnly", true);
                webSocket.sendText(MAPPER.writeValueAsString(init), true);
            } catch (Exception e) {
                System.err.println("WS init error: " + e.getMessage());
            }
            webSocket.request(1);
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            buf.append(data);
            if (last) {
                String msg = buf.toString();
                buf.setLength(0);
                try {
                    JsonNode root = MAPPER.readTree(msg);
                    if (root.isArray()) { for (JsonNode n : root) processTick(n); }
                    else processTick(root);
                } catch (Exception e) {
                    System.err.println("WS parse error: " + e.getMessage());
                }
            }
            webSocket.request(1);
            return null;
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            state.wsStatus = "WS Error";
            scheduleReconnect();
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int code, String reason) {
            state.wsStatus = "WS Disconnected";
            scheduleReconnect();
            return null;
        }
    }

    private void processTick(JsonNode n) {
        String symbol    = n.has("stock_symbol") ? n.get("stock_symbol").asText() : "";
        String stockname = n.has("stockname")    ? n.get("stockname").asText()    : "";
        String interval  = n.has("interval")     ? n.get("interval").asText()     : "";
        String startTime = n.has("start_time")   ? n.get("start_time").asText()   : "";
        double open      = n.has("open")         ? n.get("open").asDouble()        : 0;
        double close     = n.has("close")        ? n.get("close").asDouble()       : 0;
        double high      = n.has("high")         ? n.get("high").asDouble()        : 0;
        double low       = n.has("low")          ? n.get("low").asDouble()         : 0;
        double volume    = n.has("volume")       ? n.get("volume").asDouble()      : 0;

        double ltp = 0;
        if (n.has("ltp")) {
            String ltpStr = n.get("ltp").asText();
            var m = java.util.regex.Pattern.compile("LTP\\s*([\\d.]+)").matcher(ltpStr);
            try { ltp = m.find() ? Double.parseDouble(m.group(1)) : Double.parseDouble(ltpStr); }
            catch (NumberFormatException ignored) {}
        }

        long buyQty = 0, sellQty = 0;
        if (n.has("snap")) {
            String snap = n.get("snap").asText();
            var bm = java.util.regex.Pattern.compile("BuyQty (\\d+)").matcher(snap);
            var sm = java.util.regex.Pattern.compile("SellQty (\\d+)").matcher(snap);
            if (bm.find()) buyQty  = Long.parseLong(bm.group(1));
            if (sm.find()) sellQty = Long.parseLong(sm.group(1));
        }

        Candle candle = new Candle(startTime, open, close, high, low, volume);

        // Update multi-frame candles for ALL intervals (1m, 5m, 15m)
        if (!interval.isEmpty() && !symbol.isEmpty()) {
            updateAllIntervalCandles(symbol, interval, candle);
        }

        // Everything below is selected-interval-only (trading engine)
        if (!interval.equals(state.selectedInterval)) return;

        updateLastNCandles(symbol, candle);

        double qty = buyQty + sellQty;
        if (!stockname.isEmpty()) {
            state.latestMinuteQty.put(stockname, qty);
            state.latestBuyQty.put(stockname, buyQty);
            state.latestSellQty.put(stockname, sellQty);
        }

        if (AppConfig.INDEX_SYMBOL.equals(symbol) && ltp > 0) {
            state.bnLTP = ltp;
        }

        if (ltp > 0 && !stockname.isEmpty()) {
            dbService.addStockRecord(stockname, startTime, ltp, qty);
        }

        if (AppConfig.INDEX_SYMBOL.equals(symbol)) {
            tradeEngine.checkExit();
            tradeEngine.checkTradeEntryAsync();
        }
    }

    private void updateAllIntervalCandles(String symbol, String interval, Candle candle) {
        state.allIntervalCandles
            .computeIfAbsent(interval, k -> new java.util.concurrent.ConcurrentHashMap<>())
            .compute(symbol, (k, list) -> {
                if (list == null) list = new ArrayList<>();
                if (!list.isEmpty() && list.get(list.size() - 1).startTime.equals(candle.startTime)) {
                    list.set(list.size() - 1, candle); // update in-progress candle
                } else {
                    list.add(candle);
                    if (list.size() > 5) list.remove(0); // keep 5 candles per interval
                }
                return list;
            });
    }

    private void updateLastNCandles(String symbol, Candle candle) {
        state.lastNCandles.compute(symbol, (k, list) -> {
            if (list == null) list = new ArrayList<>();
            if (!list.isEmpty() && list.get(list.size() - 1).startTime.equals(candle.startTime)) {
                list.set(list.size() - 1, candle); // update in-progress candle
            } else {
                list.add(candle);
                if (list.size() > 200) list.remove(0);
            }
            return list;
        });

        // Also update bnIndicatorCandles for BANKNIFTY
        if (AppConfig.INDEX_SYMBOL.equals(symbol)) {
            synchronized (state.bnIndicatorCandles) {
                var bn = state.bnIndicatorCandles;
                if (!bn.isEmpty() && bn.get(bn.size() - 1).startTime.equals(candle.startTime)) {
                    bn.set(bn.size() - 1, candle);
                } else {
                    bn.add(candle);
                    if (bn.size() > 300) bn.remove(0);
                }
            }
        }
    }
}
