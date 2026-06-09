package org.example.hellofx;

public class BNIndicators {
    public Double  rsi;        // null if not enough data
    public String  macdDir;    // BUY / SELL / CROSS↑ / CROSS↓ / NEUTRAL
    public Double  macdVal;
    public EmaStack emaStack;
    public LeaderPatterns leaderPat;
    public double  bull;
    public double  bear;
    public boolean bullish;    // gate open bullish
    public boolean bearish;    // gate open bearish

    public static class EmaStack {
        public double ema20;
        public double ema50;
        public boolean bullish;
        public boolean bearish;
    }

    public static class LeaderPatterns {
        public int    bullCount;
        public int    bearCount;
        public java.util.List<PatternMatch> matches = new java.util.ArrayList<>();
    }

    public static class PatternMatch {
        public String stock;
        public String pattern;
        public PatternMatch(String stock, String pattern) {
            this.stock   = stock;
            this.pattern = pattern;
        }
    }
}
