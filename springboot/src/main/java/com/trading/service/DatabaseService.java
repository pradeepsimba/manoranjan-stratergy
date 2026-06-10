package com.trading.service;

import com.trading.model.Trade;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.time.LocalDate;
import java.util.*;
import java.util.stream.Collectors;

@Service
public class DatabaseService {

    @Autowired
    private JdbcTemplate jdbc;

    @PostConstruct
    public void init() {
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stockname TEXT, time TEXT, ltp REAL, qty REAL
            )""");
        jdbc.execute("CREATE INDEX IF NOT EXISTS idx_stocks_time ON stocks(time)");
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT, price REAL, time TEXT,
                confidence TEXT, pnl REAL, optionPremium REAL
            )""");
    }

    public void addStockRecord(String stockname, String time, double ltp, double qty) {
        jdbc.update("INSERT INTO stocks(stockname,time,ltp,qty) VALUES(?,?,?,?)",
            stockname, time, ltp, qty);
    }

    public void saveTrade(Trade t) {
        jdbc.update(
            "INSERT INTO trades(type,price,time,confidence,pnl,optionPremium) VALUES(?,?,?,?,?,?)",
            t.type, t.price, t.time, t.confidence, t.pnl, t.optionPremium);
    }

    public List<Trade> getTodayTrades() {
        String prefix = LocalDate.now().toString();
        return jdbc.query(
            "SELECT * FROM trades WHERE time LIKE ? ORDER BY id ASC",
            (rs, row) -> {
                Trade t = new Trade();
                t.id            = rs.getLong("id");
                t.type          = rs.getString("type");
                t.price         = rs.getDouble("price");
                t.time          = rs.getString("time");
                t.confidence    = rs.getString("confidence");
                t.pnl           = rs.getDouble("pnl");
                double op       = rs.getDouble("optionPremium");
                t.optionPremium = rs.wasNull() ? null : op;
                return t;
            },
            prefix + "%");
    }

    public List<Trade> getAllTrades() {
        return jdbc.query(
            "SELECT * FROM trades ORDER BY id ASC",
            (rs, row) -> {
                Trade t = new Trade();
                t.id            = rs.getLong("id");
                t.type          = rs.getString("type");
                t.price         = rs.getDouble("price");
                t.time          = rs.getString("time");
                t.confidence    = rs.getString("confidence");
                t.pnl           = rs.getDouble("pnl");
                double op       = rs.getDouble("optionPremium");
                t.optionPremium = rs.wasNull() ? null : op;
                return t;
            });
    }

    public void clearAllTrades() {
        jdbc.execute("DELETE FROM trades");
    }

    // ── Big Trades ─────────────────────────────────────────────────────────────

    public Map<String, List<Map<String, Object>>> getBigTradesData(String interval) {
        int intervalMin = switch (interval) {
            case "3m" -> 3; case "5m" -> 5; case "15m" -> 15; default -> 1;
        };
        String today = LocalDate.now().toString();
        List<Map<String, Object>> rows = jdbc.queryForList(
            "SELECT stockname, time, qty, ltp FROM stocks WHERE time LIKE ? AND qty > 0 ORDER BY time DESC LIMIT 5000",
            today + "%");

        // bucket: stock -> bucketTime -> [sumQty, latestLtp]
        Map<String, Map<String, double[]>> buckets = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String stockname = (String) row.get("stockname");
            if (stockname == null || stockname.equals("BANKNIFTY")) continue;
            double qty = row.get("qty") != null ? ((Number) row.get("qty")).doubleValue() : 0;
            double ltp = row.get("ltp") != null ? ((Number) row.get("ltp")).doubleValue() : 0;
            String bucket = toBucketTime((String) row.get("time"), intervalMin);
            if (bucket == null) continue;
            buckets.computeIfAbsent(stockname, k -> new LinkedHashMap<>())
                   .compute(bucket, (k, v) -> v == null
                       ? new double[]{qty, ltp}
                       : new double[]{v[0] + qty, ltp}); // sum qty, always overwrite ltp = oldest (open) price
        }

        Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
        for (Map.Entry<String, Map<String, double[]>> e : buckets.entrySet()) {
            List<Map<String, Object>> stockRows = e.getValue().entrySet().stream()
                .sorted((a, b) -> b.getKey().compareTo(a.getKey()))
                .limit(10)
                .map(en -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("time", en.getKey());
                    m.put("qty",  (long) en.getValue()[0]);
                    m.put("ltp",  en.getValue()[1]);
                    return m;
                })
                .collect(Collectors.toList());
            result.put(e.getKey(), stockRows);
        }
        return result;
    }

    public Map<String, Object> auditStockQtyStorage(int limit) {
        String today = LocalDate.now().toString();
        List<Map<String, Object>> rows = jdbc.queryForList(
            "SELECT stockname, time, qty FROM stocks ORDER BY id DESC LIMIT ?", limit);

        Set<String> tracked = Set.of("HDFC BANK", "ICICI BANK", "AXIS BANK",
                                      "STATE BANK OF INDIA", "KOTAK MAHINDRA BANK", "INDUSIND BANK");
        int scanned = 0, qtyRows = 0, trackedQty = 0, trackedTodayQty = 0;
        List<String> samples = new ArrayList<>();

        for (Map<String, Object> row : rows) {
            scanned++;
            String stockname = (String) row.get("stockname");
            String time      = (String) row.get("time");
            boolean hasQty   = row.get("qty") != null && ((Number) row.get("qty")).doubleValue() > 0;
            boolean isTracked = tracked.contains(stockname);
            boolean isToday  = time != null && time.startsWith(today);
            if (hasQty) qtyRows++;
            if (isTracked && hasQty) trackedQty++;
            if (isTracked && isToday && hasQty) trackedTodayQty++;
            if (samples.size() < 5 && (isTracked || hasQty))
                samples.add(stockname + " | " + time + " | Qty: " + (hasQty ? row.get("qty") : "missing"));
        }

        Map<String, Object> res = new LinkedHashMap<>();
        res.put("scanned",         scanned);
        res.put("qtyRows",         qtyRows);
        res.put("trackedQty",      trackedQty);
        res.put("trackedTodayQty", trackedTodayQty);
        res.put("sampleText",      samples.isEmpty()
            ? "No recent stock rows found in the last scan."
            : String.join(" | ", samples));
        return res;
    }

    private String toBucketTime(String timeStr, int intervalMin) {
        try {
            if (timeStr == null || timeStr.length() < 5) return null;
            String t = (timeStr.length() >= 16 && (timeStr.charAt(10) == ' ' || timeStr.charAt(10) == 'T'))
                ? timeStr.substring(11, 16) : timeStr.substring(0, 5);
            int hh = Integer.parseInt(t.substring(0, 2));
            int mm = Integer.parseInt(t.substring(3, 5));
            return String.format("%02d:%02d", hh, (mm / intervalMin) * intervalMin);
        } catch (Exception e) { return null; }
    }
}
