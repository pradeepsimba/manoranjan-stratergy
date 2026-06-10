package com.trading.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.trading.config.AppConfig;
import com.trading.model.AppState;
import com.trading.model.Candle;
import org.springframework.stereotype.Service;

import javax.net.ssl.*;
import java.net.URI;
import java.net.http.*;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.*;

@Service
public class HistoricalDataService {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final HttpClient httpClient;

    public HistoricalDataService() {
        this.httpClient = buildTrustAllClient();
    }

    private static HttpClient buildTrustAllClient() {
        try {
            TrustManager[] trustAll = new TrustManager[]{
                new X509ExtendedTrustManager() {
                    public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                    public void checkClientTrusted(X509Certificate[] c, String a) {}
                    public void checkServerTrusted(X509Certificate[] c, String a) {}
                    public void checkClientTrusted(X509Certificate[] c, String a, java.net.Socket s) {}
                    public void checkServerTrusted(X509Certificate[] c, String a, java.net.Socket s) {}
                    public void checkClientTrusted(X509Certificate[] c, String a, SSLEngine e) {}
                    public void checkServerTrusted(X509Certificate[] c, String a, SSLEngine e) {}
                }
            };
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, trustAll, new SecureRandom());
            SSLParameters params = new SSLParameters();
            params.setEndpointIdentificationAlgorithm(null);
            return HttpClient.newBuilder().sslContext(ctx).sslParameters(params).build();
        } catch (Exception e) {
            throw new RuntimeException("SSL setup failed", e);
        }
    }

    public Map<String, List<Candle>> fetchHistorical(String interval, int numCandles, int offset) {
        Map<String, List<Candle>> result = new LinkedHashMap<>();
        try {
            String[] dates = calculateDates(interval, numCandles, offset);
            String url     = String.format(AppConfig.API_URL_TEMPLATE, AppConfig.API_HOST, dates[0], dates[1]);

            List<Map<String, Object>> payload = new ArrayList<>();
            for (AppConfig.Stock s : AppConfig.STOCKS) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("stockname",    s.name());
                m.put("stock_symbol", s.symbol());
                m.put("intervals",    List.of(interval));
                payload.add(m);
            }

            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(payload)))
                .build();

            HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
            JsonNode root = MAPPER.readTree(resp.body());
            String key = interval + " data";

            for (JsonNode stockNode : root) {
                String symbol = stockNode.has("stock_symbol") ? stockNode.get("stock_symbol").asText() : "";
                JsonNode cn   = stockNode.has(key) ? stockNode.get(key) : null;
                if (cn == null || !cn.isArray()) continue;
                List<Candle> candles = parseCandles(cn);
                int total = numCandles + offset;
                List<Candle> window;
                if (offset == 0) {
                    window = candles.size() <= numCandles ? candles
                        : candles.subList(candles.size() - numCandles, candles.size());
                } else {
                    int from = Math.max(0, candles.size() - total);
                    int to   = Math.max(0, candles.size() - offset);
                    window = candles.subList(from, to);
                }
                result.put(symbol, new ArrayList<>(window));
            }
            AppState.get().apiStatus = "API OK";
        } catch (Exception e) {
            AppState.get().apiStatus = "API Error: " + e.getMessage();
            System.err.println("fetchHistorical error: " + e.getMessage());
        }
        return result;
    }

    public List<Candle> fetchBNIndicatorCandles(String interval) {
        try {
            LocalDate today = LocalDate.now();
            String from = today.minusDays(7).toString();
            String to   = today.plusDays(1).toString();
            String url  = String.format(AppConfig.API_URL_TEMPLATE, AppConfig.API_HOST, from, to);

            List<Map<String, Object>> payload = List.of(Map.of(
                "stockname", "BANKNIFTY", "stock_symbol", "26009", "intervals", List.of(interval)));

            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(payload)))
                .build();

            HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
            JsonNode root = MAPPER.readTree(resp.body());
            if (root.isArray() && root.size() > 0) {
                JsonNode cn = root.get(0).has(interval + " data") ? root.get(0).get(interval + " data") : null;
                if (cn != null && cn.isArray()) return parseCandles(cn);
            }
        } catch (Exception e) {
            System.err.println("fetchBNIndicatorCandles error: " + e.getMessage());
        }
        return Collections.emptyList();
    }

    private List<Candle> parseCandles(JsonNode arr) {
        List<Candle> list = new ArrayList<>();
        for (JsonNode n : arr) {
            Candle c = new Candle();
            c.startTime = n.has("start_time") ? n.get("start_time").asText() : "";
            c.open   = n.has("open")   ? n.get("open").asDouble()   : 0;
            c.close  = n.has("close")  ? n.get("close").asDouble()  : 0;
            c.high   = n.has("high")   ? n.get("high").asDouble()   : 0;
            c.low    = n.has("low")    ? n.get("low").asDouble()    : 0;
            c.volume = n.has("volume") ? n.get("volume").asDouble() : 0;
            list.add(c);
        }
        return list;
    }

    private String[] calculateDates(String interval, int numCandles, int offset) {
        int mins = switch (interval) { case "3m" -> 3; case "5m" -> 5; case "15m" -> 15; default -> 1; };
        LocalDateTime now = LocalDateTime.now(ZoneId.of("Asia/Kolkata"));
        int snapped = (now.getMinute() / mins) * mins;
        LocalDateTime end = now.withMinute(snapped).withSecond(0).withNano(0);
        LocalDateTime cap = now.withHour(15).withMinute(30).withSecond(0).withNano(0);
        if (end.isAfter(cap)) end = cap;
        LocalDateTime start = end.minusMinutes((long)(numCandles + offset) * mins);
        DateTimeFormatter fmt = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss");
        return new String[]{ start.format(fmt), end.format(fmt) };
    }
}
