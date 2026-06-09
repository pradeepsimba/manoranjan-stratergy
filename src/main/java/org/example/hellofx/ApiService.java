package org.example.hellofx;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import javax.net.ssl.*;
import java.net.URI;
import java.net.http.*;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.*;

/**
 * REST API client for historical candle data.
 * Uses trust-all SSLContext because the server uses a self-signed certificate.
 */
public class ApiService {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static HttpClient httpClient;

    static {
        try {
            // X509ExtendedTrustManager overrides the engine-level IP/hostname SAN check
            // that still fires even when a plain X509TrustManager trusts the cert.
            TrustManager[] trustAll = new TrustManager[]{
                new javax.net.ssl.X509ExtendedTrustManager() {
                    public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                    public void checkClientTrusted(X509Certificate[] c, String a) {}
                    public void checkServerTrusted(X509Certificate[] c, String a) {}
                    public void checkClientTrusted(X509Certificate[] c, String a, java.net.Socket s) {}
                    public void checkServerTrusted(X509Certificate[] c, String a, java.net.Socket s) {}
                    public void checkClientTrusted(X509Certificate[] c, String a, javax.net.ssl.SSLEngine e) {}
                    public void checkServerTrusted(X509Certificate[] c, String a, javax.net.ssl.SSLEngine e) {}
                }
            };

            SSLContext sslCtx = SSLContext.getInstance("TLS");
            sslCtx.init(null, trustAll, new SecureRandom());

            // Belt-and-suspenders: also disable endpoint identification at the HttpClient level
            javax.net.ssl.SSLParameters sslParams = new javax.net.ssl.SSLParameters();
            sslParams.setEndpointIdentificationAlgorithm(null);

            httpClient = HttpClient.newBuilder()
                .sslContext(sslCtx)
                .sslParameters(sslParams)
                .build();
        } catch (Exception e) {
            throw new RuntimeException("Failed to create trust-all HttpClient", e);
        }
    }

