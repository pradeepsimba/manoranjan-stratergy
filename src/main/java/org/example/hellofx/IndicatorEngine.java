package org.example.hellofx;

import java.util.*;

/**
 * Computes RSI(14), MACD(12,26,9), EMA(20/50) stack, candlestick patterns
 * and the BN Gate (bull/bear score ≥ 2, dominant side leads by > 0.9).
 */
public class IndicatorEngine {

    // ── EMA ────────────────────────────────────────────────────────────────────

    public static double ema(List<Double> prices, int period) {
        if (prices.size() < period) return 0;
        // Seed with SMA of the first `period` values, then apply EMA multiplier
        double k = 2.0 / (period + 1);
        double emaVal = 0;
        for (int i = 0; i < period; i++) emaVal += prices.get(i);
        emaVal /= period;
        for (int i = period; i < prices.size(); i++) {
            emaVal = prices.get(i) * k + emaVal * (1 - k);
        }
        return emaVal;
    }

    // ── RSI(14) ────────────────────────────────────────────────────────────────

    public static Double calcRSI(List<Candle> candles, int period) {
        if (candles.size() < period + 1) return null;
        double gains = 0, losses = 0;
        for (int i = candles.size() - period; i < candles.size(); i++) {
            double diff = candles.get(i).close - candles.get(i - 1).close;
            if (diff > 0) gains  += diff;
            else          losses -= diff;
        }
        if (losses == 0) return 100.0;
        double rs = (gains / period) / (losses / period);
        return Math.round((100 - 100 / (1 + rs)) * 10.0) / 10.0;
    }

    // ── MACD(12,26,9) ──────────────────────────────────────────────────────────

    public static double[] calcMACD(List<Double> closes) {
        if (closes.size() < 26) return new double[]{0, 0, 0};
        double ema12 = ema(closes, 12);
        double ema26 = ema(closes, 26);
        double macdLine = ema12 - ema26;

        // Signal: EMA(9) of MACD line — approximate with last 9 macd values
        // Simplified: use raw MACD vs previous MACD to detect cross
        int sz = closes.size();
        double prevEma12 = ema(closes.subList(0, sz - 1), 12);
        double prevEma26 = ema(closes.subList(0, sz - 1), 26);
        double prevMacd  = prevEma12 - prevEma26;

        // Signal line = 9-period EMA of MACD; simplified to prior value
        double signalLine = prevMacd;
        double histogram  = macdLine - signalLine;
        return new double[]{ macdLine, signalLine, histogram };
    }

    public static String macdDirection(List<Double> closes) {
        if (closes.size() < 27) return "—";
        double[] cur  = calcMACD(closes);
        double[] prev = calcMACD(closes.subList(0, closes.size() - 1));
        double macd     = cur[0];
        double signal   = cur[1];
        double prevMacd = prev[0];
        double prevSig  = prev[1];

        boolean crossUp   = prevMacd <= prevSig && macd > signal;
        boolean crossDown = prevMacd >= prevSig && macd < signal;
        if (crossUp)   return "CROSS↑";
        if (crossDown) return "CROSS↓";
        if (macd > signal) return "BUY";
        if (macd < signal) return "SELL";
        return "NEUTRAL";
    }

    // ── EMA Stack (20/50) ──────────────────────────────────────────────────────

    public static BNIndicators.EmaStack calcEmaStack(List<Candle> candles) {
        if (candles.size() < 50) return null;
        List<Double> closes = new ArrayList<>();
        for (Candle c : candles) closes.add(c.close);

        double price = closes.get(closes.size() - 1);
        double ema20  = ema(closes, 20);
        double ema50  = ema(closes, 50);

        BNIndicators.EmaStack stack = new BNIndicators.EmaStack();
        stack.ema20    = Math.round(ema20 * 100.0) / 100.0;
        stack.ema50    = Math.round(ema50 * 100.0) / 100.0;
        stack.bullish  = price > ema20 && ema20 > ema50;
        stack.bearish  = price < ema20 && ema20 < ema50;
        return stack;
    }

    // ── Candlestick Patterns for leader stocks ────────────────────────────────

