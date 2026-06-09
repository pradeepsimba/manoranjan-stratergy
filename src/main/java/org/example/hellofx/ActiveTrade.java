package org.example.hellofx;

public class ActiveTrade {
    public String type;        // BUY or SELL
    public double entry;
    public String entryTime;
    public String confidence;
    public double currentSL;

    public ActiveTrade(String type, double entry, String entryTime, String confidence) {
        this.type       = type;
        this.entry      = entry;
        this.entryTime  = entryTime;
        this.confidence = confidence;
        this.currentSL  = type.equals("BUY") ? entry - AppConfig.STOPLOSS : entry + AppConfig.STOPLOSS;
    }
}
