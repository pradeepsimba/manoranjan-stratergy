package org.example.hellofx;

import java.util.*;

/**
 * Detects swing, S/R, and pivot breakouts for BankNifty candles.
 */
public class BreakoutEngine {

    public record SwingPoint(int index, double price, String time) {}
    public record Swings(List<SwingPoint> highs, List<SwingPoint> lows) {}
    public record Breakout(String type, String direction, double level) {}
    public record SRLevels(List<Double> supports, List<Double> resistances) {}

    // ── Swing detection ───────────────────────────────────────────────────────

    public static Swings detectSwings(List<Candle> candles, int lookback) {
        List<SwingPoint> highs = new ArrayList<>();
        List<SwingPoint> lows  = new ArrayList<>();
        for (int i = lookback; i < candles.size() - lookback; i++) {
            double h = candles.get(i).high;
            double l = candles.get(i).low;
            boolean isHigh = true, isLow = true;
            for (int j = i - lookback; j <= i + lookback; j++) {
                if (j == i) continue;
                if (candles.get(j).high >= h) isHigh = false;
                if (candles.get(j).low  <= l) isLow  = false;
            }
            if (isHigh) highs.add(new SwingPoint(i, h, candles.get(i).startTime));
            if (isLow)  lows.add(new SwingPoint(i, l, candles.get(i).startTime));
        }
        return new Swings(highs, lows);
    }

    public static Optional<Breakout> detectSwingBreakouts(List<Candle> candles, Swings swings) {
        if (swings.highs().isEmpty() || swings.lows().isEmpty() || candles.isEmpty())
            return Optional.empty();
        Candle latest   = candles.get(candles.size() - 1);
        double recentHigh = swings.highs().get(swings.highs().size() - 1).price();
        double recentLow  = swings.lows().get(swings.lows().size()  - 1).price();
        if (latest.close > recentHigh && latest.high > recentHigh)
            return Optional.of(new Breakout("swing", "bullish", recentHigh));
        if (latest.close < recentLow  && latest.low  < recentLow)
            return Optional.of(new Breakout("swing", "bearish", recentLow));
        return Optional.empty();
    }

    // ── S/R detection ────────────────────────────────────────────────────────

    public static SRLevels detectSupportResistance(List<Candle> candles) {
        List<Double> supports     = new ArrayList<>();
        List<Double> resistances  = new ArrayList<>();
        for (int i = 2; i < candles.size() - 2; i++) {
            double p2 = candles.get(i-2).close, p1 = candles.get(i-1).close;
            double c  = candles.get(i).close;
            double n1 = candles.get(i+1).close, n2 = candles.get(i+2).close;
            if (c < p1 && c < p2 && c < n1 && c < n2) supports.add(c);
            if (c > p1 && c > p2 && c > n1 && c > n2) resistances.add(c);
        }
        return new SRLevels(
            clusterLevels(supports, 0.25).stream().skip(Math.max(0, supports.size() - 3)).toList(),
            clusterLevels(resistances, 0.25).stream().skip(Math.max(0, resistances.size() - 3)).toList()
        );
    }

    public static Optional<Breakout> detectSRBreakouts(List<Candle> candles, SRLevels sr) {
        if (candles.isEmpty() || sr.supports().isEmpty() || sr.resistances().isEmpty())
            return Optional.empty();
        Candle latest     = candles.get(candles.size() - 1);
        double support    = sr.supports().get(0);
        double resistance = sr.resistances().get(0);
        if (latest.close < support    && latest.low  < support)    return Optional.of(new Breakout("support",    "bearish", support));
        if (latest.close > resistance && latest.high > resistance) return Optional.of(new Breakout("resistance", "bullish", resistance));
        return Optional.empty();
    }

    // ── Pivot breakout ────────────────────────────────────────────────────────

    public static Optional<Breakout> detectPivotBreakout(List<Candle> candles) {
        if (candles.size() < 2) return Optional.empty();
        Candle prev   = candles.get(candles.size() - 2);
        Candle latest = candles.get(candles.size() - 1);
        double pivot  = (prev.high + prev.low + prev.close) / 3;
        if (latest.close > pivot && latest.high > pivot) return Optional.of(new Breakout("pivot", "bullish", pivot));
        if (latest.close < pivot && latest.low  < pivot) return Optional.of(new Breakout("pivot", "bearish", pivot));
        return Optional.empty();
    }

    // ── Main detect ──────────────────────────────────────────────────────────

    public static String detectBreakouts(List<Candle> bnCandles) {
        if (bnCandles.size() < 3) return "Insufficient data";
        Swings  swings = detectSwings(bnCandles, 2);
        SRLevels sr    = detectSupportResistance(bnCandles);

        Optional<Breakout> bo = detectSwingBreakouts(bnCandles, swings);
        if (bo.isEmpty()) bo  = detectSRBreakouts(bnCandles, sr);
        if (bo.isEmpty()) bo  = detectPivotBreakout(bnCandles);

        if (bo.isPresent()) {
            Breakout b   = bo.get();
            String type  = b.type().substring(0, 1).toUpperCase() + b.type().substring(1);
            String dir   = b.direction().substring(0, 1).toUpperCase() + b.direction().substring(1);
            return String.format("%s Breakout: %s (%.2f)", type, dir, b.level());
        }
        return "No Breakout Detected";
    }

    // ── Cluster levels ────────────────────────────────────────────────────────

    static List<Double> clusterLevels(List<Double> levels, double threshold) {
        if (levels.isEmpty()) return Collections.emptyList();
        List<Double> sorted = new ArrayList<>(levels);
        Collections.sort(sorted);
        List<Double> clusters = new ArrayList<>();
        List<Double> group = new ArrayList<>();
        group.add(sorted.get(0));
        for (int i = 1; i < sorted.size(); i++) {
            double prev = sorted.get(i - 1);
            double curr = sorted.get(i);
            if (prev > 0 && Math.abs(curr - prev) / prev < threshold / 100.0) {
                group.add(curr);
            } else {
                clusters.add(average(group));
                group = new ArrayList<>();
                group.add(curr);
            }
        }
        if (!group.isEmpty()) clusters.add(average(group));
        return clusters;
    }

    private static double average(List<Double> arr) {
        return arr.stream().mapToDouble(Double::doubleValue).average().orElse(0);
    }
}
