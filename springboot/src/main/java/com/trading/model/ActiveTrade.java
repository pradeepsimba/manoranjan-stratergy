package com.trading.model;

import com.trading.config.AppConfig;

public class ActiveTrade {
    public String type;        // BUY or SELL
    public double entry;
    public String entryTime;
    public String confidence;
    public double currentSL;
    public int    numLots;     // position size (1% risk-based)

    public ActiveTrade(String type, double entry, String entryTime, String confidence, int numLots) {
        this.type       = type;
        this.entry      = entry;
        this.entryTime  = entryTime;
        this.confidence = confidence;
        this.numLots    = numLots;
        this.currentSL  = type.equals("BUY") ? entry - AppConfig.STOPLOSS : entry + AppConfig.STOPLOSS;
    }
}