    /**
     * Fetches candles for all stocks for the given interval and date range.
     * Returns map: symbol → list of Candles (oldest first).
     */
    public static Map<String, List<Candle>> fetchHistorical(String interval, int numCandles, int offset) {
        Map<String, List<Candle>> result = new LinkedHashMap<>();
        try {
            String[] dates = calculateDates(interval, numCandles, offset);
            String fromDate = dates[0];
            String toDate   = dates[1];

            String url = String.format(AppConfig.API_URL_TEMPLATE, AppConfig.API_HOST, fromDate, toDate);

            // Build payload
            List<Map<String, Object>> payload = new ArrayList<>();
            for (AppConfig.Stock s : AppConfig.STOCKS) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("stockname",    s.name());
                m.put("stock_symbol", s.symbol());
                m.put("intervals",    List.of(interval));
                payload.add(m);
            }

            String body = MAPPER.writeValueAsString(payload);

            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();

            HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
            JsonNode root = MAPPER.readTree(resp.body());

            String intervalKey = interval + " data";

            for (JsonNode stockNode : root) {
                String symbol = stockNode.has("stock_symbol") ? stockNode.get("stock_symbol").asText() : "";
                JsonNode candlesNode = stockNode.has(intervalKey) ? stockNode.get(intervalKey) : null;
                if (candlesNode == null || !candlesNode.isArray()) continue;

                List<Candle> candles = new ArrayList<>();
                for (JsonNode cn : candlesNode) {
                    Candle c = new Candle();
                    c.startTime = cn.has("start_time") ? cn.get("start_time").asText() : "";
                    c.open   = cn.has("open")   ? cn.get("open").asDouble()   : 0;
                    c.close  = cn.has("close")  ? cn.get("close").asDouble()  : 0;
                    c.high   = cn.has("high")   ? cn.get("high").asDouble()   : 0;
                    c.low    = cn.has("low")    ? cn.get("low").asDouble()    : 0;
                    c.volume = cn.has("volume") ? cn.get("volume").asDouble() : 0;
                    candles.add(c);
                }
                // Keep only the window needed for display
                int total = numCandles + offset;
                List<Candle> display;
                if (offset == 0) {
                    display = candles.size() <= numCandles ? candles
                        : candles.subList(candles.size() - numCandles, candles.size());
                } else {
                    int from = Math.max(0, candles.size() - total);
                    int to   = Math.max(0, candles.size() - offset);
                    display = candles.subList(from, to);
                }
                result.put(symbol, new ArrayList<>(display));
            }
        } catch (Exception e) {
            System.err.println("fetchHistorical error: " + e.getMessage());
        }
        return result;
    }

    /** Fetches a longer candle series for BN indicator calculations (up to 200 candles). */
    public static List<Candle> fetchBNIndicatorCandles(String interval) {
        try {
            LocalDate today = LocalDate.now();
            // Fetch 7 days back to get ~200 candles for indicators
            String fromDate = today.minusDays(7).toString();
            String toDate   = today.plusDays(1).toString();
            String url = String.format(AppConfig.API_URL_TEMPLATE, AppConfig.API_HOST, fromDate, toDate);

            List<Map<String, Object>> payload = new ArrayList<>();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("stockname",    "BANKNIFTY");
            m.put("stock_symbol", "26009");
            m.put("intervals",    List.of(interval));
            payload.add(m);

            String body = MAPPER.writeValueAsString(payload);
            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();

            HttpResponse<String> resp = httpClient.send(req, HttpResponse.BodyHandlers.ofString());
            JsonNode root = MAPPER.readTree(resp.body());
            String intervalKey = interval + " data";

            List<Candle> candles = new ArrayList<>();
            if (root.isArray() && root.size() > 0) {
                JsonNode candlesNode = root.get(0).has(intervalKey) ? root.get(0).get(intervalKey) : null;
                if (candlesNode != null && candlesNode.isArray()) {
                    for (JsonNode cn : candlesNode) {
                        Candle c = new Candle();
                        c.startTime = cn.has("start_time") ? cn.get("start_time").asText() : "";
                        c.open   = cn.has("open")   ? cn.get("open").asDouble()   : 0;
                        c.close  = cn.has("close")  ? cn.get("close").asDouble()  : 0;
                        c.high   = cn.has("high")   ? cn.get("high").asDouble()   : 0;
                        c.low    = cn.has("low")    ? cn.get("low").asDouble()    : 0;
                        c.volume = cn.has("volume") ? cn.get("volume").asDouble() : 0;
                        candles.add(c);
                    }
                }
            }
            return candles;
        } catch (Exception e) {
            System.err.println("fetchBNIndicatorCandles error: " + e.getMessage());
            return Collections.emptyList();
        }
    }

    // ── Date helpers ────────────────────────────────────────────────────────────

    static String[] calculateDates(String interval, int numCandles, int offset) {
        int intervalMin = switch (interval) {
            case "3m"  -> 3;
            case "5m"  -> 5;
            case "15m" -> 15;
            default    -> 1;
        };

        LocalDateTime now = nowIST();

        // Snap to interval boundary
        int snappedMin = (now.getMinute() / intervalMin) * intervalMin;
        LocalDateTime snapped = now.withMinute(snappedMin).withSecond(0).withNano(0);

        // Market close cap
        LocalDateTime marketClose = now.withHour(15).withMinute(30).withSecond(0).withNano(0);
        if (snapped.isAfter(marketClose)) snapped = marketClose;

        int total = numCandles + offset;
        LocalDateTime from = snapped.minusMinutes((long) total * intervalMin);

        DateTimeFormatter fmt = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss");
        return new String[]{ from.format(fmt), snapped.format(fmt) };
    }

    public static LocalDateTime nowIST() {
        return LocalDateTime.now(java.time.ZoneId.of("Asia/Kolkata"));
    }
}