    public static BNIndicators.LeaderPatterns checkLeaderPatterns() {
        BNIndicators.LeaderPatterns lp = new BNIndicators.LeaderPatterns();
        AppState st = AppState.get();

        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName))
                .findFirst().orElse(null);
            if (stock == null) continue;

            List<Candle> candles = st.lastNCandles.get(stock.symbol());
            if (candles == null || candles.size() < 3) continue;

            Candle c  = candles.get(candles.size() - 1);
            Candle c1 = candles.get(candles.size() - 2);
            Candle c2 = candles.size() >= 3 ? candles.get(candles.size() - 3) : null;

            String pat = detectPattern(c, c1, c2);
            if (pat == null) continue;

            boolean bull = pat.contains("Bull") || pat.contains("Morning") || pat.contains("Hammer");
            boolean bear = pat.contains("Bear") || pat.contains("Evening") || pat.contains("Shooting");
            if (bull) lp.bullCount++;
            if (bear) lp.bearCount++;
            lp.matches.add(new BNIndicators.PatternMatch(stockName, pat));
        }
        return lp;
    }

    private static String detectPattern(Candle c, Candle prev, Candle c2) {
        double body  = c.body();
        double range = c.range();
        if (range == 0) return null;
        double upperWick = c.isBullish() ? c.high - c.close : c.high - c.open;
        double lowerWick = c.isBullish() ? c.open - c.low  : c.close - c.low;

        // Hammer (bullish reversal at bottom)
        if (c.isBullish() && lowerWick >= 2 * body && upperWick < body * 0.3 && prev.isBearish())
            return "Hammer (Bull)";
        // Shooting star (bearish reversal at top)
        if (c.isBearish() && upperWick >= 2 * body && lowerWick < body * 0.3 && prev.isBullish())
            return "Shooting Star (Bear)";
        // Bullish engulfing
        if (c.isBullish() && prev.isBearish() && c.open < prev.close && c.close > prev.open)
            return "Bull Engulfing";
        // Bearish engulfing
        if (c.isBearish() && prev.isBullish() && c.open > prev.close && c.close < prev.open)
            return "Bear Engulfing";
        // Morning star
        if (c2 != null && c2.isBearish() && prev.body() < c2.body() * 0.3 && c.isBullish()
                && c.close > (c2.open + c2.close) / 2)
            return "Morning Star (Bull)";
        // Evening star
        if (c2 != null && c2.isBullish() && prev.body() < c2.body() * 0.3 && c.isBearish()
                && c.close < (c2.open + c2.close) / 2)
            return "Evening Star (Bear)";
        return null;
    }

    // ── BN Gate ────────────────────────────────────────────────────────────────

    public static BNIndicators checkBNIndicators() {
        BNIndicators ind = new BNIndicators();
        AppState st = AppState.get();

        // Take a snapshot so background writes don't cause ConcurrentModificationException
        List<Candle> bnCandles;
        synchronized (st.bnIndicatorCandles) {
            bnCandles = new ArrayList<>(st.bnIndicatorCandles);
        }
        if (bnCandles.isEmpty()) {
            List<Candle> dc = st.lastNCandles.get(AppConfig.INDEX_SYMBOL);
            bnCandles = dc != null ? new ArrayList<>(dc) : Collections.emptyList();
        }

        List<Double> closes = new ArrayList<>();
        for (Candle c : bnCandles) closes.add(c.close);

        // RSI
        ind.rsi = calcRSI(bnCandles, 14);

        // MACD
        String mDir = macdDirection(closes);
        ind.macdDir = mDir;
        if (closes.size() >= 26) {
            double[] m = calcMACD(closes);
            ind.macdVal = Math.round(m[0] * 100.0) / 100.0;
        }

        // EMA Stack
        ind.emaStack = calcEmaStack(bnCandles);

        // Leader patterns
        ind.leaderPat = checkLeaderPatterns();

        // ── Scoring ──────────────────────────────────────────────────────────
        double bull = 0, bear = 0;

        // RSI contribution
        if (ind.rsi != null) {
            if (ind.rsi > 58)      { bull += 1; }
            else if (ind.rsi < 42) { bear += 1; }
            if (ind.rsi > 72)      { bull += 0.5; }   // overbought — slight bear lean
            else if (ind.rsi < 28) { bear += 0.5; }   // oversold — slight bull lean
        }

        // MACD contribution
        switch (mDir) {
            case "CROSS↑" -> bull += 2;
            case "CROSS↓" -> bear += 2;
            case "BUY"    -> bull += 1;
            case "SELL"   -> bear += 1;
        }

        // EMA Stack contribution
        if (ind.emaStack != null) {
            if (ind.emaStack.bullish) bull += 2;
            if (ind.emaStack.bearish) bear += 2;
        }

        // EMA over-extension penalty (if price > EMA20 by >1.2%, reduce bull score)
        if (ind.emaStack != null && bnCandles.size() > 0) {
            double price = bnCandles.get(bnCandles.size() - 1).close;
            if (ind.emaStack.ema20 > 0) {
                double ext = Math.abs(price - ind.emaStack.ema20) / ind.emaStack.ema20 * 100;
                if (ext > 1.2) {
                    if (price > ind.emaStack.ema20) bull -= 0.5;
                    else                            bear -= 0.5;
                }
            }
        }

        ind.bull    = Math.max(0, bull);
        ind.bear    = Math.max(0, bear);
        ind.bullish = bull >= 2 && bull > bear + 0.9;
        ind.bearish = bear >= 2 && bear > bull + 0.9;

        return ind;
    }

    // ── Sideways filter ────────────────────────────────────────────────────────

    /** Returns the range (high−low) over last 5 candles, or null if insufficient data. */
    public static Double sidewaysRange(List<Candle> candles) {
        if (candles == null || candles.size() < 5) return null;
        int start = candles.size() - 5;
        double maxHigh = candles.get(start).high;
        double minLow  = candles.get(start).low;
        for (int i = start + 1; i < candles.size(); i++) {
            if (candles.get(i).high > maxHigh) maxHigh = candles.get(i).high;
            if (candles.get(i).low  < minLow)  minLow  = candles.get(i).low;
        }
        return maxHigh - minLow;
    }

    // ── Momentum ──────────────────────────────────────────────────────────────

    public static double getMomentumThreshold(String interval) {
        return switch (interval) {
            case "3m"  -> 20;
            case "5m"  -> 25;
            case "15m" -> 40;
            default    -> 15;
        };
    }

    /** ATR-based threshold: 0.5 × ATR(5). */
    static double atrThreshold(List<Candle> candles) {
        if (candles.size() < 5) return 999;
        double atr = 0;
        int n = Math.min(5, candles.size() - 1);
        for (int i = candles.size() - n; i < candles.size(); i++) {
            atr += candles.get(i).range();
        }
        return 0.5 * (atr / n);
    }

    public record MomentumResult(boolean ok, String reason) {}

    public static MomentumResult strongMomentum(List<Candle> candles, String interval) {
        if (candles == null || candles.size() < 2)
            return new MomentumResult(false, "No candles");

        Candle c1 = candles.get(candles.size() - 1);  // latest
        Candle c2 = candles.get(candles.size() - 2);  // prev

        double momPts  = getMomentumThreshold(interval);
        double atrThr  = atrThreshold(candles);

        double c1Move = c1.close - c1.open;
        double c2Move = c2.close - c2.open;
        double c1Abs  = Math.abs(c1Move);
        double c2Abs  = Math.abs(c2Move);

        // Single strong candle
        if (c1Abs >= momPts * 0.8)
            return new MomentumResult(true, String.format("C1=%.1f pts", c1Abs));

        // Two candles same direction
        if (Math.signum(c1Move) == Math.signum(c2Move) && c1Abs + c2Abs >= momPts)
            return new MomentumResult(true, String.format("2C=%.1f+%.1f pts", c1Abs, c2Abs));

        // ATR-dynamic check
        if (c1Abs >= atrThr)
            return new MomentumResult(true, String.format("ATR=%.1f C1=%.1f", atrThr, c1Abs));

        return new MomentumResult(false,
            String.format("Weak: C1=%.1f need ≥%.0f or ATR≥%.1f", c1Abs, momPts * 0.8, atrThr));
    }
}
