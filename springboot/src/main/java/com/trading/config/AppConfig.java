package com.trading.config;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class AppConfig {

    private AppConfig() {}

    public static final String API_HOST          = "34.100.254.34";
    public static final String API_URL_TEMPLATE  = "https://%s:8000/api/historical-data/?from_date=%s&to_date=%s";
    public static final String WS_URL            = "ws://" + API_HOST + ":8083/historical-data";

    public static final String INDEX_NAME   = "BANKNIFTY";
    public static final String INDEX_SYMBOL = "26009";

    // Trading constants
    public static final double TARGET            = 35;
    public static final double STOPLOSS          = 18;
    public static final double BREAKEVEN_TRIGGER = 12;
    public static final double TRAIL_TRIGGER     = 18;
    public static final double TRAIL_DISTANCE    = 12;
    public static final int    LOT_SIZE          = 30;
    public static final int    SAME_DIRECTION_REQUIRED = 3;

    public static final double DEFAULT_FUNDS  = 100_000;
    public static final double RISK_FREE_RATE = 0.065;

    public static final List<Stock> STOCKS = List.of(
        new Stock("BANKNIFTY",            "26009"),
        new Stock("HDFC BANK",            "1333"),
        new Stock("ICICI BANK",           "4963"),
        new Stock("AXIS BANK",            "5900"),
        new Stock("STATE BANK OF INDIA",  "3045"),
        new Stock("KOTAK MAHINDRA BANK",  "1922"),
        new Stock("INDUSIND BANK",        "5258"),
        new Stock("AU SMALL FINANCE BANK","21238"),
        new Stock("FEDERAL BANK",         "1023"),
        new Stock("IDFC FIRST BANK",      "11184"),
        new Stock("PUNJAB NATIONAL BANK", "10666"),
        new Stock("CANARA BANK",          "10794")
    );

    public static final List<String> LEADER_STOCKS = List.of(
        "HDFC BANK", "ICICI BANK", "AXIS BANK",
        "STATE BANK OF INDIA", "KOTAK MAHINDRA BANK", "INDUSIND BANK"
    );

    public static final Map<String, Double> INDEX_WEIGHTS;
    static {
        INDEX_WEIGHTS = new LinkedHashMap<>();
        INDEX_WEIGHTS.put("1333",  31.86);
        INDEX_WEIGHTS.put("4963",  20.14);
        INDEX_WEIGHTS.put("3045",  17.83);
        INDEX_WEIGHTS.put("1922",   8.79);
        INDEX_WEIGHTS.put("5900",   7.96);
        INDEX_WEIGHTS.put("5258",   2.92);
        INDEX_WEIGHTS.put("10666",  2.86);
        INDEX_WEIGHTS.put("10794",  2.40);
        INDEX_WEIGHTS.put("11184",  1.40);
        INDEX_WEIGHTS.put("21238",  1.35);
        INDEX_WEIGHTS.put("1023",   1.19);
    }

    public static final Map<String, Integer> STOCK_QTY_THRESHOLD;
    static {
        STOCK_QTY_THRESHOLD = new LinkedHashMap<>();
        STOCK_QTY_THRESHOLD.put("HDFC BANK",           1000);
        STOCK_QTY_THRESHOLD.put("ICICI BANK",          1000);
        STOCK_QTY_THRESHOLD.put("AXIS BANK",           1000);
        STOCK_QTY_THRESHOLD.put("STATE BANK OF INDIA", 1000);
        STOCK_QTY_THRESHOLD.put("KOTAK MAHINDRA BANK", 1000);
        STOCK_QTY_THRESHOLD.put("INDUSIND BANK",       1000);
    }

    public static final int MARKET_OPEN_HOUR   = 9;
    public static final int MARKET_OPEN_MIN    = 15;
    public static final int MARKET_CLOSE_HOUR  = 15;
    public static final int MARKET_CLOSE_MIN   = 30;
    public static final int ENTRY_START_HOUR   = 9;
    public static final int ENTRY_START_MIN    = 30;
    public static final int ENTRY_END_HOUR     = 15;
    public static final int ENTRY_END_MIN      = 0;

    public record Stock(String name, String symbol) {}
}
