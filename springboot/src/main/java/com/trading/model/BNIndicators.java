package com.trading.model;

import java.util.ArrayList;
import java.util.List;

public class BNIndicators {
    public Double  rsi;
    public String  macdDir;
    public Double  macdVal;
    public EmaStack emaStack;
    public LeaderPatterns leaderPat;
    public double  bull;
    public double  bear;
    public boolean bullish;
    public boolean bearish;

    public static class EmaStack {
        public double  ema20;
        public double  ema50;
        public boolean bullish;
        public boolean bearish;
    }

    public static class LeaderPatterns {
        public int bullCount;
        public int bearCount;
        public List<PatternMatch> matches = new ArrayList<>();
    }

    public static class PatternMatch {
        public String stock;
        public String pattern;
        public PatternMatch(String stock, String pattern) {
            this.stock = stock; this.pattern = pattern;
        }
    }
}
