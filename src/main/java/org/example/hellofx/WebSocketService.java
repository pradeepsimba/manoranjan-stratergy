package org.example.hellofx;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.util.*;
import java.util.concurrent.CompletionStage;
import java.util.function.Consumer;

/**
 * Java 11 built-in WebSocket client for live tick feed.
 * Sends LIVE_FEED_INIT on connect, processes JSON tick arrays.
 */
public class WebSocketService {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private WebSocket ws;
    private final Consumer<List<TickUpdate>> onTicks;
    private final Consumer<String> onStatusChange;
    private volatile boolean running = false;

    public WebSocketService(Consumer<List<TickUpdate>> onTicks, Consumer<String> onStatusChange) {
        this.onTicks        = onTicks;
        this.onStatusChange = onStatusChange;
    }

    public void connect(String interval) {
        running = true;
        try {
            HttpClient client = HttpClient.newHttpClient();
            ws = client.newWebSocketBuilder()
                .buildAsync(URI.create(AppConfig.WS_URL), new Listener(interval))
                .join();
        } catch (Exception e) {
            onStatusChange.accept("WS Error: " + e.getMessage());
        }
    }

    public void disconnect() {
        running = false;
        if (ws != null) {
            try { ws.sendClose(WebSocket.NORMAL_CLOSURE, "bye"); } catch (Exception ignored) {}
            ws = null;
        }
    }

    private class Listener implements WebSocket.Listener {
        private final String interval;
        private final StringBuilder buffer = new StringBuilder();

        Listener(String interval) { this.interval = interval; }

        @Override
        public void onOpen(WebSocket webSocket) {
            onStatusChange.accept("WS Connected");
            // Send LIVE_FEED_INIT
            try {
                List<Map<String, String>> filters = new ArrayList<>();
                for (AppConfig.Stock s : AppConfig.STOCKS) {
                    Map<String, String> f = new LinkedHashMap<>();
                    f.put("stock_symbol", s.symbol());
                    f.put("stockname",    s.name());
                    f.put("interval",     interval);
                    filters.add(f);
                }
                Map<String, Object> init = new LinkedHashMap<>();
                init.put("type",       "LIVE_FEED_INIT");
                init.put("filters",    filters);
                init.put("latestOnly", true);
                webSocket.sendText(MAPPER.writeValueAsString(init), true);
            } catch (Exception e) {
                System.err.println("WS init send error: " + e.getMessage());
            }
            webSocket.request(1);
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            buffer.append(data);
            if (last) {
                String msg = buffer.toString();
                buffer.setLength(0);
                try {
                    JsonNode root = MAPPER.readTree(msg);
                    List<TickUpdate> ticks = new ArrayList<>();
                    if (root.isArray()) {
                        for (JsonNode node : root) {
                            ticks.add(parseTick(node));
                        }
                    } else {
                        ticks.add(parseTick(root));
                    }
                    if (!ticks.isEmpty()) onTicks.accept(ticks);
                } catch (Exception e) {
                    System.err.println("WS parse error: " + e.getMessage());
                }
            }
            webSocket.request(1);
            return null;
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            onStatusChange.accept("WS Error");
            if (running) {
                // Reconnect after delay
                new Thread(() -> {
                    try { Thread.sleep(5000); } catch (InterruptedException ignored) {}
                    if (running) connect(interval);
                }).start();
            }
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            onStatusChange.accept("WS Disconnected");
            if (running) {
                new Thread(() -> {
                    try { Thread.sleep(5000); } catch (InterruptedException ignored) {}
                    if (running) connect(interval);
                }).start();
            }
            return null;
        }

        private TickUpdate parseTick(JsonNode n) {
            TickUpdate t = new TickUpdate();
            t.stockSymbol = n.has("stock_symbol") ? n.get("stock_symbol").asText() : "";
            t.stockname   = n.has("stockname")    ? n.get("stockname").asText()    : "";
            t.interval    = n.has("interval")     ? n.get("interval").asText()     : "";
            t.startTime   = n.has("start_time")   ? n.get("start_time").asText()   : "";
            t.open        = n.has("open")         ? n.get("open").asDouble()        : 0;
            t.close       = n.has("close")        ? n.get("close").asDouble()       : 0;
            t.high        = n.has("high")         ? n.get("high").asDouble()        : 0;
            t.low         = n.has("low")          ? n.get("low").asDouble()         : 0;
            t.volume      = n.has("volume")       ? n.get("volume").asDouble()      : 0;
            t.snap        = n.has("snap")         ? n.get("snap").asText()          : "";

            // Parse LTP from "LTP 52050.00 ..." string
            if (n.has("ltp")) {
                String ltpStr = n.get("ltp").asText();
                java.util.regex.Matcher m = java.util.regex.Pattern.compile("LTP\\s*([\\d.]+)").matcher(ltpStr);
                if (m.find()) {
                    try { t.ltp = Double.parseDouble(m.group(1)); } catch (NumberFormatException ignored) {}
                } else {
                    try { t.ltp = Double.parseDouble(ltpStr); } catch (NumberFormatException ignored) {}
                }
            }
            // Parse BuyQty / SellQty from snap
            if (!t.snap.isEmpty()) {
                java.util.regex.Matcher bm = java.util.regex.Pattern.compile("BuyQty (\\d+)").matcher(t.snap);
                java.util.regex.Matcher sm = java.util.regex.Pattern.compile("SellQty (\\d+)").matcher(t.snap);
                if (bm.find()) t.buyQty  = Long.parseLong(bm.group(1));
                if (sm.find()) t.sellQty = Long.parseLong(sm.group(1));
            }
            return t;
        }
    }

    public static class TickUpdate {
        public String stockSymbol;
        public String stockname;
        public String interval;
        public String startTime;
        public double open, close, high, low, ltp, volume;
        public long   buyQty, sellQty;
        public String snap;
    }
}
