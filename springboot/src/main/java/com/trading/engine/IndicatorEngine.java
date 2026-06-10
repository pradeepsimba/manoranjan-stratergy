package com.trading.engine;

import com.trading.config.AppConfig;
import com.trading.model.AppState;
import com.trading.model.BNIndicators;
import com.trading.model.Candle;

import java.util.*;

public class IndicatorEngine {

    // ── EMA ──────────────────────────────────────────────────────────────────────

    public static double ema(List<Double> prices, int period) {
        if (prices.size() < period) return 0;
        double k = 2.0 / (period + 1);
        double val = 0;
        for (int i = 0; i < period; i++) val += prices.get(i);
        val /= period;
        for (int i = period; i < prices.size(); i++) val = prices.get(i) * k + val * (1 - k);
        return val;
    }

    // ── RSI(14) ───────────────────────────────────────────────────────────────────

    public static Double calcRSI(List<Candle> candles, int period) {
        if (candles.size() < period + 1) return null;
        double gains = 0, losses = 0;
        for (int i = candles.size() - period; i < candles.size(); i++) {
            double diff = candles.get(i).close - candles.get(i - 1).close;
            if (diff > 0) gains += diff; else losses -= diff;
        }
        if (losses == 0) return 100.0;
        double rs = (gains / period) / (losses / period);
        return Math.round((100 - 100 / (1 + rs)) * 10.0) / 10.0;
    }

    // ── MACD(12,26,9) ─────────────────────────────────────────────────────────────

    public static double[] calcMACD(List<Double> closes) {
        if (closes.size() < 34) return new double[]{0, 0, 0};
        // Build MACD line series (last 30 values) to compute proper 9-EMA signal
        int start = Math.max(26, closes.size() - 30);
        List<Double> macdSeries = new ArrayList<>();
        for (int i = start; i <= closes.size(); i++) {
            List<Double> sub = closes.subList(0, i);
            macdSeries.add(ema(sub, 12) - ema(sub, 26));
        }
        if (macdSeries.size() < 9) {
            double m = macdSeries.get(macdSeries.size() - 1);
            return new double[]{m, m, 0};
        }
        double macdLine  = macdSeries.get(macdSeries.size() - 1);
        double signalLine = ema(macdSeries, 9);   // true 9-period EMA of MACD line
        return new double[]{ macdLine, signalLine, macdLine - signalLine };
    }

    public static String macdDirection(List<Double> closes) {
        if (closes.size() < 35) return "—";
        double[] cur  = calcMACD(closes);
        double[] prev = calcMACD(closes.subList(0, closes.size() - 1));
        boolean crossUp   = prev[0] <= prev[1] && cur[0] > cur[1];
        boolean crossDown = prev[0] >= prev[1] && cur[0] < cur[1];
        if (crossUp)   return "CROSS↑";
        if (crossDown) return "CROSS↓";
        if (cur[0] > cur[1]) return "BUY";
        if (cur[0] < cur[1]) return "SELL";
        return "NEUTRAL";
    }

    // ── EMA Stack (20/50) ─────────────────────────────────────────────────────────

    public static BNIndicators.EmaStack calcEmaStack(List<Candle> candles) {
        if (candles.size() < 50) return null;
        List<Double> closes = new ArrayList<>();
        for (Candle c : candles) closes.add(c.close);
        double price = closes.get(closes.size() - 1);
        double ema20 = ema(closes, 20);
        double ema50 = ema(closes, 50);
        BNIndicators.EmaStack stack = new BNIndicators.EmaStack();
        stack.ema20   = Math.round(ema20 * 100.0) / 100.0;
        stack.ema50   = Math.round(ema50 * 100.0) / 100.0;
        stack.bullish = price > ema20 && ema20 > ema50;
        stack.bearish = price < ema20 && ema20 < ema50;
        return stack;
    }

    // ── Candlestick patterns ──────────────────────────────────────────────────────

