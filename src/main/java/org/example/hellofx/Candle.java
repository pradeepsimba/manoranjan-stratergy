package org.example.hellofx;

public class Candle {
    public String startTime;
    public double open;
    public double close;
    public double high;
    public double low;
    public double volume;

    public Candle() {}

    public Candle(String startTime, double open, double close, double high, double low, double volume) {
        this.startTime = startTime;
        this.open   = open;
        this.close  = close;
        this.high   = high;
        this.low    = low;
        this.volume = volume;
    }

    public boolean isBullish() { return close > open; }
    public boolean isBearish() { return close < open; }
    public double  body()      { return Math.abs(close - open); }
    public double  range()     { return high - low; }

    @Override
    public String toString() {
        return String.format("Candle{%s O=%.2f C=%.2f H=%.2f L=%.2f}", startTime, open, close, high, low);
    }
}
