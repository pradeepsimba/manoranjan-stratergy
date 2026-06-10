package com.trading.model;

public class Trade {
    public long   id;
    public String type;          // BUY / SELL / BUY_EXIT / SELL_EXIT
    public double price;
    public String time;
    public String confidence;
    public double pnl;
    public Double optionPremium; // nullable

    public Trade() {}
}
