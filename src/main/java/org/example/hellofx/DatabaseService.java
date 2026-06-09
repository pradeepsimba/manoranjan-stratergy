package org.example.hellofx;

import java.sql.*;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;

/**
 * SQLite persistence. All public methods synchronize on the shared connection
 * so background scheduler threads and the FX thread can call them safely.
 *
 * Trade times are stored as ISO-8601 (yyyy-MM-dd HH:mm:ss) for reliable date filtering.
 */
public class DatabaseService {

    private static final String DB_URL = "jdbc:sqlite:trading.db";
    private static Connection conn;
    private static final Object LOCK = new Object();

    public static void init() {
        synchronized (LOCK) {
            try {
                conn = DriverManager.getConnection(DB_URL);
                try (Statement st = conn.createStatement()) {
                    st.execute("""
                        CREATE TABLE IF NOT EXISTS stocks (
                            id        INTEGER PRIMARY KEY AUTOINCREMENT,
                            stockname TEXT,
                            time      TEXT,
                            ltp       REAL,
                            qty       REAL
                        )""");
                    st.execute("CREATE INDEX IF NOT EXISTS idx_stocks_time ON stocks(time)");
                    st.execute("""
                        CREATE TABLE IF NOT EXISTS trades (
                            id            INTEGER PRIMARY KEY AUTOINCREMENT,
                            type          TEXT,
                            price         REAL,
                            time          TEXT,
                            confidence    TEXT,
                            pnl           REAL,
                            optionPremium REAL
                        )""");
                }
            } catch (SQLException e) {
                System.err.println("DB init error: " + e.getMessage());
            }
        }
    }

    // ── Stocks ─────────────────────────────────────────────────────────────────

    public static void addStockRecord(String stockname, String time, double ltp, double qty) {
        synchronized (LOCK) {
            try (PreparedStatement ps = conn.prepareStatement(
                    "INSERT INTO stocks(stockname,time,ltp,qty) VALUES(?,?,?,?)")) {
                ps.setString(1, stockname);
                ps.setString(2, time);
                ps.setDouble(3, ltp);
                ps.setDouble(4, qty);
                ps.executeUpdate();
            } catch (SQLException e) {
                System.err.println("addStockRecord error: " + e.getMessage());
            }
        }
    }

    public static List<StockTick> getTodayStockTicks() {
        String today = LocalDate.now().toString(); // yyyy-MM-dd
        synchronized (LOCK) {
            List<StockTick> list = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT stockname,time,ltp,qty FROM stocks WHERE time LIKE ? ORDER BY time DESC LIMIT 5000")) {
                ps.setString(1, today + "%");
                ResultSet rs = ps.executeQuery();
                while (rs.next()) {
                    list.add(new StockTick(
                        rs.getString("stockname"), rs.getString("time"),
                        rs.getDouble("ltp"),       rs.getDouble("qty")));
                }
            } catch (SQLException e) {
                System.err.println("getTodayStockTicks error: " + e.getMessage());
            }
            return list;
        }
    }

    // ── Trades ─────────────────────────────────────────────────────────────────

    /** Save a trade. Time is stored as ISO-8601 for reliable date filtering. */
    public static void saveTrade(Trade t) {
        synchronized (LOCK) {
            try (PreparedStatement ps = conn.prepareStatement(
                    "INSERT INTO trades(type,price,time,confidence,pnl,optionPremium) VALUES(?,?,?,?,?,?)")) {
                ps.setString(1, t.type);
                ps.setDouble(2, t.price);
                // Store as ISO timestamp so getTodayTrades() can filter reliably
                ps.setString(3, t.time != null ? t.time
                    : java.time.LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")));
                ps.setString(4, t.confidence);
                ps.setDouble(5, t.pnl);
                if (t.optionPremium != null) ps.setDouble(6, t.optionPremium);
                else                          ps.setNull(6, Types.REAL);
                ps.executeUpdate();
            } catch (SQLException e) {
                System.err.println("saveTrade error: " + e.getMessage());
            }
        }
    }

    public static List<Trade> getAllTrades() {
        synchronized (LOCK) {
            List<Trade> list = new ArrayList<>();
            try (Statement st = conn.createStatement();
                 ResultSet rs = st.executeQuery("SELECT * FROM trades ORDER BY id ASC")) {
                while (rs.next()) {
                    Trade t = new Trade();
                    t.id            = rs.getLong("id");
                    t.type          = rs.getString("type");
                    t.price         = rs.getDouble("price");
                    t.time          = rs.getString("time");
                    t.confidence    = rs.getString("confidence");
                    t.pnl           = rs.getDouble("pnl");
                    double op       = rs.getDouble("optionPremium");
                    t.optionPremium = rs.wasNull() ? null : op;
                    list.add(t);
                }
            } catch (SQLException e) {
                System.err.println("getAllTrades error: " + e.getMessage());
            }
            return list;
        }
    }

    /** Returns trades whose time starts with today's ISO date prefix (yyyy-MM-dd). */
    public static List<Trade> getTodayTrades() {
        String todayPrefix = LocalDate.now().toString(); // "yyyy-MM-dd"
        synchronized (LOCK) {
            List<Trade> list = new ArrayList<>();
            try (PreparedStatement ps = conn.prepareStatement(
                    "SELECT * FROM trades WHERE time LIKE ? ORDER BY id ASC")) {
                ps.setString(1, todayPrefix + "%");
                ResultSet rs = ps.executeQuery();
                while (rs.next()) {
                    Trade t = new Trade();
                    t.id            = rs.getLong("id");
                    t.type          = rs.getString("type");
                    t.price         = rs.getDouble("price");
                    t.time          = rs.getString("time");
                    t.confidence    = rs.getString("confidence");
                    t.pnl           = rs.getDouble("pnl");
                    double op       = rs.getDouble("optionPremium");
                    t.optionPremium = rs.wasNull() ? null : op;
                    list.add(t);
                }
            } catch (SQLException e) {
                System.err.println("getTodayTrades error: " + e.getMessage());
            }
            return list;
        }
    }

    public static void clearAllTrades() {
        synchronized (LOCK) {
            try (Statement st = conn.createStatement()) {
                st.execute("DELETE FROM trades");
            } catch (SQLException e) {
                System.err.println("clearAllTrades error: " + e.getMessage());
            }
        }
    }

    public record StockTick(String stockname, String time, double ltp, double qty) {}
}
