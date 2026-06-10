package com.trading.service;

import com.trading.model.Trade;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Service;

import java.time.LocalDate;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;

@Service
public class DatabaseService {

    @Autowired
    private JdbcTemplate jdbc;

    // Async queue: tick thread offers here, dedicated writer drains in batches
    private final BlockingQueue<Object[]> stockQueue = new LinkedBlockingQueue<>(200_000);
    private volatile boolean writerRunning = false;
    private Thread writerThread;

    // In-memory cache — avoids a SELECT on every 1s scheduler tick
    private final CopyOnWriteArrayList<Trade> tradeCache = new CopyOnWriteArrayList<>();
    private volatile LocalDate cacheDate = null;

    private static final RowMapper<Trade> TRADE_ROW = (rs, n) -> {
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
    };

    @PostConstruct
    public void init() {
        // WAL: readers never block the writer, writer never blocks readers
        jdbc.execute("PRAGMA journal_mode=WAL");
        // NORMAL: fsync only at checkpoints — much faster than FULL, still crash-safe
        jdbc.execute("PRAGMA synchronous=NORMAL");
        jdbc.execute("PRAGMA cache_size=10000");
        jdbc.execute("PRAGMA temp_store=MEMORY");
        jdbc.execute("PRAGMA busy_timeout=5000");

        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stockname TEXT, time TEXT, ltp REAL, qty REAL
            )""");
        jdbc.execute("CREATE INDEX IF NOT EXISTS idx_stocks_time     ON stocks(time)");
        jdbc.execute("CREATE INDEX IF NOT EXISTS idx_stocks_name_time ON stocks(stockname,time)");
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT, price REAL, time TEXT,
                confidence TEXT, pnl REAL, optionPremium REAL
            )""");

        refreshTradeCache();

        writerRunning = true;
        writerThread = new Thread(this::batchStockWriter, "db-batch-writer");
        writerThread.setDaemon(true);
        writerThread.start();
    }

    @PreDestroy
    public void shutdown() {
        writerRunning = false;
        if (writerThread != null) writerThread.interrupt();
        flushStockQueue();
    }

    // ── Stock records ─────────────────────────────────────────────────────────────

    // Non-blocking: returns immediately; the batch writer thread does the actual INSERT
    public void addStockRecord(String stockname, String time, double ltp, double qty) {
        stockQueue.offer(new Object[]{stockname, time, ltp, qty});
    }

    private void batchStockWriter() {
        List<Object[]> batch = new ArrayList<>(200);
        while (writerRunning) {
            try {
                Object[] item = stockQueue.poll(200, TimeUnit.MILLISECONDS);
                if (item != null) {
                    batch.add(item);
                    stockQueue.drainTo(batch, 199); // coalesce burst ticks into one transaction
                }
                if (!batch.isEmpty()) {
                    jdbc.batchUpdate("INSERT INTO stocks(stockname,time,ltp,qty) VALUES(?,?,?,?)", batch);
                    batch.clear();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            } catch (Exception e) {
                System.err.println("Batch write error: " + e.getMessage());
                batch.clear();
            }
        }
    }

    private void flushStockQueue() {
        List<Object[]> remaining = new ArrayList<>();
        stockQueue.drainTo(remaining);
        if (!remaining.isEmpty()) {
            try {
                jdbc.batchUpdate("INSERT INTO stocks(stockname,time,ltp,qty) VALUES(?,?,?,?)", remaining);
            } catch (Exception e) {
                System.err.println("Final flush error: " + e.getMessage());
            }
        }
    }

    // Prune rows older than today — call once per day (e.g. from a cron-scheduled task)
    public void pruneOldStockData() {
        String today = LocalDate.now().toString();
        int deleted = jdbc.update("DELETE FROM stocks WHERE time < ?", today);
        if (deleted > 0) System.out.println("Pruned " + deleted + " old stock records");
    }

    // ── Trades ────────────────────────────────────────────────────────────────────

    public void saveTrade(Trade t) {
        jdbc.update(
            "INSERT INTO trades(type,price,time,confidence,pnl,optionPremium) VALUES(?,?,?,?,?,?)",
            t.type, t.price, t.time, t.confidence, t.pnl, t.optionPremium);
        LocalDate today = LocalDate.now();
        if (!today.equals(cacheDate)) { tradeCache.clear(); cacheDate = today; }
        if (t.time != null && t.time.startsWith(today.toString())) tradeCache.add(t);
    }

    // Reads from in-memory cache; only touches SQLite when the date rolls over
    public List<Trade> getTodayTrades() {
        if (!LocalDate.now().equals(cacheDate)) refreshTradeCache();
        return new ArrayList<>(tradeCache);
    }

    public List<Trade> getAllTrades() {
        return jdbc.query("SELECT * FROM trades ORDER BY id ASC", TRADE_ROW);
    }

    public void clearAllTrades() {
        jdbc.execute("DELETE FROM trades");
        tradeCache.clear();
        cacheDate = LocalDate.now();
    }

    private void refreshTradeCache() {
        String prefix = LocalDate.now().toString();
        List<Trade> fresh = jdbc.query(
            "SELECT * FROM trades WHERE time LIKE ? ORDER BY id ASC", TRADE_ROW, prefix + "%");
        tradeCache.clear();
        tradeCache.addAll(fresh);
        cacheDate = LocalDate.now();
    }

    // ── Big Trades ────────────────────────────────────────────────────────────────

    public Map<String, List<Map<String, Object>>> getBigTradesData(String interval) {
        int intervalMin = switch (interval) {
            case "3m" -> 3; case "5m" -> 5; case "15m" -> 15; default -> 1;
        };
        String today = LocalDate.now().toString();
        List<Map<String, Object>> rows = jdbc.queryForList(
            "SELECT stockname, time, qty, ltp FROM stocks WHERE time LIKE ? AND qty > 0 ORDER BY time DESC LIMIT 5000",
            today + "%");

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
                       : new double[]{v[0] + qty, ltp}); // sum qty; keep oldest ltp (open price)
        }

        Map<String, List<Map<String, Object>>> result = new LinkedHashMap<>();
        for (var e : buckets.entrySet()) {
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
        List<Map<String, Object>> rows = jdbc.queryForList(
            "SELECT stockname, time, qty FROM stocks ORDER BY id DESC LIMIT ?", limit);
        Set<String> tracked = Set.of("HDFC BANK", "ICICI BANK", "AXIS BANK",
                                      "STATE BANK OF INDIA", "KOTAK MAHINDRA BANK", "INDUSIND BANK");
        String today = LocalDate.now().toString();
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
                samples.add(stockname + " | " + time + " | Qty: " + row.get("qty"));
        }
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("scanned",         scanned);
        res.put("qtyRows",         qtyRows);
        res.put("trackedQty",      trackedQty);
        res.put("trackedTodayQty", trackedTodayQty);
        res.put("sampleText",      samples.isEmpty()
            ? "No recent stock rows found." : String.join(" | ", samples));
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
