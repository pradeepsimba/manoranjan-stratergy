package com.trading.service;

import com.trading.model.Trade;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import jakarta.annotation.PostConstruct;
import java.time.LocalDate;
import java.util.List;

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
}
