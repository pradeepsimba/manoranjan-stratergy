package com.trading.rest;

import com.trading.engine.TradeEngine;
import com.trading.model.AppState;
import com.trading.model.Trade;
import com.trading.service.DatabaseService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api")
public class DashboardController {

    @Autowired private TradeEngine    tradeEngine;
    @Autowired private DatabaseService dbService;

    private final AppState state = AppState.get();

    // ── Status ────────────────────────────────────────────────────────────────────

    @GetMapping("/status")
    public Map<String, Object> status() {
        return Map.of(
            "wsStatus",  state.wsStatus,
            "apiStatus", state.apiStatus,
            "interval",  state.selectedInterval,
            "funds",     state.availableFunds
        );
    }

    // ── Trades ────────────────────────────────────────────────────────────────────

    @GetMapping("/trades")
    public List<Trade> todayTrades() {
        return dbService.getTodayTrades();
    }

    @GetMapping("/trades/all")
    public List<Trade> allTrades() {
        return dbService.getAllTrades();
    }

    @DeleteMapping("/trades")
    public Map<String, String> clearTrades() {
        dbService.clearAllTrades();
        return Map.of("status", "cleared");
    }

    // ── Manual entry/exit ─────────────────────────────────────────────────────────

    @PostMapping("/entry")
    public Map<String, Object> manualEntry(@RequestBody Map<String, Object> body) {
        String type  = (String) body.get("type");
        double price = Double.parseDouble(body.get("price").toString());
        if (!"BUY".equals(type) && !"SELL".equals(type))
            return Map.of("error", "type must be BUY or SELL");
        if (state.activeTrade != null)
            return Map.of("error", "trade already active");
        tradeEngine.manualEntry(type, price);
        return Map.of("status", "entered", "type", type, "price", price);
    }

    @PostMapping("/exit")
    public Map<String, Object> manualExit() {
        if (state.activeTrade == null)
            return Map.of("error", "no active trade");
        tradeEngine.manualExit();
        return Map.of("status", "exited");
    }

    // ── Interval ──────────────────────────────────────────────────────────────────

    @PostMapping("/interval")
    public Map<String, String> setInterval(@RequestBody Map<String, String> body) {
        String interval = body.get("interval");
        if (List.of("1m","3m","5m","15m").contains(interval)) {
            state.selectedInterval = interval;
            return Map.of("status", "ok", "interval", interval);
        }
        return Map.of("error", "invalid interval");
    }

    // ── Funds ─────────────────────────────────────────────────────────────────────

    @PostMapping("/funds")
    public Map<String, Object> setFunds(@RequestBody Map<String, Object> body) {
        double funds = Double.parseDouble(body.get("funds").toString());
        state.availableFunds = funds;
        return Map.of("status", "ok", "funds", funds);
    }

    // ── Big Trades ────────────────────────────────────────────────────────────────

    @PostMapping("/big-trades/check")
    public Map<String, Object> bigTradesCheck() {
        return dbService.auditStockQtyStorage(200);
    }
}