    public static BNIndicators.LeaderPatterns checkLeaderPatterns() {
        BNIndicators.LeaderPatterns lp = new BNIndicators.LeaderPatterns();
        AppState st = AppState.get();
        for (String stockName : AppConfig.LEADER_STOCKS) {
            AppConfig.Stock stock = AppConfig.STOCKS.stream()
                .filter(s -> s.name().equals(stockName)).findFirst().orElse(null);
            if (stock == null) continue;
            List<Candle> candles = st.lastNCandles.get(stock.symbol());
            if (candles == null || candles.size() < 3) continue;
            Candle c  = candles.get(candles.size() - 1);
            Candle c1 = candles.get(candles.size() - 2);
            Candle c2 = candles.get(candles.size() - 3);
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
        double upper = c.isBullish() ? c.high - c.close : c.high - c.open;
        double lower = c.isBullish() ? c.open - c.low  : c.close - c.low;
        double prevBody = prev.body();
        // Hammer: long lower wick (≥2×body), small upper wick (≤50% body), small real body vs range
        if (c.isBullish() && lower >= 2 * body && upper <= body * 0.5 && body / range < 0.4 && prev.isBearish())
            return "Hammer (Bull)";
        // Shooting Star: long upper wick (≥2×body), small lower wick (≤50% body), small real body vs range
        if (c.isBearish() && upper >= 2 * body && lower <= body * 0.5 && body / range < 0.4 && prev.isBullish())
            return "Shooting Star (Bear)";
        // Engulfing: current body must be ≥90% of previous body size
        if (c.isBullish() && prev.isBearish() && c.open <= prev.close && c.close >= prev.open
                && body > prevBody * 0.9) return "Bull Engulfing";
        if (c.isBearish() && prev.isBullish() && c.open >= prev.close && c.close <= prev.open
                && body > prevBody * 0.9) return "Bear Engulfing";
        // Morning/Evening Star: middle candle must be small (≤40% of first candle body)
        if (c2 != null && c2.isBearish() && prev.body() <= c2.body() * 0.4 && c.isBullish()
                && c.close > (c2.open + c2.close) / 2) return "Morning Star (Bull)";
        if (c2 != null && c2.isBullish() && prev.body() <= c2.body() * 0.4 && c.isBearish()
                && c.close < (c2.open + c2.close) / 2) return "Evening Star (Bear)";
        return null;
    }

    // ── BN Gate ───────────────────────────────────────────────────────────────────

    public static BNIndicators checkBNIndicators() {
        BNIndicators ind = new BNIndicators();
        AppState st = AppState.get();

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

        ind.rsi     = calcRSI(bnCandles, 14);
        ind.macdDir = macdDirection(closes);
        if (closes.size() >= 34) {   // calcMACD needs 34 minimum
            double[] m = calcMACD(closes);
            ind.macdVal = Math.round(m[0] * 100.0) / 100.0;
        }
        ind.emaStack  = calcEmaStack(bnCandles);
        ind.leaderPat = checkLeaderPatterns();

        double bull = 0, bear = 0;

        // RSI zone scoring
        if (ind.rsi != null) {
            if (ind.rsi > 58)      bull += 1;
            else if (ind.rsi < 42) bear += 1;
            // Overbought/oversold extremes signal reversal risk — penalise the dominant side
            if (ind.rsi > 72)      bear += 0.5;   // overbought → adds bear pressure
            else if (ind.rsi < 28) bull += 0.5;   // oversold   → adds bull pressure
        }

        // MACD direction scoring
        switch (ind.macdDir != null ? ind.macdDir : "") {
            case "CROSS↑" -> bull += 2;
            case "CROSS↓" -> bear += 2;
            case "BUY"    -> bull += 1;
            case "SELL"   -> bear += 1;
        }

        // EMA stack scoring
        if (ind.emaStack != null) {
            if (ind.emaStack.bullish) bull += 2;
            if (ind.emaStack.bearish) bear += 2;
        }

        // Candlestick pattern scoring from leader stocks (+2 when ≥2 patterns align)
        if (ind.leaderPat != null) {
            if (ind.leaderPat.bullCount >= 2) bull += 2;
            if (ind.leaderPat.bearCount >= 2) bear += 2;
        }
        // Over-extended EMA penalty: adds pressure to the opposite side
        if (ind.emaStack != null && !bnCandles.isEmpty()) {
            double price = bnCandles.get(bnCandles.size() - 1).close;
            if (ind.emaStack.ema20 > 0) {
                double ext = Math.abs(price - ind.emaStack.ema20) / ind.emaStack.ema20 * 100;
                if (ext > 1.2) { if (price > ind.emaStack.ema20) bear += 0.5; else bull += 0.5; }
            }
        }
        ind.bull    = Math.max(0, bull);
        ind.bear    = Math.max(0, bear);
        ind.bullish = bull >= 2 && bull > bear + 0.9;
        ind.bearish = bear >= 2 && bear > bull + 0.9;
        return ind;
    }

    // ── Sideways filter ───────────────────────────────────────────────────────────

    // Uses close-to-close range of last 5 candles (not high-low), matching original logic
    public static Double sidewaysRange(List<Candle> candles) {
        if (candles == null || candles.size() < 5) return null;
        int start = candles.size() - 5;
        double hi = candles.get(start).close, lo = candles.get(start).close;
        for (int i = start + 1; i < candles.size(); i++) {
            if (candles.get(i).close > hi) hi = candles.get(i).close;
            if (candles.get(i).close < lo) lo = candles.get(i).close;
        }
        return hi - lo;
    }

    // ── Momentum ─────────────────────────────────────────────────────────────────

    // 10-period True Range ATR (max of H-L, |H-prevC|, |L-prevC|)
    static double calcATR(List<Candle> candles, int period) {
        if (candles.size() < period + 1) return -1;
        double sum = 0;
        for (int i = candles.size() - period; i < candles.size(); i++) {
            Candle c = candles.get(i), prev = candles.get(i - 1);
            double tr = Math.max(c.high - c.low,
                       Math.max(Math.abs(c.high - prev.close),
                                Math.abs(c.low  - prev.close)));
            sum += tr;
        }
        return sum / period;
    }

    public record MomentumResult(boolean ok, String reason) {}

    public static MomentumResult strongMomentum(List<Candle> candles, String interval) {
        if (candles == null || candles.size() < 2) return new MomentumResult(false, "No candles");
        Candle c1 = candles.get(candles.size() - 1);
        Candle c2 = candles.get(candles.size() - 2);
        double fixed = switch (interval) { case "3m" -> 20; case "5m" -> 28; case "15m" -> 50; default -> 15; };
        double atr = calcATR(candles, 10);
        // Dynamically lower threshold using ATR (makes it easier in choppy markets)
        double threshold = fixed;
        if (atr > 0) {
            double atrThr = atr * 0.7;
            threshold = Math.min(fixed, Math.max(atrThr, fixed * 0.6));
        }
        double c1Move = c1.close - c1.open, c2Move = c2.close - c2.open;
        double c1Abs  = Math.abs(c1Move), c2Abs = Math.abs(c2Move);
        // Case A: single candle ≥ 80% of (dynamic) threshold
        if (c1Abs >= threshold * 0.8)
            return new MomentumResult(true, String.format("C1=%.1f pts", c1Abs));
        // Case B: two same-direction candles combined ≥ threshold
        if (Math.signum(c1Move) == Math.signum(c2Move) && c1Abs + c2Abs >= threshold)
            return new MomentumResult(true, String.format("2C=%.1f+%.1f pts", c1Abs, c2Abs));
        // Case C: ATR-based gate — c1 ≥ 50% of raw ATR AND ≥ 60% of fixed threshold
        if (atr > 0 && c1Abs >= atr * 0.5 && c1Abs >= fixed * 0.6)
            return new MomentumResult(true, String.format("ATR=%.1f C1=%.1f", atr, c1Abs));
        return new MomentumResult(false, String.format("Weak: C1=%.1f need>=%.0f", c1Abs, threshold * 0.8));
    }
}
